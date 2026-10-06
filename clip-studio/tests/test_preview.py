import time
import uuid
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend import media, preview, storage, store
from backend.app import app
from test_pipeline import data, request_for, source
from backend.models import Clip, Project


def test_frame_request_deduplicates_and_supersedes_queued_scrub_frames(data):
    asset = source(data)
    request = request_for(asset)
    body = {'project': request.project.model_dump(), 'mode': 'sdr', 'resolution': 720, 'time': .2}
    with TestClient(app) as client:
        first = client.post('/api/preview/frame', json=body)
        repeated = client.post('/api/preview/frame', json=body)
        assert first.status_code == repeated.status_code == 202
        assert first.json()['job_id'] == repeated.json()['job_id']
        newer = client.post('/api/preview/frame', json={**body, 'time': .3})
        assert newer.status_code == 202
    with store.connect() as conn:
        rows = conn.execute("SELECT id,status FROM jobs WHERE kind='preview-frame' ORDER BY created").fetchall()
    assert [(row['id'], row['status']) for row in rows] == [
        (first.json()['job_id'], 'cancelled'), (newer.json()['job_id'], 'queued')]


def test_frame_hdr_request_is_rejected_without_calling_it_sdr(data):
    asset = source(data)
    request = request_for(asset)
    with TestClient(app) as client:
        response = client.post('/api/preview/frame', json={
            'project': request.project.model_dump(), 'mode': 'hdr', 'resolution': 720, 'time': .2})
    assert response.status_code == 400
    assert 'SDR' in response.json()['detail'] and 'HDR' in response.json()['detail']


def test_asset_cannot_be_deleted_while_nested_preview_request_is_queued(data):
    asset = source(data)
    request = request_for(asset)
    store.new_job('preview-frame', {'request': request.model_dump(), 'time': .5, 'fingerprint': 'a' * 64})
    assert storage.busy(asset['id'])
    try:
        storage.delete_asset(asset['id'])
        assert False, 'expected the active preview to hold the source asset'
    except ValueError as exc:
        assert 'очереди' in str(exc)


def test_preview_file_serves_only_valid_cache_ids_and_expired_files(data):
    cache_id = 'a' * 64
    folder = data/'previews'
    folder.mkdir()
    path = folder/f'{cache_id}.png'
    path.write_bytes(b'png placeholder')
    old = time.time() - storage.PREVIEW_TTL - 5
    import os
    os.utime(path, (old, old))
    with TestClient(app) as client:
        assert client.get('/api/previews/not-a-key').status_code == 404
        assert client.get(f'/api/previews/{cache_id}').status_code == 404


def test_original_asset_supports_http_byte_ranges_and_media_mime(data):
    asset = source(data)
    with TestClient(app) as client:
        response = client.get(f"/api/assets/{asset['id']}/original", headers={'Range': 'bytes=0-31'})
    assert response.status_code == 206
    assert response.headers['content-type'].startswith('video/x-matroska')
    assert response.headers['accept-ranges'] == 'bytes'
    assert response.headers['content-range'].endswith('/' + str(asset['size']))
    assert len(response.content) == 32


