import html
import json
import mimetypes
import re
import shutil
import subprocess
import os
import textwrap
from fractions import Fraction
from pathlib import Path

from . import store
from . import camera
from .models import ExportRequest

FULL_FFMPEG = Path('/opt/homebrew/opt/ffmpeg-full/bin')
FFMPEG = os.environ.get('CLIP_FFMPEG', str(FULL_FFMPEG / 'ffmpeg') if (FULL_FFMPEG / 'ffmpeg').exists() else 'ffmpeg')
FFPROBE = os.environ.get('CLIP_FFPROBE', str(FULL_FFMPEG / 'ffprobe') if (FULL_FFMPEG / 'ffprobe').exists() else 'ffprobe')
OUTPUT_FPS = 30
FRAME_TIME_EPSILON = 1e-8
FRAME_INDEX_EPSILON = 1e-9
SDR_TONEMAP_VERSION = 2
SDR_HDR_SIDEDATA_FILTER = ('sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA,'
                           'sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL')


def runtime_profile():
    return 'desktop' if os.environ.get('CLIP_RUNTIME', '').lower() == 'desktop' else 'server'


def ffmpeg_threads():
    try:
        return max(1, min(8, int(os.environ.get('CLIP_FFMPEG_THREADS', '4' if runtime_profile() == 'desktop' else '2'))))
    except ValueError:
        return 4 if runtime_profile() == 'desktop' else 2


def video_encoder_args(mode, width, height, metadata=None, proxy=False):
    """Keep server defaults; local desktop explicitly requires VideoToolbox."""
    if runtime_profile() != 'desktop':
        if mode == 'hdr':
            return ['-c:v', 'libx265', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p10le',
                    '-tag:v', 'hvc1', '-x265-params', hdr_encoder_params(metadata)]
        return ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-threads', '2']
    codec = 'hevc_videotoolbox' if mode == 'hdr' else 'h264_videotoolbox'
    factor = 4 if proxy else (10 if mode == 'hdr' else 6)
    proxy_cap = 6_000_000
    if proxy:
        try:
            proxy_cap = max(500_000, min(6_000_000, int(os.environ.get('CLIP_DESKTOP_PROXY_BITRATE', '6000000'))))
        except ValueError:
            pass
    bitrate = min(proxy_cap if proxy else 32_000_000,
                  max(500_000 if proxy else 6_000_000, int(width * height * factor)))
    args = ['-c:v', codec, '-allow_sw', '0', '-b:v', str(bitrate)]
    if mode == 'hdr':
        args += ['-pix_fmt', 'yuv420p10le', '-tag:v', 'hvc1', '-color_primaries', 'bt2020',
                 '-color_trc', metadata['transfer'], '-colorspace', metadata['matrix'] if metadata['matrix'] != 'unknown' else 'bt2020nc',
                 '-color_range', 'tv']
    else:
        args += ['-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']
    return args


def proxy_dimensions(width, height, short_edge):
    short = min(int(width), int(height))
    scale = min(1.0, float(short_edge) / short)
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def proxy_short_edge(asset):
    if asset.get('preview_width') and asset.get('preview_height'):
        return min(int(asset['preview_width']), int(asset['preview_height']))
    # Older database entries store only proxy height. Estimate width from the
    # original aspect ratio to recognize low-resolution portrait proxies too.
    height = int(asset.get('preview_height') or 0)
    if height <= 0:
        return 0
    width = round(height * int(asset['width']) / int(asset['height']))
    return min(width, height)


def playback_capabilities(asset):
    """Report a safe MIME and candidate browser support without probing/decode."""
    name = str(asset.get('original') or '')
    suffix = Path(name).suffix.lower()
    mime_by_suffix = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
                      '.webm': 'video/webm', '.mkv': 'video/x-matroska'}
    mime = mime_by_suffix.get(suffix)
    if not mime:
        path = store.DATA/'assets'/asset['id']/name
        try:
            with path.open('rb') as source:
                header = source.read(16)
            if len(header) >= 8 and header[4:8] == b'ftyp':
                mime = 'video/mp4'
            elif header.startswith(b'\x1a\x45\xdf\xa3'):
                mime = 'video/x-matroska'
            else:
                mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        except OSError:
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    codec = str(asset.get('codec') or '').lower()
    browser_playable = mime == 'video/mp4' and codec in {'h264', 'hevc'} and not asset.get('hdr')
    return {'original_mime': mime, 'original_codec': codec,
            'browser_playable': browser_playable}


