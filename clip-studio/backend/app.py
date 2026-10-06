import base64
import binascii
import hashlib
import hmac
import os
import json
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal
from fastapi import Query
from starlette.concurrency import run_in_threadpool

from . import ai, composition, media, preview, storage, store
from .models import AISettings, AnalysisRequest, ExportRequest, Project, YoutubeRequest
from .worker import youtube_url


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("CLIP_REQUIRE_AUTH") == "1" and (not os.environ.get("CLIP_USERNAME") or len(os.environ.get("CLIP_PASSWORD", "")) < 16):
        raise RuntimeError("Set CLIP_USERNAME and a CLIP_PASSWORD with at least 16 characters before serving publicly")
    store.init()
    yield


app = FastAPI(title="Clip Studio", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    expected_password = os.environ.get("CLIP_PASSWORD")
    if expected_password:
        valid = False
        try:
            scheme, value = request.headers.get("authorization", "").split(" ", 1)
            username, password = base64.b64decode(value, validate=True).decode().split(":", 1)
            valid = scheme.lower() == "basic" and hmac.compare_digest(username.encode(), os.environ.get("CLIP_USERNAME", "creator").encode()) and hmac.compare_digest(password.encode(), expected_password.encode())
        except (ValueError, UnicodeDecodeError, binascii.Error):
            pass
        if not valid:
            return JSONResponse({"detail": "Нужен вход"}, 401, headers={"WWW-Authenticate": 'Basic realm="Clip Studio", charset="UTF-8"'})
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin:
            origin_host = urlparse(origin).netloc
            allowed = {request.headers.get("host")}
            if os.environ.get('CLIP_REQUIRE_AUTH') != '1':
                allowed.add(os.environ.get("CLIP_DEV_ORIGIN", "http://127.0.0.1:5173").split("://")[-1])
            if origin_host not in allowed:
                return JSONResponse({"detail": "Запрос с другого сайта отклонён"}, 403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


def check_queue():
    with store.connect() as conn:
        count = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running', 'cancelling')").fetchone()[0]
    if count >= 10:
        raise HTTPException(429, "В очереди уже 10 задач. Дождитесь завершения обработки")


def get_asset(asset_id):
    asset = store.asset(asset_id)
    if not asset:
        raise HTTPException(404, "Видео не найдено")
    return asset


@app.get("/api/health")
def health():
    desktop = media.runtime_profile() == 'desktop'
    return {"ok": True, "ffmpeg": bool(shutil.which(media.FFMPEG)), "ffprobe": bool(shutil.which(media.FFPROBE)),
            "auth": bool(os.environ.get("CLIP_PASSWORD")), "runtime": 'desktop' if desktop else 'server',
            "renderer": 'videotoolbox' if desktop else 'software',
            "preview": {'original': 'byte-range', 'proxy': 'seekable', 'source': 'fhd-on-demand'},
            "parallel_quick_jobs": os.environ.get('CLIP_PARALLEL_QUICK_JOBS', '1' if desktop else '0') == '1'}


@app.get("/api/assets")
def list_assets():
    output = []
    for asset in store.assets():
        proxy = store.DATA/'assets'/asset['id']/'proxy.mp4'
        try:
            stat = proxy.stat()
            revision = f'{stat.st_mtime_ns}-{stat.st_size}'
        except OSError:
            revision = ''
        output.append({**asset, **media.playback_capabilities(asset), 'preview_revision': revision})
    return output


@app.get("/api/storage")
def storage_status():
    return storage.status()


@app.get('/api/assets/{asset_id}/audio/waveform')
def audio_waveform(asset_id: str, start: float = Query(0, ge=0), end: float | None = None,
                   bins: int = Query(1200, ge=50, le=4000)):
    from . import audio
    asset = get_asset(asset_id)
    try:
        return audio.envelope(asset, start, asset['duration'] if end is None else end, bins)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post('/api/assets/{asset_id}/playback/prepare', status_code=202)
def prepare_asset_playback(asset_id: str):
    if media.runtime_profile() != 'desktop':
        raise HTTPException(409, 'Полноразмерный предпросмотр доступен в локальном режиме Clip Studio')
    asset = get_asset(asset_id)
    target_short = min(1080, int(asset['width']), int(asset['height']))
    proxy = store.DATA/'assets'/asset_id/'proxy.mp4'
    if proxy.is_file() and media.proxy_short_edge(asset) >= target_short:
        return {'ready': True, 'metadata': {'width': asset.get('preview_width'),
                'height': asset['preview_height'], 'mode': 'sdr', 'preview_height': asset['preview_height']}}
    source = store.DATA/'assets'/asset_id/asset['original']
    try:
        stat = source.stat()
        fingerprint = hashlib.sha256(json.dumps([asset_id, stat.st_size, stat.st_mtime_ns,
                                                  target_short, 'desktop-proxy-v1']).encode()).hexdigest()
        payload = {'asset_id': asset_id, 'fingerprint': fingerprint}
        existing = storage.active_fingerprint_job('prepare-playback', fingerprint)
        if existing:
            return {'job_id': existing}
        target_width, target_height = media.proxy_dimensions(asset['width'], asset['height'], target_short)
        encoder = media.video_encoder_args('sdr', target_width, target_height, proxy=True)
        bitrate = int(encoder[encoder.index('-b:v')+1])
        estimated = int(float(asset['duration']) * (bitrate + 160_000) / 8 * 1.15) + 16*1024*1024
        try:
            storage.guard(estimated)
        except ValueError as exc:
            raise HTTPException(507, 'Недостаточно свободного места для полного предпросмотра. Можно продолжить с выбранным фрагментом через предпросмотр исходника.') from exc
        job_id = storage.enqueue_fingerprinted('prepare-playback', payload, fingerprint)
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'job_id': job_id}


@app.delete("/api/assets/{asset_id}")
def delete_asset(asset_id: str):
    get_asset(asset_id)
    try:
        storage.delete_asset(asset_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.delete("/api/exports/{job_id}")
def delete_export(job_id: str):
    try:
        uuid.UUID(job_id)
        storage.delete_export(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.get("/api/fonts/{name}")
def font_file(name: str):
    if name not in composition.FONTS:
        raise HTTPException(404)
    return FileResponse(composition.FONT_DIR/f'{name}.ttf', media_type='font/ttf')


@app.post("/api/graphics", status_code=201)
async def upload_graphic(file: UploadFile = File(...)):
    import io
    from PIL import Image, ImageOps, UnidentifiedImageError
    content = await file.read(10*1048576+1)
    await file.close()
    if len(content)>10*1048576:
        raise HTTPException(413,'Изображение должно быть до 10 МБ')
    try:
        storage.guard(len(content))
        image=Image.open(io.BytesIO(content))
        if image.format not in {'JPEG','PNG','WEBP'} or image.width*image.height>20000000:
            raise ValueError('Нужен JPEG, PNG или WebP до 20 мегапикселей')
        image=ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((2048,2048))
    except (ValueError,UnidentifiedImageError,OSError,Image.DecompressionBombError) as exc:
        raise HTTPException(400,'Не удалось прочитать изображение или достигнут лимит хранилища') from exc
    graphic_id=uuid.uuid4().hex
    image.save(store.DATA/'graphics'/f'{graphic_id}.webp',quality=88)
    return {'id':graphic_id,'width':image.width,'height':image.height}


@app.get("/api/graphics/{graphic_id}")
def graphic_file(graphic_id: str):
    try:
        uuid.UUID(graphic_id)
    except ValueError:
        raise HTTPException(404)
    path=store.DATA/'graphics'/f'{graphic_id}.webp'
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path,media_type='image/webp')


@app.get("/api/ai/settings")
def ai_settings():
    return ai.public_settings()


@app.put("/api/ai/settings")
def save_ai_settings(body: AISettings):
    return ai.save_settings(body)


@app.post("/api/assets/{asset_id}/analyze", status_code=202)
def analyze_video(asset_id: str, body: AnalysisRequest):
    asset = get_asset(asset_id)
    check_queue()
    if body.end > asset["duration"]:
        raise HTTPException(400, "Участок анализа выходит за границы видео")
    try:
        ai.check_provider(body.provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": store.new_job("analyze", {"asset_id": asset_id, **body.model_dump()})}


@app.get("/api/assets/{asset_id}/analysis/result")
def recommendations(asset_id: str):
    get_asset(asset_id)
    path = store.DATA / "assets" / asset_id / "recommendations.json"
    if not path.exists():
        return None
    import json
    return json.loads(path.read_text())


@app.post("/api/assets/upload", status_code=202)
async def upload_video(file: UploadFile = File(...)):
    check_queue()
    asset_id = uuid.uuid4().hex
    folder = store.DATA / "assets" / asset_id
    folder.mkdir(parents=True)
    max_bytes = int(os.environ.get("CLIP_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
    size = 0
    try:
        with (folder / "original").open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f"Максимальный размер файла — {max_bytes // 1048576} МБ")
                try:
                    storage.guard(len(chunk))
                except ValueError as exc:
                    raise HTTPException(507, str(exc)) from exc
                await run_in_threadpool(output.write, chunk)
        if not size:
            raise HTTPException(400, "Файл пустой")
        job_id = store.new_job("prepare", {"asset_id": asset_id, "title": Path(file.filename or "Видео").stem[:150]})
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()
    return {"job_id": job_id, "asset_id": asset_id}


@app.post("/api/assets/youtube", status_code=202)
def import_youtube(body: YoutubeRequest):
    check_queue()
    try:
        url = youtube_url(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    asset_id = uuid.uuid4().hex
    return {"job_id": store.new_job("youtube", {**body.model_dump(),"url": url, "asset_id": asset_id}), "asset_id": asset_id}


@app.post('/api/youtube/inspect',status_code=202)
def youtube_inspect(body: YoutubeRequest):
    check_queue()
    try:
        url=youtube_url(body.url)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    return {'job_id':store.new_job('youtube-info',{'url':url})}


@app.post('/api/jobs/{job_id}/dismiss')
def dismiss_job(job_id: str):
    try: uuid.UUID(job_id)
    except ValueError: raise HTTPException(400,'Некорректная задача') from None
    with store.connect() as conn:
        row=conn.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:raise HTTPException(404,'Задача не найдена')
        if row['status'] not in {'error','dismissed','cancelled'}:
            raise HTTPException(409,'Можно закрыть только завершённое уведомление об ошибке')
        conn.execute("UPDATE jobs SET status='dismissed' WHERE id=?",(job_id,))
    return {'ok':True}


@app.get("/api/assets/{asset_id}/{variant}")
def asset_file(asset_id: str, variant: str):
    asset = get_asset(asset_id)
    filenames = {"proxy": "proxy.mp4", "thumbnail": "thumbnail.jpg", "original": asset["original"]}
    if variant not in filenames:
        raise HTTPException(404)
    path = store.DATA / "assets" / asset_id / filenames[variant]
    if not path.is_file():
        raise HTTPException(404)
    mime = "image/jpeg" if variant == "thumbnail" else "video/mp4" if variant == "proxy" else media.playback_capabilities(asset)['original_mime']
    return FileResponse(path, media_type=mime)


@app.get("/api/assets/{asset_id}/transcript/cues")
def transcript(asset_id: str):
    get_asset(asset_id)
    return media.read_cues(media.subtitle_path(asset_id))


@app.post("/api/assets/{asset_id}/transcript", status_code=201)
async def upload_transcript(asset_id: str, file: UploadFile = File(...)):
    get_asset(asset_id)
    content = await file.read(2 * 1024 * 1024 + 1)
    await file.close()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(413, "Субтитры должны быть меньше 2 МБ")
    if Path(file.filename or "").suffix.lower() not in {".srt", ".vtt"}:
        raise HTTPException(400, "Нужен файл SRT или VTT")
    target = store.DATA / "assets" / asset_id / "captions.srt"
    temporary = target.with_name("captions.pending")
    temporary.write_bytes(content)
    cues = media.read_cues(temporary)
    if not cues:
        temporary.unlink(missing_ok=True)
        raise HTTPException(400, "В файле не найдены субтитры с временными метками")
    temporary.replace(target)
    return {"cues": len(cues)}


@app.get("/api/projects")
def projects():
    return store.project_list()


@app.put("/api/projects/{project_id}")
def save_project(project_id: str, project: Project):
    try:
        uuid.UUID(project_id)
    except ValueError as exc:
        raise HTTPException(400, "Некорректный ID проекта") from exc
    store.save_project(project_id, project.model_dump())
    return {"id": project_id}


@app.get("/api/jobs")
def list_jobs():
    return store.jobs()


@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(404, 'Задача не найдена') from None
    status = store.request_cancel(job_id)
    if status is None:
        raise HTTPException(404, 'Задача не найдена')
    messages = {'cancelled': 'Задача отменена', 'cancelling': 'Останавливаем обработку',
                'ready': 'Задача уже завершена', 'error': 'Задача завершилась с ошибкой',
                'dismissed': 'Задача уже закрыта'}
    return {'job_id': job_id, 'status': status, 'message': messages.get(status, 'Состояние задачи обновлено')}


@app.post('/api/jobs/cancel-all')
def cancel_all_jobs():
    result = store.cancel_all_active()
    return {**result, 'message': 'Запрос на отмену активных задач принят'}


@app.get('/api/jobs/{job_id}')
def job_status(job_id: str):
    try:
        uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(404, 'Задача не найдена') from None
    with store.connect() as conn:
        row = conn.execute('SELECT id,kind,status,progress,message,result,created,updated FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Задача не найдена')
    import json
    return {**dict(row), 'result': json.loads(row['result']) if row['result'] else None}


@app.post("/api/exports", status_code=202)
def export(request: ExportRequest):
    check_queue()
    try:
        media.validate_export(request)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": store.new_job("export", request.model_dump())}


@app.post('/api/preview/frame', status_code=202)
def preview_frame(request: preview.PreviewFrameRequest):
    try:
        preview.validate(request)
        fingerprint = preview.fingerprint(request)
        payload = {'request': {'project': request.project.model_dump(), 'mode': 'sdr', 'resolution': request.resolution},
                   'time': request.time, 'fingerprint': fingerprint}
        job_id = storage.enqueue_fingerprinted('preview-frame', payload, fingerprint, supersede=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'job_id': job_id}


@app.post('/api/preview/video', status_code=202)
def preview_video(request: ExportRequest):
    try:
        media.validate_export(request)
        fingerprint = preview.fingerprint(request)
        payload = {'request': request.model_dump(), 'project': request.project.model_dump(),
                   'mode': request.mode, 'resolution': request.resolution, 'fingerprint': fingerprint}
        job_id = storage.enqueue_fingerprinted('preview-video', payload, fingerprint, supersede=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'job_id': job_id}


@app.post('/api/preview/source', status_code=202)
def preview_source(request: preview.SourcePreviewRequest):
    try:
        preview.validate_source(request)
        fingerprint = preview.source_fingerprint(request)
        clip = request.clip.model_dump()
        payload = {'clip': clip, 'project': {'clips': [clip]},
                   'resolution': request.resolution, 'fingerprint': fingerprint}
        job_id = storage.enqueue_fingerprinted('preview-source', payload, fingerprint, supersede=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'job_id': job_id}


@app.get('/api/previews/{cache_id}')
def preview_file(cache_id: str):
    import re
    if not re.fullmatch(r'[a-f0-9]{64}', cache_id):
        raise HTTPException(404, 'Предпросмотр не найден')
    for suffix, mime in (('.png', 'image/png'), ('.mp4', 'video/mp4')):
        path = store.DATA/'previews'/f'{cache_id}{suffix}'
        if path.is_file():
            try:
                if path.stat().st_mtime < time.time() - storage.PREVIEW_TTL:
                    path.unlink(missing_ok=True)
                    break
                os.utime(path, None)
            except FileNotFoundError:
                continue
            return FileResponse(path, media_type=mime)
    raise HTTPException(404, 'Предпросмотр ещё не готов или срок хранения истёк')


class ReframeSuggestionRequest(BaseModel):
    clip: dict
    style: dict = {}
    mode: Literal['scene', 'speaker'] = 'scene'


@app.post('/api/reframe/suggest', status_code=202)
def reframe_suggest(body: ReframeSuggestionRequest):
    from .models import Clip, Style
    try:
        clip = Clip.model_validate(body.clip)
        style = Style.model_validate(body.style)
    except Exception as exc:
        raise HTTPException(400, 'Проверьте фрагмент и параметры кадрирования') from exc
    if not store.asset(clip.asset_id):
        raise HTTPException(404, 'Исходное видео не найдено')
    if clip.end > store.asset(clip.asset_id)['duration'] + .05:
        raise HTTPException(400, 'Фрагмент выходит за длительность видео')
    if body.mode == 'speaker' and not store.asset(clip.asset_id).get('has_audio'):
        raise HTTPException(400, 'Для переключения на говорящего нужна звуковая дорожка')
    if body.mode == 'speaker' and style.fit != 'cover':
        raise HTTPException(400, 'Для переключения камеры выберите кадрирование «Заполнить»')
    check_queue()
    try:
        job_id = store.new_job('reframe', {'clip': clip.model_dump(), 'style': style.model_dump(), 'mode': body.mode})
    except Exception as exc:
        raise HTTPException(500, 'Не удалось поставить задачу кадрирования в очередь') from exc
    return {'job_id': job_id}


@app.get("/api/exports/{job_id}")
def download(job_id: str):
    try:
        uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(404) from exc
    path = store.DATA / "exports" / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(404, "Файл ещё не готов")
    return FileResponse(path, media_type="video/mp4", filename="clip-studio.mp4")


DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="studio")
