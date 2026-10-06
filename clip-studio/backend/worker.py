"""One supervisor, one main job and an optional bounded interactive job slot."""
import json
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import ai, media, storage, store

STOP_REQUESTED = False


def _request_shutdown(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def download_run(command, log_path, timeout):
    with log_path.open('w') as log, log_path.with_suffix('.stderr.log').open('w') as errors:
        supervised = os.environ.get('CLIP_JOB_PROCESS_GROUP') == '1'
        process=subprocess.Popen(command,stdout=log,stderr=errors,start_new_session=not supervised)
        deadline=time.monotonic()+timeout
        try:
            while process.poll() is None:
                storage.guard()
                if time.monotonic()>deadline:
                    raise ValueError('YouTube слишком долго отвечает. Повторите позже или загрузите файл')
                time.sleep(.5)
        except Exception:
            try:
                if supervised:
                    # The supervised job owns this process group, including its
                    # downloader/transcoder descendants.
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                else:
                    os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait();raise
    if process.returncode:
        detail=log_path.with_suffix('.stderr.log').read_text(errors='replace')
        if 'HTTP Error 403' in detail:
            raise ValueError('YouTube отклонил скачивание потока (HTTP 403). Повторите импорт позже или загрузите исходный файл вручную')
        raise ValueError('YouTube не отдал видео. Попробуйте позже или загрузите исходный файл вручную')


def youtube_base():
    return [sys.executable,'-m','yt_dlp','--ignore-config','--no-playlist','--no-progress','--js-runtimes','node','--socket-timeout','20','--retries','1']


def inspect_youtube(job):
    folder=store.DATA/'work'/job['id'];folder.mkdir(parents=True,exist_ok=True)
    store.update_job(job['id'],progress=.2,message='Проверка доступных качеств YouTube')
    try:
        download_run([*youtube_base(),'--skip-download','--dump-single-json',youtube_url(job['payload']['url'])],folder/'metadata.json',120)
        info=json.loads((folder/'metadata.json').read_text())
        if info.get('is_live') or not info.get('duration') or info['duration']>14400:
            raise ValueError('Нужно завершённое видео длительностью до четырёх часов')
        qualities={}
        for f in info.get('formats',[]):
            height=f.get('height')
            if not height or f.get('vcodec') in {None,'none'}: continue
            hdr=f.get('dynamic_range') in {'HDR','HDR10','HLG','DV'}
            key=(height,hdr)
            estimate=f.get('filesize') or f.get('filesize_approx')
            qualities[key]={'height':height,'hdr':hdr,'codec':f.get('vcodec'),'estimated_bytes':estimate}
        return {'title':info.get('title','Видео YouTube'),'duration':info['duration'],'qualities':sorted(qualities.values(),key=lambda f:(f['height'],f['hdr']),reverse=True)}
    finally:
        shutil.rmtree(folder,ignore_errors=True)


def youtube_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.fragment:
        raise ValueError("Вставьте HTTPS-ссылку на отдельное видео YouTube")
    if parsed.hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        video_id = parse_qs(parsed.query).get("v", [""])[0] if parsed.path == "/watch" else parsed.path.split("/")[-1] if parsed.path.startswith(("/shorts/", "/live/")) else ""
    elif parsed.hostname == "youtu.be":
        video_id = parsed.path.strip("/")
    else:
        raise ValueError("Поддерживаются только youtube.com и youtu.be")
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Не удалось найти ID видео в ссылке")
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_ffmpeg():
    return shutil.which(media.FFMPEG) or media.FFMPEG


def validate_download_size(info, max_bytes):
    formats=info.get('requested_formats') or [info]
    sizes=[f.get('filesize') or f.get('filesize_approx') for f in formats]
    # Known video bytes alone can exceed the budget even without an audio estimate.
    estimate=sum(size or 0 for size in sizes)
    if estimate > max_bytes:
        height=info.get('height') or max((f.get('height') or 0 for f in formats),default=0)
        raise ValueError(f'Полный исходник {height}p занимает примерно {estimate/1048576:.0f} МБ, лимит — {max_bytes/1048576:.0f} МБ. Выберите меньшее качество или загрузите короткий исходный фрагмент')


def import_youtube(job):
    url = youtube_url(job["payload"]["url"])
    asset_id = job["payload"]["asset_id"]
    folder = store.DATA / "assets" / asset_id
    folder.mkdir(parents=True, exist_ok=True)
    store.update_job(job["id"], progress=0.1, message="Загрузка оригинала с YouTube")
    max_bytes = int(os.environ.get("CLIP_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
    base = [*youtube_base(), "--ffmpeg-location", youtube_ffmpeg(), "-o", str(folder / "source.%(ext)s")]
    height=job['payload'].get('max_height',1080)
    if job['payload'].get('reuse_original'):
        cached_info=json.loads((folder/'source.info.json').read_text())
        cached=folder/'source.mkv'
        if cached_info.get('id')!=parse_qs(urlparse(url).query)['v'][0] or not cached.exists() or media.probe(cached)['height']!=height:
            raise ValueError('Сохранённый исходник не соответствует запросу')
    else:
        selector=f"bv*[height<={height}]+ba/b[height<={height}]"
        sort='hdr,res' if job['payload'].get('prefer_hdr') else 'res,hdr:sdr'
        metadata=folder/'selection.json'
        download_run([*youtube_base(),'--skip-download','--dump-single-json','-f',selector,'-S',sort,url],metadata,120)
        try:
            selection=json.loads(metadata.read_text())
            validate_download_size(selection,max_bytes)
        finally:
            metadata.unlink(missing_ok=True)
        command = [*base, "--max-filesize", str(max_bytes), "--match-filter", "duration <= 14400 & !is_live", "-f", selector, "-S", sort, "--merge-output-format", "mkv", "--remux-video", "mkv", "--write-info-json", url]
        for attempt in range(2):
            try:
                download_run(command,folder/'download.log',7200)
                break
            except ValueError:
                detail=(folder/'download.stderr.log').read_text(errors='replace')
                if attempt or 'HTTP Error 403' not in detail:
                    raise
                store.update_job(job['id'],message='YouTube отклонил поток; обновляем ссылку и повторяем загрузку')
                time.sleep(3)
    originals = [p for p in folder.glob("source.*") if p.suffix in {".mkv", ".mp4", ".webm", ".mov"}]
    if not originals:
        raise ValueError("YouTube не отдал видео. Возможны ограничения доступа, проверка входа или изменение загрузчика. Загрузите исходный файл вручную; детали сохранены в download.log на сервере.")
    original = originals[0]
    if original.stat().st_size > max_bytes:
        raise ValueError("Исходник превышает лимит размера")
    info_path = folder / "source.info.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    store.update_job(job["id"], progress=0.4, message="Создание лёгкого предпросмотра")
    preview_source=None
    if media.probe(original)['hdr'] and info.get('duration',0)>300:
        store.update_job(job['id'],message='Загрузка лёгкого SDR-предпросмотра; оригинал 1080p HDR сохранён')
        download_run([*base,'-o',str(folder/'preview-source.%(ext)s'),'-f','bv[height<=240][dynamic_range=SDR]+ba/b[height<=240]','-S','res,hdr:sdr','--max-filesize',str(max_bytes),'--merge-output-format','mkv',url],folder/'preview-download.log',1800)
        preview_source=next((p for p in folder.glob('preview-source.*') if p.suffix in {'.mkv','.mp4','.webm'}),None)
        if not preview_source:
            raise ValueError('Не удалось скачать лёгкий предпросмотр; оригинал сохранён')
    asset = media.prepare_asset(asset_id, original, info.get("title", "Видео YouTube"), "youtube", url,preview_source=preview_source)
    if preview_source:
        preview_source.unlink(missing_ok=True)
    # Optional subtitles must not make a successfully downloaded video unusable.
    try:
        with (folder / "subtitles.log").open("w") as log:
            subprocess.run([*base, "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "ru,en", "--sub-format", "vtt", url], stdout=log, stderr=log, timeout=180)
    except subprocess.TimeoutExpired:
        pass
    # Metadata includes signed provider URLs; retain only the selected source file.
    info_path.unlink(missing_ok=True)
    ai.queue_auto(asset_id,requested=job['payload'].get('analyze',True),parent_job_id=job['id'])
    return {"asset_id": asset["id"]}


def process(job):
    if job['kind']=='youtube-info':
        return inspect_youtube(job)
    if job["kind"] == "youtube":
        return import_youtube(job)
    if job["kind"] == "prepare":
        asset_id = job["payload"]["asset_id"]
        source = store.DATA / "assets" / asset_id / "original"
        store.update_job(job["id"], progress=0.2, message="Проверка видео и создание предпросмотра")
        asset = media.prepare_asset(asset_id, source, job["payload"]["title"])
        ai.queue_auto(asset_id, parent_job_id=job['id'])
        return {"asset_id": asset["id"]}
    if job['kind'] == 'prepare-playback':
        asset_id = job['payload']['asset_id']
        work = store.DATA/'work'/job['id']
        source_asset = store.asset(asset_id)
        if not source_asset:
            raise ValueError('Исходное видео не найдено')
        duration = max(.001, float(source_asset['duration']))
        store.update_job(job['id'], progress=.05, message='Создание полноразмерного предпросмотра')
        def report_proxy_progress(seconds):
            seconds = min(duration, max(0.0, float(seconds)))
            def stamp(value):
                value = int(value)
                hours, remainder = divmod(value, 3600)
                minutes, secs = divmod(remainder, 60)
                return f'{hours:02d}:{minutes:02d}:{secs:02d}' if hours else f'{minutes:02d}:{secs:02d}'
            store.update_job(job['id'], progress=.05 + .87 * seconds / duration,
                             message=f'SDR предпросмотр · обработано {stamp(seconds)} из {stamp(duration)}')
        result = media.upgrade_playback_proxy(asset_id, work, progress=report_proxy_progress)
        store.update_job(job['id'], progress=.95, message='Проверка предпросмотра')
        shutil.rmtree(work, ignore_errors=True)
        return result
    if job["kind"] == "export":
        return media.export_video(job)
    if job["kind"] == "preview-frame":
        from .models import ExportRequest
        request = ExportRequest.model_validate(job['payload']['request'])
        cache_id = job['payload']['fingerprint']
        folder = store.DATA/'previews'
        folder.mkdir(parents=True, exist_ok=True)
        output = folder/f'{cache_id}.png'
        work = store.DATA/'work'/job['id']
        work.mkdir(parents=True, exist_ok=True)
        partial = work/'frame.png'
        store.update_job(job['id'], message='Точный SDR-кадр из оригинала', progress=.1)
        storage.guard_preview_cache(12*1024*1024)
        metadata = media.render_frame(request, job['payload']['time'], partial, work)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise ValueError('Не удалось создать кадр предпросмотра')
        partial.replace(output)
        storage.guard()
        storage.guard_preview_cache(preserve=output)
        shutil.rmtree(work, ignore_errors=True)
        return {'cache_id': cache_id, 'url': f'/api/previews/{cache_id}', 'metadata': metadata}
    if job["kind"] == "preview-video":
        from .models import ExportRequest
        request = ExportRequest.model_validate(job['payload']['request'])
        # export_video produces the exact export bitstream; move it out of the
        # export namespace so playback previews never appear as downloadable exports.
        result = media.export_video(job)
        source = store.DATA/'exports'/f"{job['id']}.mp4"
        target = store.DATA/'previews'/f"{job['payload']['fingerprint']}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        try:
            storage.guard()
            storage.guard_preview_cache(preserve=target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {'cache_id': job['payload']['fingerprint'], 'url': f"/api/previews/{job['payload']['fingerprint']}",
                'mode': request.mode, 'metadata': result.get('metadata')}
    if job['kind'] == 'preview-source':
        from .models import Clip
        clip = Clip.model_validate(job['payload']['clip'])
        cache_id = job['payload']['fingerprint']
        folder = store.DATA/'previews'
        folder.mkdir(parents=True, exist_ok=True)
        target = folder/f'{cache_id}.mp4'
        work = store.DATA/'work'/job['id']
        work.mkdir(parents=True, exist_ok=True)
        partial = work/'source-preview.mp4'
        store.update_job(job['id'], message='Создание SDR-копии выбранного фрагмента', progress=.1)
        metadata = media.render_source_preview(clip, job['payload']['resolution'], partial, work)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise ValueError('Не удалось создать SDR-предпросмотр исходника')
        storage.guard_preview_cache(incoming_bytes=partial.stat().st_size)
        partial.replace(target)
        try:
            storage.guard()
            storage.guard_preview_cache(preserve=target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        shutil.rmtree(work, ignore_errors=True)
        return {'cache_id': cache_id, 'url': f'/api/previews/{cache_id}', 'metadata': metadata}
    if job["kind"] == "reframe":
        from . import autoframe
        if job['payload'].get('mode') == 'speaker':
            from . import speaker
            return speaker.suggest(job['payload']['clip'], job['payload'].get('style'), job['id'])
        return autoframe.suggest(job['payload']['clip'], job['payload'].get('style'), job['id'])
    if job["kind"] == "analyze":
        return ai.analyze(job)
    raise ValueError("Неизвестная задача")


def _record_job_failure(job, exc):
    state = store.job_status(job['id'])
    if not state or state['status'] in {'cancelling', 'cancelled'}:
        return
    message = str(exc) if isinstance(exc, ValueError) else "Ошибка обработки. Проверьте журнал задачи на сервере; попробуйте другой файл или SDR-экспорт."
    if not store.update_job(job['id'], status='error', message=message[:500]):
        return
    print(f"Job {job['id']} failed: {type(exc).__name__}", flush=True)
    error_folder = store.DATA/'work'/job['id']
    error_folder.mkdir(parents=True, exist_ok=True)
    (error_folder/'error.txt').write_text(str(exc), encoding='utf-8')
    if job['kind'] == 'export':
        (store.DATA/'exports'/f"{job['id']}.mp4").unlink(missing_ok=True)
        for partial in error_folder.iterdir():
            if partial.is_file() and partial.suffix not in {'.log','.txt'}:
                partial.unlink(missing_ok=True)
    if job['kind'] in {'preview-frame','preview-video','preview-source','prepare-playback'}:
        cache_id = job['payload'].get('fingerprint')
        if cache_id:
            (store.DATA/'previews'/f'{cache_id}.png').unlink(missing_ok=True)
            (store.DATA/'previews'/f'{cache_id}.mp4').unlink(missing_ok=True)
        (store.DATA/'exports'/f"{job['id']}.mp4").unlink(missing_ok=True)
        for partial in error_folder.rglob('*'):
            if partial.is_file() and partial.suffix.lower() in {'.png','.mp4','.mkv','.webm','.mov'}:
                partial.unlink(missing_ok=True)
    if job['kind'] in {'prepare','youtube'}:
        asset_id = job['payload']['asset_id']
        if not store.asset(asset_id) and not job['payload'].get('reuse_original'):
            (error_folder/'source-id.txt').write_text(asset_id, encoding='utf-8')
            for log_file in (store.DATA/'assets'/asset_id).glob('*.log'):
                shutil.copy(log_file, error_folder/log_file.name)
            shutil.rmtree(store.DATA/'assets'/asset_id, ignore_errors=True)


def _cleanup_cancelled_job(job):
    # Remove scratch data but keep source assets, projects, caches, and completed exports.
    shutil.rmtree(store.DATA/'work'/job['id'], ignore_errors=True)
    if job['kind'] == 'export':
        output = store.DATA/'exports'/f"{job['id']}.mp4"
        if output.exists():
            try:
                metadata = media.probe(output)
                request = job['payload'].get('project', {})
                expected = sum(float(clip['end'])-float(clip['start']) for clip in request.get('clips', []))
                if expected <= 0 or abs(metadata['duration']-expected) > .2:
                    output.unlink(missing_ok=True)
            except Exception:
                output.unlink(missing_ok=True)
    elif job['kind'] == 'preview-video':
        (store.DATA/'exports'/f"{job['id']}.mp4").unlink(missing_ok=True)
    if job['kind'] in {'prepare','youtube'}:
        folder = store.DATA/'assets'/job['payload']['asset_id']
        if folder.exists():
            for path in folder.rglob('*'):
                if path.is_file() and (path.name.endswith('.part') or path.suffix in {'.ytdl','.pending'}):
                    path.unlink(missing_ok=True)
            for path in folder.glob('*.info.json'):
                path.unlink(missing_ok=True)
            (folder/'selection.json').unlink(missing_ok=True)


def _process_start_token(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        try:
            result = subprocess.run(['ps','-o','lstart=','-p',str(pid)], capture_output=True, text=True, timeout=2)
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None


def _write_job_pid(path, pid):
    temporary = path.with_name(f'{path.name}.{os.getpid()}.pending')
    temporary.write_text(json.dumps({'pid':pid, 'start':_process_start_token(pid)}))
    temporary.replace(path)


def _terminate_job_group(pid, grace=1.5, leader_reaped=False):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic()+grace
    reaped = leader_reaped
    while time.monotonic() < deadline:
        if not reaped:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                reaped = waited == pid
            except ChildProcessError:
                reaped = True
        alive = _job_group_has_live_processes(pid)
        if reaped and not alive:
            return True
        time.sleep(.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return False
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        if not reaped:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
                reaped = waited == pid
            except ChildProcessError:
                reaped = True
        if reaped and not _job_group_has_live_processes(pid, after_kill=True):
            return True
        time.sleep(.05)
    return False


def _job_group_has_live_processes(pgid, after_kill=False):
    proc = Path('/proc')
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == pgid and fields[0] != 'Z':
                    return True
            except (OSError, IndexError, ValueError):
                continue
        return False
    try:
        result = subprocess.run(['ps','-axo','pgid=,stat='], capture_output=True, text=True, timeout=2)
        return any(int(line.split()[0]) == pgid and 'Z' not in line.split()[1]
                   for line in result.stdout.splitlines() if len(line.split()) >= 2)
    except PermissionError:
        # Some macOS sandbox profiles deny process-group existence probes. We
        # still sent SIGKILL above; Linux deployments use /proc for confirmation.
        return not after_kill
    except (OSError, ValueError, subprocess.TimeoutExpired):
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return not after_kill


def _job_lifecycle(job, lock_fd):
    pid_path = store.DATA/'work'/job['id']/'job.pid.json'
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid = os.fork()
    except Exception as exc:
        _record_job_failure(job, exc)
        return
    if pid == 0:
        try:
            os.close(lock_fd)
            os.setpgid(0, 0)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.environ['CLIP_JOB_PROCESS_GROUP'] = '1'
            _write_job_pid(pid_path, os.getpid())
            try:
                storage.guard()
                result = process(job)
                store.update_job(job['id'], status='ready', progress=1, message='Готово', result=result)
            except Exception as exc:
                _record_job_failure(job, exc)
        finally:
            os._exit(0)
    try:
        try:
            os.setpgid(pid, pid)
        except (PermissionError, ProcessLookupError):
            pass  # The child already established its job-only process group or exited.
        _write_job_pid(pid_path, pid)
    except Exception as exc:
        # Do not release the serial worker slot until the forked job and all
        # descendants are gone, even when writing the recovery marker failed.
        while not _terminate_job_group(pid):
            time.sleep(.2)
        state = store.job_status(job['id'])
        if state and state['status'] == 'cancelling':
            store.update_job(job['id'], status='cancelled', message='Отменено пользователем')
            _cleanup_cancelled_job(job)
        else:
            _record_job_failure(job, exc)
        return
    try:
        while True:
            state = store.job_status(job['id'])
            if STOP_REQUESTED and state and state['status'] == 'running':
                store.request_cancel(job['id'])
                state = store.job_status(job['id'])
            if state and state['status'] == 'cancelling':
                if not _terminate_job_group(pid):
                    time.sleep(.2)
                    continue
                store.update_job(job['id'], status='cancelled', message='Отменено пользователем')
                _cleanup_cancelled_job(job)
                break
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited = pid
            if waited == pid:
                # The leader can exit while one of its tool descendants remains.
                # Always reap the whole group before releasing the asset lock.
                while not _terminate_job_group(pid, grace=.25, leader_reaped=True):
                    time.sleep(.2)
                state = store.job_status(job['id'])
                if state and state['status'] == 'cancelling':
                    store.update_job(job['id'], status='cancelled', message='Отменено пользователем')
                    _cleanup_cancelled_job(job)
                elif state and state['status'] == 'running':
                    _record_job_failure(job, RuntimeError('Worker job process exited unexpectedly'))
                break
            yield
    finally:
        pid_path.unlink(missing_ok=True)


def _execute_claimed_job(job, lock_fd):
    """Serial entry point used by checks and callers processing a single job."""
    for _ in _job_lifecycle(job, lock_fd):
        time.sleep(.1)


def parallel_quick_jobs():
    return os.environ.get('CLIP_PARALLEL_QUICK_JOBS', '1' if media.runtime_profile() == 'desktop' else '0') == '1'


def _recover_stale_jobs():
    with store.connect() as conn:
        rows = conn.execute("SELECT id,status,kind,payload FROM jobs WHERE status IN ('running','cancelling')").fetchall()
    for row in rows:
        pid_path = store.DATA/'work'/row['id']/'job.pid.json'
        try:
            identity = json.loads(pid_path.read_text())
            pid = int(identity['pid'])
            leader_matches = bool(identity.get('start') and _process_start_token(pid) == identity['start'])
            group_live = _job_group_has_live_processes(pid)
            if leader_matches:
                while not _terminate_job_group(pid, leader_reaped=True):
                    time.sleep(.2)
            elif group_live:
                raise RuntimeError('Stale worker group identity cannot be verified; refusing to start another worker')
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError('Stale worker process identity is missing; refusing to release its active job') from exc
        if row['status'] == 'cancelling':
            store.update_job(row['id'], status='cancelled', message='Отменено при остановке сервера')
            _cleanup_cancelled_job({'id':row['id'],'kind':row['kind'],'payload':json.loads(row['payload'])})
        else:
            store.update_job(row['id'], status='error', message='Обработка прервана перезапуском сервера. Запустите задачу заново.')
    return False


def main():
    global STOP_REQUESTED
    store.init()
    lock = (store.DATA / 'worker.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('A worker is already processing this data directory')
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    _recover_stale_jobs()
    parallel = parallel_quick_jobs()
    print(f"Clip Studio: worker ready ({'main + quick' if parallel else 'serial'})", flush=True)
    last_cleanup=0
    active = {}
    lanes = ('main', 'quick') if parallel else (None,)
    while not STOP_REQUESTED or active:
        for lane in lanes:
            if lane not in active and not STOP_REQUESTED:
                job = store.claim_job(lane)
                if job:
                    active[lane] = _job_lifecycle(job, lock.fileno())
            lifecycle = active.get(lane)
            if lifecycle is not None:
                try:
                    next(lifecycle)
                except StopIteration:
                    del active[lane]
        if not active and not STOP_REQUESTED:
            if time.monotonic()-last_cleanup>3600:
                storage.cleanup();last_cleanup=time.monotonic()
        time.sleep(.1 if active else .25)
    lock.close()


if __name__ == "__main__":
    main()