def playback_color_filters(metadata):
    """Place desktop 30 fps decimation before expensive SDR tone mapping."""
    filters = ['fps=30:start_time=0'] if runtime_profile() == 'desktop' else []
    filters.append(to_sdr(metadata))
    return filters


def frame_sample_filter(sample_time):
    threshold = max(0.0, float(sample_time) - FRAME_TIME_EPSILON)
    return f"select='gte(t,{threshold:.12g})'"


def frame_index_for_time(time):
    return max(0, int(max(0.0, float(time)) * OUTPUT_FPS + FRAME_INDEX_EPSILON))


def probe(path):
    result = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, text=True, timeout=45, check=True)
    info = json.loads(result.stdout)
    video = next((s for s in info.get("streams", []) if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise ValueError("В файле нет видеодорожки")
    # HEVC mastering SEI and some colour information are attached to decoded frames.
    frame_result = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-read_intervals', '%+#8', '-show_frames', '-of', 'json', str(path)], capture_output=True, text=True, timeout=45, check=True)
    frames = json.loads(frame_result.stdout).get('frames', [])
    if frames:
        first = frames[0]
        for key in ('color_transfer', 'color_primaries', 'color_space'):
            if video.get(key, 'unknown') == 'unknown' and first.get(key):
                video[key] = first[key]
        known = {s.get('side_data_type') for s in video.get('side_data_list', [])}
        video.setdefault('side_data_list', []).extend(s for s in first.get('side_data_list', []) if s.get('side_data_type') not in known and s.get('side_data_type') in {'Mastering display metadata', 'Content light level metadata'})
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    if not 0 < duration <= 14400:
        raise ValueError("Нужен видеофайл с известной длительностью до 4 часов")
    transfer = video.get("color_transfer", "unknown")
    primaries = video.get("color_primaries", "unknown")
    pixel = video.get("pix_fmt", "unknown")
    depth = int(video.get("bits_per_raw_sample") or (10 if "10" in pixel else 12 if "12" in pixel else 8))
    dv = any("dovi" in s.get("side_data_type", "").lower() or "dolby" in s.get("side_data_type", "").lower() for s in video.get("side_data_list", []))
    hdr = transfer in ("smpte2084", "arib-std-b67") and primaries == "bt2020" and depth >= 10
    width, height = video["width"], video["height"]
    rotation = next((s.get("rotation", 0) for s in video.get("side_data_list", []) if "rotation" in s), 0)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return {"duration": duration, "width": width, "height": height, "codec": video["codec_name"], "pix_fmt": pixel, "bit_depth": depth,
            "transfer": transfer, "primaries": primaries, "matrix": video.get("color_space", "unknown"), "hdr": hdr,
            "dolby_vision": dv, "has_audio": any(s["codec_type"] == "audio" for s in info.get("streams", [])),
            "size": path.stat().st_size, "side_data": video.get("side_data_list", [])}


def ffmpeg_log_time(log):
    """Parse the most recent FFmpeg stats timestamp from a bounded log tail."""
    try:
        with Path(log).open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            tail = stream.read(8192).decode('utf-8', errors='replace')
    except OSError:
        return None
    matches = re.findall(r'time=(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)', tail)
    if not matches:
        return None
    hours, minutes, seconds = matches[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def run_ffmpeg(args, work, name="encode", timeout=10800, progress=None):
    import time
    from . import storage
    log = work / f"{name}.log"
    with log.open("w") as output:
        threads = ffmpeg_threads()
        process = subprocess.Popen([FFMPEG, "-hide_banner", "-nostdin", "-y", "-threads", str(threads), "-filter_threads", str(min(threads, 2)), *map(str, args)], stdout=output, stderr=output)
        deadline = time.monotonic() + timeout
        last_progress_at = 0.0
        try:
            while process.poll() is None:
                storage.guard()
                if time.monotonic() > deadline:
                    raise ValueError("Обработка превысила допустимое время")
                now = time.monotonic()
                if progress and now - last_progress_at >= 2:
                    processed = ffmpeg_log_time(log)
                    if processed is not None:
                        progress(processed)
                        last_progress_at = now
                time.sleep(.5)
        except Exception:
            process.kill(); process.wait()
            raise
    if process.returncode:
        tail = log.read_text(errors="replace")[-2500:]
        if 'videotoolbox' in ' '.join(map(str, args)).lower() and '-allow_sw' in map(str, args):
            raise ValueError('Не удалось использовать аппаратный VideoToolbox. Программный fallback отключён; проверьте сборку FFmpeg и доступность кодировщика H.264/HEVC на этом Mac.')
        raise RuntimeError(f"FFmpeg не смог обработать видео. Подробности: {tail}")


def to_sdr(metadata):
    if metadata["hdr"]:
        matrix = metadata['matrix'] if metadata['matrix'] != 'unknown' else 'bt2020nc'
        # HLG's scene signal needs its nominal 1000 nit display peak to avoid
        # lifting ordinary midtones. Keep the established PQ path unchanged;
        # zscale's npl is an HLG display parameter, not a PQ mastering override.
        npl = 1000 if metadata['transfer'] == 'arib-std-b67' else 100
        return f"zscale=pin=bt2020:tin={metadata['transfer']}:min={matrix}:t=linear:npl={npl},format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=limited,format=yuv420p"
    primaries = metadata['primaries'] if metadata['primaries'] != 'unknown' else 'bt709'
    transfer = metadata['transfer'] if metadata['transfer'] != 'unknown' else 'bt709'
    matrix = metadata['matrix'] if metadata['matrix'] not in ('unknown', 'gbr') else 'bt709'
    return f"zscale=pin={primaries}:tin={transfer}:min={matrix}:p=bt709:t=bt709:m=bt709:r=limited,format=yuv420p"


def render_source_preview(clip, resolution, target, work):
    """Encode a short, clean SDR playback copy of one source range."""
    asset = store.asset(clip.asset_id)
    if not asset:
        raise ValueError('Исходное видео не найдено')
    source = store.DATA / 'assets' / clip.asset_id / asset['original']
    if not source.is_file():
        raise ValueError('Исходный файл видео не найден. Перезагрузите его')
    if clip.end > asset['duration'] + 0.05:
        raise ValueError('Конец фрагмента выходит за длительность видео')
    duration = clip.end - clip.start
    if duration <= 0 or duration > 180:
        raise ValueError('Предпросмотр исходника ограничен фрагментом до 180 секунд')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    # `resolution` is the target short edge: 1080 means 1920x1080 landscape
    # or 1080x1920 portrait. Each expression is also capped by the native size,
    # so smaller originals remain at native resolution instead of being enlarged.
    vf = (f"{','.join(playback_color_filters(asset))},scale=w='min(iw,{resolution}*iw/min(iw,ih))':"
          f"h='min(ih,{resolution}*ih/min(iw,ih))':"
          "force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,"
          "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=limited," + SDR_HDR_SIDEDATA_FILTER)
    short_edge = min(asset['width'], asset['height'])
    output_short = min(resolution, short_edge)
    output_width = max(2, int(asset['width'] * output_short / short_edge) // 2 * 2)
    output_height = max(2, int(asset['height'] * output_short / short_edge) // 2 * 2)
    encoder = video_encoder_args('sdr', output_width, output_height)
    run_ffmpeg([
        '-ss', clip.start, '-i', source, '-t', duration,
        '-map', '0:v:0', '-map', '0:a:0?', '-vf', vf,
        *encoder, '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-ac', '2',
        '-af', 'apad', '-movflags', '+faststart', target,
    ], work, 'source-preview', timeout=max(120, duration * 20))
    result = probe(target)
    if result['hdr']:
        target.unlink(missing_ok=True)
        raise RuntimeError('Не удалось преобразовать предпросмотр в SDR')
    return {'width': result['width'], 'height': result['height'], 'mode': 'sdr',
            'source_start': clip.start, 'source_end': clip.end, 'has_audio': result['has_audio']}


def hdr_encoder_params(asset):
    params = ['pools=2', 'frame-threads=1', 'colorprim=bt2020', f"transfer={asset['transfer']}", 'colormatrix=bt2020nc']
    for item in asset.get('side_data', []):
        if item.get('side_data_type') == 'Mastering display metadata':
            def coordinate(key):
                return round(Fraction(item[key]) * 50000)
            def luminance(key):
                return round(Fraction(item[key]) * 10000)
            params.append(f"master-display=G({coordinate('green_x')},{coordinate('green_y')})B({coordinate('blue_x')},{coordinate('blue_y')})R({coordinate('red_x')},{coordinate('red_y')})WP({coordinate('white_point_x')},{coordinate('white_point_y')})L({luminance('max_luminance')},{luminance('min_luminance')})")
        elif item.get('side_data_type') == 'Content light level metadata':
            params.append(f"max-cll={item['max_content']},{item['max_average']}")
    return ':'.join(params)


def prepare_asset(asset_id, original, title, source="file", source_url=None, preview_source=None):
    meta = probe(original)
    if meta["dolby_vision"]:
        raise ValueError("Обнаружен Dolby Vision. В этой версии его монтаж не поддерживается: сначала подготовьте HLG/PQ или SDR-файл в видеоредакторе.")
    directory = original.parent
    proxy = directory / "proxy.mp4"
    thumb = directory / "thumbnail.jpg"
    preview_meta=probe(preview_source) if preview_source else meta
    if abs(preview_meta["duration"]-meta["duration"])>2:
        raise ValueError("Длительность предпросмотра не совпадает с оригиналом")
    proxy_meta = render_playback_proxy(preview_meta, preview_source or original, proxy, directory,
                                       1080 if runtime_profile() == 'desktop' else 540)
    run_ffmpeg(["-ss", min(1, meta["duration"] / 2), "-i", proxy, "-frames:v", "1", "-vf", "scale=320:-2", thumb], directory, "thumbnail", timeout=60)
    body = {"id": asset_id, "title": title[:150], "source": source, "source_url": source_url, "original": original.name, "preview_width": proxy_meta['width'], "preview_height": proxy_meta["height"], "preview_hdr": False, **meta}
    store.put_asset(body)
    return body


def render_playback_proxy(metadata, source, target, work, short_edge, progress=None):
    """Build a seekable SDR proxy without loading the source into Python memory."""
    width, height = proxy_dimensions(metadata['width'], metadata['height'], short_edge)
    vf = (f"{','.join(playback_color_filters(metadata))},scale=w='min(iw,{short_edge}*iw/min(iw,ih))':"
          f"h='min(ih,{short_edge}*ih/min(iw,ih))':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,")
    vf += "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=limited"
    vf += ',' + SDR_HDR_SIDEDATA_FILTER
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(work).mkdir(parents=True, exist_ok=True)
    run_ffmpeg(['-i', source, '-map', '0:v:0', '-map', '0:a:0?', '-vf', vf, '-r', '30',
                *video_encoder_args('sdr', width, height, proxy=True), '-g', '30',
                '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', target],
               Path(work), 'playback-proxy', timeout=max(120, metadata['duration'] * 5), progress=progress)
    result = probe(target)
    if result['hdr'] or abs(result['duration'] - metadata['duration']) > .3:
        target.unlink(missing_ok=True)
        raise RuntimeError('Проверка SDR-предпросмотра не прошла')
    return result


def upgrade_playback_proxy(asset_id, work, progress=None):
    asset = store.asset(asset_id)
    if not asset:
        raise ValueError('Исходное видео не найдено')
    source = store.DATA/'assets'/asset_id/asset['original']
    if not source.is_file():
        raise ValueError('Исходный файл видео не найден')
    short_edge = min(1080, int(asset['width']), int(asset['height']))
    current = store.DATA/'assets'/asset_id/'proxy.mp4'
    if proxy_short_edge(asset) >= short_edge and current.is_file():
        return {'asset_id': asset_id, 'metadata': {'width': asset.get('preview_width'),
                'height': asset['preview_height'], 'mode': 'sdr', 'preview_height': asset['preview_height']}}
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    temporary = work/'proxy-upgrade.mp4'
    try:
        metadata = render_playback_proxy(asset, source, temporary, work, short_edge, progress=progress)
        # Atomic replacement keeps readers of the old proxy working until this
        # complete, seekable file has passed its duration/color validation.
        temporary.replace(current)
        updated = {**asset, 'preview_width': metadata['width'], 'preview_height': metadata['height'], 'preview_hdr': False}
        store.put_asset(updated)
        return {'asset_id': asset_id, 'metadata': {'width': metadata['width'], 'height': metadata['height'],
                'mode': 'sdr', 'preview_height': metadata['height']}}
    finally:
        temporary.unlink(missing_ok=True)


def timestamp(value):
    parts = value.replace(",", ".").split(":")
    return sum(float(part) * 60 ** i for i, part in enumerate(reversed(parts)))


def read_cues(path):
    if not path or not path.exists():
        return []
    content = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    matches = re.finditer(r"(?m)^(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)", content, re.S)
    cues = []
    for match in matches:
        text = html.unescape(re.sub(r"<[^>]+>", "", match[3])).strip()
        start, end = timestamp(match[1]), timestamp(match[2])
        if text and end > start and (not cues or cues[-1]["text"] != text):
            cues.append({"start": start, "end": end, "text": text})
    return cues[:30000]


def subtitle_path(asset_id):
    folder = store.DATA / "assets" / asset_id
    paths = list(folder.glob("captions.*")) + sorted(folder.glob("source*.vtt"))
    return paths[0] if paths else None


def srt_time(seconds):
    milliseconds = round(max(0, seconds) * 1000)
    return f"{milliseconds // 3600000:02}:{milliseconds // 60000 % 60:02}:{milliseconds // 1000 % 60:02},{milliseconds % 1000:03}"


def clip_subtitles(asset_id, start, end, target):
    entries = []
    for cue in read_cues(subtitle_path(asset_id)):
        if cue["end"] > start and cue["start"] < end:
            # SRT is rendered by libass: escape override tags and backslash commands.
            text = cue["text"].replace("\\", "＼").replace("{", "｛").replace("}", "｝")
            entries.append(f"{len(entries)+1}\n{srt_time(max(start, cue['start']) - start)} --> {srt_time(min(end, cue['end']) - start)}\n{text}\n")
    target.write_text("\n".join(entries), encoding="utf-8")
    return bool(entries)


def validate_export(request):
    project = request.project
    if not project.clips:
        raise ValueError("Добавьте хотя бы один фрагмент")
    metadata = []
    for clip in project.clips:
        asset = store.asset(clip.asset_id)
        if not asset:
            raise ValueError("Исходное видео не найдено")
        if clip.end > asset["duration"] + 0.05:
            raise ValueError("Конец фрагмента выходит за длительность видео")
        if asset["dolby_vision"]:
            raise ValueError("Монтаж Dolby Vision пока не поддерживается")
        metadata.append(asset)
    if request.mode == "hdr":
        if not all(a["hdr"] for a in metadata):
            raise ValueError("Для HDR-экспорта все фрагменты должны иметь HDR-исходники")
        if len({(a["transfer"], a["primaries"], a["matrix"]) for a in metadata}) != 1:
            raise ValueError("Для HDR используйте исходники с одинаковыми цветовыми характеристиками")
        if project.style.title or project.style.subtitles or project.style.border or project.style.background != "#101116" or project.style.layout != "plain" or project.style.corner_radius or project.style.account or project.style.header_image:
            raise ValueError("HDR пока поддерживает нарезку и кадрирование с чёрными полями. Текст, цветная рамка и субтитры доступны при SDR-экспорте")
    return metadata


def output_size(style, resolution):
    ratio = {"9:16": (9, 16), "1:1": (1, 1), "16:9": (16, 9)}[style.aspect]
    base = resolution
    width, height = (base, round(base * ratio[1] / ratio[0])) if ratio[0] <= ratio[1] else (round(base * ratio[0] / ratio[1]), base)
    return width // 2 * 2, height // 2 * 2


def final_color_metadata_filter(mode):
    """Restore encoder frame tags that composed/zoompan filters may drop."""
    if mode == "sdr":
        return SDR_HDR_SIDEDATA_FILTER + ",setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=limited"
    return None


def video_filters(request, clip, asset, index, work, timeline_offset=0, sample_time=None):
    """Shared source-to-canvas video filters for exports and exact-frame renders."""
    from . import composition
    style = request.project.style
    width, height = output_size(style, request.resolution)
    base = request.resolution
    left, top, inner_w, inner_h = composition.geometry(style, width, height, base)
    filters = []
    if timeline_offset:
        filters.append(f"setpts=PTS-STARTPTS+{timeline_offset:.12g}/TB")
    # Normalize the source clock before evaluating camera trajectories. Camera time is
    # always the 30 fps export timeline, independent of the input's frame rate.
    filters.append("fps=30:start_time=0")
    if sample_time is not None:
        filters.append(frame_sample_filter(sample_time))
    filters.append("zscale=r=limited,format=yuv420p10le" if request.mode == "hdr" else to_sdr(asset))
    camera_background = "black" if request.mode == "hdr" else style.background
    if style.fit == "cover":
        if clip.camera:
            transform = None if request.mode == "hdr" else camera.zoompan(clip, asset["width"], asset["height"], inner_w, inner_h, style.fit, camera_background)
            if transform:
                filters.append(transform)
                if sample_time is not None:
                    filters.append(f"setpts=PTS+{sample_time:.12g}/TB")
            else:
                filters.append(f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=increase:force_divisible_by=2")
                exp = camera.expressions(clip)
                filters.append(f"scale=w='ceil(iw*({exp['zoom']})/2)*2':h='ceil(ih*({exp['zoom']})/2)*2':eval=frame")
                filters.append(f"crop={inner_w}:{inner_h}:x='(in_w-out_w)*({exp['x']})':y='(in_h-out_h)*({exp['y']})'")
        else:
            filters.append(f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=increase:force_divisible_by=2")
            filters.append(f"crop={inner_w}:{inner_h}:x=(in_w-out_w)*{clip.crop_x}:y=(in_h-out_h)/2")
    else:
        if clip.camera:
            transform = None if request.mode == "hdr" else camera.zoompan(clip, asset["width"], asset["height"], inner_w, inner_h, style.fit, camera_background)
            if transform:
                filters.append(transform)
                if sample_time is not None:
                    filters.append(f"setpts=PTS+{sample_time:.12g}/TB")
            else:
                filters.append(f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={inner_w}:{inner_h}:(ow-iw)/2:(oh-ih)/2:color={camera_background}")
                exp = camera.expressions(clip)
                filters.append(f"scale=w='ceil(iw*({exp['zoom']})/2)*2':h='ceil(ih*({exp['zoom']})/2)*2':eval=frame")
                filters.append(f"crop={inner_w}:{inner_h}:x='(in_w-out_w)*({exp['x']})':y='(in_h-out_h)*({exp['y']})'")
        else:
            filters.append(f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={inner_w}:{inner_h}:(ow-iw)/2:(oh-ih)/2:color={'black' if request.mode == 'hdr' else style.background}")
    if left or top or inner_h != height or inner_w != width:
        filters.append(f"pad={width}:{height}:{left}:{top}:color={style.background}")
    filters.append("setsar=1")
    color_filter = final_color_metadata_filter(request.mode)
    if color_filter:
        filters.append(color_filter)
    subtitle_filter = None
    if style.subtitles:
        subtitles = work / f"captions{index}.srt"
        if clip_subtitles(clip.asset_id, clip.start, clip.end, subtitles):
            subtitle_filter = f"subtitles='{subtitles}':force_style='Fontname=DejaVu Sans,Fontsize=20,Outline=2,MarginV=48'"
    return filters, subtitle_filter, width, height


def export_video(job):
    request = ExportRequest.model_validate(job["payload"])
    assets = validate_export(request)
    style = request.project.style
    width, height = output_size(style, request.resolution)
    base = request.resolution
    work = store.DATA / "work" / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    from . import composition
    canvas = work / "canvas.png"
    designed = request.mode == 'sdr'
    if designed:
        composition.overlay(style, width, height, base, canvas)
    title = work / "title.txt"
    title_size = round(style.font_size * base / 1080)
    columns = max(10, int(width * .88 / (title_size * .62)))
    wrapped = '\n'.join('\n'.join(textwrap.wrap(line, columns, break_on_hyphens=False)) for line in style.title.split('\n'))
    title.write_text(wrapped, encoding="utf-8")
    total = len(assets)
    pieces = []
    for index, (clip, asset) in enumerate(zip(request.project.clips, assets)):
        store.update_job(job["id"], message=f"Рендер фрагмента {index+1} из {total}", progress=0.05 + index / total * 0.85)
        source = store.DATA / "assets" / clip.asset_id / asset["original"]
        piece = work / f"part{index}.mp4"
        filters, subtitle_filter, _, _ = video_filters(request, clip, asset, index, work)
        if style.title and style.layout == "plain" and not designed:
            font = next((p for p in [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"), Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")] if p.exists()), None)
            size = round(style.font_size * base / 1080)
            y = {"top": "h*0.12", "center": "(h-text_h)/2", "bottom": "h*0.76-text_h"}[style.title_position]
            font_arg = f"fontfile='{font}':" if font else ""
            filters.append(f"drawtext={font_arg}textfile='{title}':expansion=none:fontsize={size}:fontcolor={style.text_color}:box=1:boxcolor=black@0.6:boxborderw=14:x=(w-text_w)/2:y={y}")
        args = ["-ss", clip.start, "-i", source]
        if not asset["has_audio"] or style.mute:
            args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        if designed:
            canvas_index = 2 if not asset["has_audio"] or style.mute else 1
            args += ["-i", canvas]
            # Overlay after crop/pad, before subtitles so captions are never hidden.
            graph = f"[0:v]{','.join(filters)}[video];[video][{canvas_index}:v]overlay=0:0:format=yuv420"
            if subtitle_filter:
                graph += "," + subtitle_filter
            graph += ",format=yuv420p," + final_color_metadata_filter(request.mode) + "[v]"
            args += ["-filter_complex_threads", "1", "-filter_complex", graph, "-map", "[v]"]
        else:
            if subtitle_filter:
                filters.append(subtitle_filter)
            color_filter = final_color_metadata_filter(request.mode)
            if color_filter:
                filters.append(color_filter)
            args += ["-map", "0:v:0", "-vf", ",".join(filters)]
        args += ["-t", clip.end - clip.start, "-map", "1:a:0" if not asset["has_audio"] or style.mute else "0:a:0"]
        args += video_encoder_args(request.mode, width, height, asset)
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-af", "apad", "-movflags", "+faststart", piece]
        run_ffmpeg(args, work, f"part{index}")
        pieces.append(piece)
    store.update_job(job["id"], message="Сборка и проверка файла", progress=0.93)
    manifest = work / "concat.txt"
    manifest.write_text("\n".join(f"file '{p}'" for p in pieces), encoding="utf-8")
    output = store.DATA / "exports" / f"{job['id']}.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", manifest, "-c", "copy", "-movflags", "+faststart", output], work, "concat")
    meta = probe(output)
    if request.mode == "hdr" and not meta["hdr"]:
        output.unlink(missing_ok=True)
        raise RuntimeError("Проверка готового файла не подтвердила HDR")
    result = {"url": f"/api/exports/{job['id']}", "name": request.project.name, "mode": request.mode, "metadata": meta}
    shutil.rmtree(work)
    return result


def render_frame(request: ExportRequest, time: float, target: Path, work: Path):
    """Render one SDR export frame from original media using global output seconds."""
    if request.mode == "hdr":
        raise ValueError("Кадр монитора пока поддерживает только SDR")
    assets = validate_export(request)
    width, height = output_size(request.project.style, request.resolution)
    frame_index = frame_index_for_time(time)
    clip_frames = [max(1, int((clip.end - clip.start) * OUTPUT_FPS + .999999999)) for clip in request.project.clips]
    total_frames = sum(clip_frames)
    frame_index = min(frame_index, total_frames - 1)
    actual_time = frame_index / OUTPUT_FPS
    start_frame = 0
    selected = 0
    for idx, count in enumerate(clip_frames):
        if frame_index < start_frame + count:
            selected = idx
            break
        start_frame += count
    clip = request.project.clips[selected]
    asset = assets[selected]
    local_frame = frame_index - start_frame
    local_time = local_frame / OUTPUT_FPS
    source_time = clip.start + local_time
    seek_time = max(0.0, source_time - .5)
    timeline_offset = seek_time - clip.start
    source = store.DATA / "assets" / clip.asset_id / asset["original"]
    work.mkdir(parents=True, exist_ok=True)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    from . import composition
    canvas = work / "frame-canvas.png"
    composition.overlay(request.project.style, width, height, request.resolution, canvas)
    filters, subtitle_filter, _, _ = video_filters(request, clip, asset, selected, work, timeline_offset, sample_time=local_time)
    # Match export's YUV composition before converting to PNG RGB. Leaving the
    # overlay format automatic makes FFmpeg 5.1 choose RGB here, while the MP4
    # export chooses YUV420, producing visibly different colors (especially greens).
    graph = f"[0:v]{','.join(filters)}[video];[video][1:v]overlay=0:0:format=yuv420"
    if subtitle_filter:
        graph += "," + subtitle_filter
    graph += ",format=yuv420p," + final_color_metadata_filter("sdr")
    graph += ",scale=w=iw:h=ih:in_color_matrix=bt709:out_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24[v]"
    args = ["-ss", seek_time, "-i", source, "-i", canvas, "-filter_complex_threads", "1", "-filter_complex", graph,
            "-map", "[v]", "-frames:v", "1", "-an", "-sn", "-dn", target]
    run_ffmpeg(args, work, "preview-frame", timeout=120)
    return {"width": width, "height": height, "time": actual_time, "mode": "sdr"}