def test_extensionless_mp4_original_is_identified_for_browser_playback(data):
    asset_id = uuid.uuid4().hex
    folder = data/'assets'/asset_id
    folder.mkdir(parents=True)
    (folder/'original').write_bytes(b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00')
    store.put_asset({'id':asset_id,'original':'original','codec':'h264','hdr':False,'title':'MP4'})
    with TestClient(app) as client:
        assets = client.get('/api/assets').json()
        original = client.get(f'/api/assets/{asset_id}/original')
    found = next(item for item in assets if item['id'] == asset_id)
    assert found['original_mime'] == 'video/mp4'
    assert found['original_codec'] == 'h264' and found['browser_playable']
    assert original.headers['content-type'] == 'video/mp4'


def test_local_desktop_health_advertises_renderer_and_seekable_previews(data, monkeypatch):
    monkeypatch.setenv('CLIP_RUNTIME', 'desktop')
    with TestClient(app) as client:
        response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json()['runtime'] == 'desktop'
    assert response.json()['renderer'] == 'videotoolbox'
    assert response.json()['preview'] == {
        'original': 'byte-range', 'proxy': 'seekable', 'source': 'fhd-on-demand'}


def test_desktop_video_encoders_require_hardware_and_keep_portrait_proxy_fhd(data, monkeypatch):
    monkeypatch.setenv('CLIP_RUNTIME', 'desktop')
    h264 = media.video_encoder_args('sdr', 1920, 1080)
    hevc = media.video_encoder_args('hdr', 3840, 2160, {
        'transfer': 'smpte2084', 'matrix': 'bt2020nc'})
    assert h264[:3] == ['-c:v', 'h264_videotoolbox', '-allow_sw']
    assert '0' in h264 and '-b:v' in h264
    assert h264[h264.index('-b:v') + 1] == str(1920 * 1080 * 6)
    assert hevc[:3] == ['-c:v', 'hevc_videotoolbox', '-allow_sw']
    assert '0' in hevc and '-tag:v' in hevc
    proxy_encoder = media.video_encoder_args('sdr', 1920, 1080, proxy=True)
    assert proxy_encoder[proxy_encoder.index('-b:v') + 1] == '6000000'
    assert media.proxy_dimensions(1920, 1080, 1080) == (1920, 1080)
    assert media.proxy_dimensions(1080, 1920, 1080) == (1080, 1920)
    assert media.proxy_dimensions(640, 360, 1080) == (640, 360)


def test_desktop_playback_downsamples_before_sdr_tonemapping(data, monkeypatch):
    asset = source(data, hdr=True)
    monkeypatch.setenv('CLIP_RUNTIME', 'desktop')
    filters = media.playback_color_filters(asset)
    assert filters[0] == 'fps=30:start_time=0'
    assert filters[1] == media.to_sdr(asset)
    monkeypatch.setenv('CLIP_RUNTIME', 'server')
    assert media.playback_color_filters(asset) == [media.to_sdr(asset)]


def test_desktop_early_fps_keeps_duration_audio_and_software_test_fallback(data, monkeypatch):
    asset = source(data, hdr=True)
    clip = Clip(id='fps-preview', asset_id=asset['id'], start=.5, end=1.5)
    output = data/'desktop-fps-source.mp4'
    monkeypatch.setenv('CLIP_RUNTIME', 'desktop')
    monkeypatch.setattr(media, 'video_encoder_args', lambda mode, width, height, metadata=None, proxy=False:
                        ['-c:v','libx264','-preset','ultrafast','-crf','20','-pix_fmt','yuv420p','-threads','1'])
    metadata = media.render_source_preview(clip, 720, output, data/'work'/'desktop-fps')
    rate = subprocess.run([media.FFPROBE,'-v','error','-select_streams','v:0','-show_entries',
                           'stream=avg_frame_rate','-of','default=nw=1:nk=1',str(output)],
                          capture_output=True,text=True,check=True,timeout=30).stdout.strip()
    assert rate == '30/1'
    assert metadata['has_audio']
    assert media.probe(output)['duration'] == pytest.approx(1.0, abs=.08)


def test_zero_retention_preserves_local_assets_and_exports(data, monkeypatch):
    asset = source(data)
    export = data/'exports'/'older-export.mp4'
    export.write_bytes(b'completed export')
    old = time.time() - 60*86400
    import os
    os.utime(data/'assets'/asset['id'], (old, old))
    os.utime(export, (old, old))
    monkeypatch.setenv('CLIP_RETENTION_DAYS', '0')
    storage.cleanup()
    assert store.asset(asset['id']) is not None
    assert (data/'assets'/asset['id']/'source.mkv').is_file()
    assert export.is_file()


def test_ffmpeg_progress_parser_reads_only_the_log_tail(data):
    log = data/'long-encode.log'
    log.write_bytes(b'x' * 20000 + b'frame= 300 fps=28 time=01:12:34.50 bitrate=6000kbits/s\n')
    assert media.ffmpeg_log_time(log) == pytest.approx(4354.5)


def test_legacy_proxy_upgrade_is_queued_and_deduplicated(data, monkeypatch):
    asset = source(data)
    legacy = {**asset, 'width': 1920, 'height': 1080, 'preview_width': 426, 'preview_height': 240}
    store.put_asset(legacy)
    monkeypatch.setenv('CLIP_RUNTIME', 'desktop')
    with TestClient(app) as client:
        first = client.post(f"/api/assets/{asset['id']}/playback/prepare")
        second = client.post(f"/api/assets/{asset['id']}/playback/prepare")
    assert first.status_code == second.status_code == 202
    assert first.json()['job_id'] == second.json()['job_id']
    row = store.jobs()[0]
    assert row['kind'] == 'prepare-playback'
    assert row['context']['title'] == asset['title']
    assert row['context']['asset_id'] == asset['id']


def test_legacy_proxy_upgrade_preflights_disk_before_queueing(data, monkeypatch):
    asset = source(data)
    store.put_asset({**asset, 'width':1920, 'height':1080, 'preview_width':426, 'preview_height':240})
    monkeypatch.setenv('CLIP_RUNTIME', 'desktop')
    monkeypatch.setenv('CLIP_DATA_BUDGET_MB', '1')
    with TestClient(app) as client:
        response = client.post(f"/api/assets/{asset['id']}/playback/prepare")
    assert response.status_code == 507
    assert 'выбранным фрагментом' in response.json()['detail']
    with store.connect() as conn:
        count = conn.execute("SELECT count(*) FROM jobs WHERE kind='prepare-playback'").fetchone()[0]
    assert count == 0


def test_legacy_proxy_upgrade_replaces_proxy_only_after_verified_output(data, monkeypatch):
    asset = source(data)
    folder = data/'assets'/asset['id']
    proxy = folder/'proxy.mp4'
    proxy.write_bytes(b'old proxy remains readable until replacement')
    store.put_asset({**asset, 'preview_width': 160, 'preview_height': 90})
    monkeypatch.setenv('CLIP_RUNTIME', 'server')
    result = media.upgrade_playback_proxy(asset['id'], data/'work'/'upgrade-test')
    actual = media.probe(proxy)
    assert result['metadata']['mode'] == 'sdr'
    assert (actual['width'], actual['height']) == (320, 180)
    assert actual['duration'] == pytest.approx(asset['duration'], abs=.3)
    assert proxy.read_bytes() != b'old proxy remains readable until replacement'
    assert store.asset(asset['id'])['preview_width'] == 320


def test_worker_renders_sdr_snapshot_cache_from_original(data):
    from PIL import Image
    from backend import preview
    from backend.worker import process
    asset = source(data)
    request = request_for(asset)
    from backend.models import ExportRequest
    request.project.style.title = 'Точный кадр'
    fingerprint = preview.fingerprint(request)
    job_id = store.new_job('preview-frame', {'request': request.model_dump(), 'time': .5, 'fingerprint': fingerprint})
    result = process({'id': job_id, 'kind': 'preview-frame', 'payload': {
        'request': request.model_dump(), 'time': .5, 'fingerprint': fingerprint}})
    output = data/'previews'/f'{fingerprint}.png'
    assert result['cache_id'] == fingerprint and result['metadata']['mode'] == 'sdr'
    assert result['metadata']['time'] == .5 and output.is_file()
    image = Image.open(output)
    assert image.size == (720, 1280)


def test_fractional_multiclip_timeline_accepts_and_selects_last_rendered_frame(data, monkeypatch):
    asset = source(data)
    request = request_for(asset)
    request.project.clips = [
        request.project.clips[0].model_copy(update={'start': 0, 'end': .37}),
        request.project.clips[0].model_copy(update={'id': 'second', 'start': 1, 'end': 1.37}),
    ]
    body = preview.PreviewFrameRequest(project=request.project, mode='sdr', resolution=720, time=23/30)
    preview.validate(body)
    observed = {}
    monkeypatch.setattr(media, 'run_ffmpeg', lambda args, work, *a, **kw: observed.update(args=args))
    result = media.render_frame(request, 23/30, data/'frame.png', data/'work'/'frame')
    args = observed['args']
    assert result['time'] == 23/30
    assert float(args[1]) + .5 == pytest.approx(1 + 11/30)


def test_cache_guard_evicts_only_required_old_entry_and_preserves_current(data, monkeypatch):
    monkeypatch.setattr(storage, 'PREVIEW_MAX_COUNT', 3)
    folder = data/'previews'
    folder.mkdir()
    files = []
    for index in range(3):
        path = folder/f'{index:064x}.png'
        path.write_bytes(b'x')
        import os
        os.utime(path, (time.time()+index, time.time()+index))
        files.append(path)
    storage.guard_preview_cache(incoming_bytes=1, preserve=files[-1])
    assert not files[0].exists()
    assert files[1].exists() and files[2].exists()


def test_renderer_version_invalidates_existing_preview_fingerprint(data, monkeypatch):
    asset = source(data)
    request = request_for(asset)
    before = preview.fingerprint(request)
    monkeypatch.setattr(preview, 'PREVIEW_RENDERER_VERSION', preview.PREVIEW_RENDERER_VERSION + 1)
    assert preview.fingerprint(request) != before


def test_tonemap_version_invalidates_source_preview_fingerprint(data, monkeypatch):
    from backend.models import Clip
    asset = source(data)
    clip = Clip(id='source-preview', asset_id=asset['id'], start=.5, end=1.5)
    request = preview.SourcePreviewRequest(clip=clip, resolution=1080)
    before = preview.source_fingerprint(request)
    monkeypatch.setattr(preview, 'SDR_TONEMAP_VERSION', preview.SDR_TONEMAP_VERSION + 1)
    assert preview.source_fingerprint(request) != before


def test_tonemap_version_invalidates_exact_frame_and_full_video_fingerprints(data, monkeypatch):
    from backend.models import ExportRequest
    asset = source(data)
    project = request_for(asset).project
    frame_request = preview.PreviewFrameRequest(project=project, mode='sdr', resolution=720, time=.5)
    video_request = ExportRequest(project=project, mode='sdr', resolution=720)
    frame_before = preview.fingerprint(frame_request)
    video_before = preview.fingerprint(video_request)
    monkeypatch.setattr(preview, 'SDR_TONEMAP_VERSION', preview.SDR_TONEMAP_VERSION + 1)
    assert preview.fingerprint(frame_request) != frame_before
    assert preview.fingerprint(video_request) != video_before


def test_project_color_mode_persists_as_default_export_mode():
    project = Project.model_validate({'color_mode': 'hdr'})
    assert project.model_dump()['color_mode'] == 'hdr'
    assert media.ExportRequest.model_validate({'project': project}).mode == 'hdr'
    assert media.ExportRequest.model_validate({'project': project, 'mode': 'sdr'}).mode == 'sdr'


def test_source_preview_is_a_deduplicated_selected_range_job(data):
    asset = source(data)
    clip = request_for(asset).project.clips[0]
    body = {'clip': clip.model_dump(), 'resolution': 1080}
    with TestClient(app) as client:
        first = client.post('/api/preview/source', json=body)
        second = client.post('/api/preview/source', json=body)
        assert first.status_code == second.status_code == 202
        assert first.json()['job_id'] == second.json()['job_id']
    with store.connect() as conn:
        row = conn.execute("SELECT kind,payload FROM jobs WHERE id=?", (first.json()['job_id'],)).fetchone()
    assert row['kind'] == 'preview-source'
    import json
    payload = json.loads(row['payload'])
    assert payload['clip']['start'] == clip.start and payload['clip']['end'] == clip.end
    assert payload['project']['clips'][0]['asset_id'] == asset['id']


def test_source_preview_encodes_selected_range_without_upscaling(data):
    from backend.models import Clip
    from backend.worker import process
    asset = source(data)
    clip = Clip(id='source-preview', asset_id=asset['id'], start=.5, end=1.5)
    fingerprint = preview.source_fingerprint(preview.SourcePreviewRequest(clip=clip, resolution=1080))
    job_id = store.new_job('preview-source', {'clip': clip.model_dump(), 'project': {'clips': [clip.model_dump()]},
                                                'resolution': 1080, 'fingerprint': fingerprint})
    result = process({'id': job_id, 'kind': 'preview-source', 'payload': {
        'clip': clip.model_dump(), 'resolution': 1080, 'fingerprint': fingerprint}})
    output = data/'previews'/f'{fingerprint}.mp4'
    metadata = result['metadata']
    assert result['url'] == f'/api/previews/{fingerprint}'
    assert (metadata['width'], metadata['height']) == (320, 180)
    assert metadata['mode'] == 'sdr' and metadata['has_audio']
    assert metadata['source_start'] == .5 and metadata['source_end'] == 1.5
    assert .9 <= media.probe(output)['duration'] <= 1.1
    source_path = data/'assets'/asset['id']/asset['original']
    frames = []
    for path, seek in ((source_path, '0.5'), (output, '0')):
        decoded = subprocess.run([media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-ss', seek,
                                  '-i', str(path), '-frames:v', '1', '-vf', 'scale=320:180',
                                  '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
                                 check=True, capture_output=True, timeout=30)
        frames.append(decoded.stdout)
    import numpy as np
    delta = np.abs(np.frombuffer(frames[0], dtype=np.uint8).astype(np.int16) -
                   np.frombuffer(frames[1], dtype=np.uint8).astype(np.int16))
    assert delta.mean() < 18, 'preview must begin at the requested source time'


def test_source_preview_keeps_landscape_full_hd_at_full_hd(data):
    from backend.models import Clip
    from backend.worker import process
    asset_id = uuid.uuid4().hex
    folder = data/'assets'/asset_id
    folder.mkdir(parents=True)
    original = folder/'source.mkv'
    subprocess.run([media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=1920x1080:rate=5:duration=1',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1',
                    '-pix_fmt', 'yuv420p', str(original)],
                   check=True, capture_output=True, timeout=60)
    asset = {'id': asset_id, 'original': original.name, 'title': 'Full HD test', **media.probe(original)}
    store.put_asset(asset)
    clip = Clip(id='full-hd', asset_id=asset_id, start=0, end=1)
    fingerprint = preview.source_fingerprint(preview.SourcePreviewRequest(clip=clip, resolution=1080))
    job_id = store.new_job('preview-source', {'clip': clip.model_dump(), 'project': {'clips': [clip.model_dump()]},
                                                'resolution': 1080, 'fingerprint': fingerprint})
    result = process({'id': job_id, 'kind': 'preview-source', 'payload': {
        'clip': clip.model_dump(), 'resolution': 1080, 'fingerprint': fingerprint}})
    assert result['metadata']['width'] == 1920
    assert result['metadata']['height'] == 1080


def test_hlg_tonemap_keeps_midgray_and_rolls_white_highlight(data):
    from PIL import Image
    colors = [(0, 0, 0), (32, 32, 32), (64, 64, 64), (128, 128, 128),
              (192, 192, 192), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255)]
    image = Image.new('RGB', (len(colors) * 48, 48))
    for index, color in enumerate(colors):
        for x in range(index * 48, (index + 1) * 48):
            for y in range(48):
                image.putpixel((x, y), color)
    png = data/'patches.png'
    image.save(png)
    hlg = data/'patches-hlg.mkv'
    subprocess.run([media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-loop', '1', '-framerate', '1',
                    '-i', str(png), '-vf', 'zscale=pin=bt709:tin=bt709:min=bt709:p=bt2020:t=arib-std-b67:m=bt2020nc:npl=1000,format=yuv420p10le',
                    '-t', '1', '-c:v', 'libx265', '-x265-params', 'pools=1:frame-threads=1', '-pix_fmt', 'yuv420p10le',
                    '-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67', '-colorspace', 'bt2020nc', str(hlg)],
                   check=True, capture_output=True, timeout=60)
    metadata = media.probe(hlg)
    assert metadata['hdr'] and metadata['transfer'] == 'arib-std-b67'
    raw = data/'decoded.rgb'
    subprocess.run([media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-i', str(hlg), '-frames:v', '1',
                    '-vf', f'{media.to_sdr(metadata)},format=rgb24', '-f', 'rawvideo', str(raw)],
                   check=True, capture_output=True, timeout=60)
    decoded = Image.frombytes('RGB', (image.width, image.height), raw.read_bytes())
    samples = [decoded.getpixel((index * 48 + 24, 24)) for index in range(len(colors))]
    gray = [sum(samples[index]) / 3 for index in range(6)]
    assert gray == sorted(gray)
    assert 110 <= gray[3] <= 150
    assert 180 <= gray[5] < 250
    assert samples[6][0] > samples[6][1] * 2 and samples[6][0] > samples[6][2] * 2
    assert samples[7][1] > samples[7][0] * 2 and samples[7][1] > samples[7][2] * 2
    assert samples[8][2] > samples[8][0] * 2 and samples[8][2] > samples[8][1] * 2
