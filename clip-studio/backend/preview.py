"""Validation and fingerprint helpers for exact SDR frame previews."""
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from .models import Clip, ExportRequest, Project
from . import media, store


PREVIEW_RENDERER_VERSION = 3
SDR_TONEMAP_VERSION = media.SDR_TONEMAP_VERSION


class PreviewFrameRequest(BaseModel):
    project: Project
    mode: Literal['sdr', 'hdr'] = 'sdr'
    resolution: Literal[720, 1080] = 1080
    time: float = Field(ge=0, allow_inf_nan=False)


class SourcePreviewRequest(BaseModel):
    clip: Clip
    resolution: Literal[720, 1080] = 1080


def validate(request):
    if request.mode != 'sdr':
        raise ValueError('Точный предпросмотр доступен в SDR. HDR-экспорт использует другую цветовую обработку и не может быть показан как SDR-кадр.')
    if request.resolution not in (720, 1080):
        raise ValueError('Разрешение предпросмотра должно быть 720 или 1080')
    if not request.project.clips:
        raise ValueError('Добавьте хотя бы один фрагмент')
    total_frames = 0
    for clip in request.project.clips:
        asset = store.asset(clip.asset_id)
        if not asset:
            raise ValueError('Исходное видео не найдено')
        source = store.DATA/'assets'/clip.asset_id/asset['original']
        if not source.is_file():
            raise ValueError('Исходный файл видео не найден. Перезагрузите его')
        if clip.end > asset['duration'] + 0.05:
            raise ValueError('Конец фрагмента выходит за длительность видео')
        # Match render_frame's ceil-to-frame accounting clip by clip.
        total_frames += max(1, int((clip.end-clip.start)*30 + .999999999))
    total_output_time = total_frames/30
    if request.time > total_output_time + 1e-9:
        raise ValueError('Время предпросмотра выходит за длительность проекта')
    media.validate_export(ExportRequest(project=request.project, mode='sdr', resolution=request.resolution))


def fingerprint(request):
    """Include validated settings and stable source-file identity in cache key."""
    assets = []
    for clip in request.project.clips:
        metadata = store.asset(clip.asset_id)
        source = store.DATA / 'assets' / clip.asset_id / metadata['original']
        try:
            stat = source.stat()
        except OSError as exc:
            raise ValueError('Исходный файл видео не найден. Перезагрузите его') from exc
        identity = [clip.asset_id, metadata['original'], stat.st_size, stat.st_mtime_ns]
        subtitle = media_subtitle_identity(clip.asset_id)
        assets.append([identity, subtitle])
    header = request.project.style.header_image
    graphic = None
    if header:
        path = store.DATA/'graphics'/f'{header}.webp'
        if path.is_file():
            stat = path.stat()
            graphic = [header, stat.st_size, stat.st_mtime_ns]
    payload = {'renderer_version': PREVIEW_RENDERER_VERSION,
               'tonemap_version': SDR_TONEMAP_VERSION,
               'request': request.model_dump(mode='json'), 'assets': assets, 'graphic': graphic}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def media_subtitle_identity(asset_id):
    from . import media
    path = media.subtitle_path(asset_id)
    if not path or not path.is_file():
        return None
    stat = path.stat()
    return [path.name, stat.st_size, stat.st_mtime_ns]


def source_fingerprint(request):
    asset = store.asset(request.clip.asset_id)
    if not asset:
        raise ValueError('Исходное видео не найдено')
    source = store.DATA / 'assets' / request.clip.asset_id / asset['original']
    try:
        stat = source.stat()
    except OSError as exc:
        raise ValueError('Исходный файл видео не найден. Перезагрузите его') from exc
    identity = [request.clip.asset_id, asset['original'], stat.st_size, stat.st_mtime_ns]
    payload = {'tonemap_version': SDR_TONEMAP_VERSION, 'source': identity,
               'start': request.clip.start, 'end': request.clip.end,
               'resolution': request.resolution}
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_source(request):
    asset = store.asset(request.clip.asset_id)
    if not asset:
        raise ValueError('Исходное видео не найдено')
    source = store.DATA / 'assets' / request.clip.asset_id / asset['original']
    if not source.is_file():
        raise ValueError('Исходный файл видео не найден. Перезагрузите его')
    if request.clip.end > asset['duration'] + 0.05:
        raise ValueError('Конец фрагмента выходит за длительность видео')
    if asset.get('dolby_vision'):
        raise ValueError('Для предпросмотра Dolby Vision сначала подготовьте HLG/PQ или SDR-файл')
