"""Owner-controlled retention and a shared budget for originals, proxies and work."""
import os
import json
import uuid
import shutil
import time
from pathlib import Path

from . import store


PREVIEW_TTL = 24 * 60 * 60
PREVIEW_MAX_COUNT = 300
PREVIEW_MAX_BYTES = 512 * 1024 * 1024


def active_fingerprint_job(kind, fingerprint):
    with store.connect() as conn:
        rows = conn.execute("SELECT id,payload FROM jobs WHERE kind=? AND status IN ('queued','running','cancelling') ORDER BY created DESC", (kind,)).fetchall()
    for row in rows:
        if json.loads(row['payload']).get('fingerprint') == fingerprint:
            return row['id']
    return None


def enqueue_fingerprinted(kind, payload, fingerprint, supersede=False):
    """Atomically reuse an in-flight result or queue a fresh cache render."""
    now = time.time()
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        request = payload.get('request', {})
        project = request.get('project', payload.get('project', {}))
        for clip in project.get('clips', []):
            if not conn.execute('SELECT 1 FROM assets WHERE id=?', (clip.get('asset_id'),)).fetchone():
                raise ValueError('Исходное видео не найдено')
        # Completed cache records are reusable only while their file is present.
        rows = conn.execute("SELECT id,status,payload,result FROM jobs WHERE kind=? ORDER BY created DESC", (kind,)).fetchall()
        for row in rows:
            old = json.loads(row['payload'])
            if old.get('fingerprint') != fingerprint:
                continue
            if row['status'] in {'queued', 'running'}:
                return row['id']
            if row['status'] == 'ready':
                result = json.loads(row['result'] or '{}')
                cache_id = result.get('cache_id')
                suffix = '.mp4' if kind in {'preview-video', 'preview-source'} else '.png'
                cache_path = store.DATA / 'previews' / f'{cache_id}{suffix}' if cache_id else None
                if cache_path and cache_path.is_file() and cache_path.stat().st_mtime >= now - PREVIEW_TTL:
                    return row['id']
        if supersede:
            conn.execute("UPDATE jobs SET status='cancelled',message='Заменено более новым кадром предпросмотра',updated=? WHERE kind=? AND status='queued'", (now, kind))
        count = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0]
        if count >= 10:
            raise ValueError('В очереди уже 10 задач. Дождитесь завершения обработки')
        job_id = uuid.uuid4().hex
        conn.execute("INSERT INTO jobs (id,kind,payload,status,created,updated) VALUES (?,?,?,'queued',?,?)",
                     (job_id, kind, json.dumps(payload, ensure_ascii=False), now, now))
        return job_id


def guard_preview_cache(incoming_bytes=0, preserve=None):
    folder = store.DATA / 'previews'
    folder.mkdir(parents=True, exist_ok=True)
    paths = sorted((p for pattern in ('*.png', '*.mp4') for p in folder.glob(pattern) if p.is_file()), key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - PREVIEW_TTL
    for path in paths:
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
    paths = sorted((p for pattern in ('*.png', '*.mp4') for p in folder.glob(pattern) if p.is_file()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in paths)
    protected = Path(preserve).resolve() if preserve else None
    projected_count = len(paths) + (1 if incoming_bytes else 0)
    protected_bytes = protected.stat().st_size if protected and protected.is_file() else 0
    if protected_bytes > PREVIEW_MAX_BYTES:
        raise ValueError('Файл предпросмотра превышает лимит кэша 512 МБ')
    while paths and (projected_count > PREVIEW_MAX_COUNT or total + incoming_bytes > PREVIEW_MAX_BYTES):
        old = next((candidate for candidate in paths if not protected or candidate.resolve() != protected), None)
        if old is None:
            raise ValueError('Для предпросмотра превышен лимит кэша 512 МБ или 300 файлов')
        paths.remove(old)
        projected_count -= 1
        size = old.stat().st_size
        old.unlink(missing_ok=True)
        total -= size
    if incoming_bytes > PREVIEW_MAX_BYTES:
        raise ValueError('Кадр предпросмотра превышает лимит кэша')
    try:
        guard(incoming_bytes)
    except ValueError:
        raise


def usage():
    total=0
    for p in store.DATA.rglob('*'):
        try:
            if p.is_file() and not p.is_symlink():total+=p.stat().st_size
        except FileNotFoundError:
            pass  # Another process may finish or remove temporary media during this scan.
    return total


def status():
    return {"used": usage(), "budget": int(os.environ.get('CLIP_DATA_BUDGET_MB', '2048')) * 1048576,
            "free": shutil.disk_usage(store.DATA).free, "max_upload_mb": int(os.environ.get('CLIP_MAX_UPLOAD_MB', '2048')),
            "retention_days": int(os.environ.get('CLIP_RETENTION_DAYS', '7'))}


def guard(extra=0):
    state = status()
    if state['used'] + extra > state['budget']:
        raise ValueError('Достигнут лимит хранилища. Удалите ненужные исходники или экспорты')
    minimum = int(os.environ.get('CLIP_MIN_FREE_MB', '512')) * 1048576
    if state['free'] - extra < minimum:
        raise ValueError('На диске недостаточно свободного места для безопасной обработки')


def busy(asset_id=None):
    with store.connect() as conn:
        rows = conn.execute("SELECT payload FROM jobs WHERE status IN ('queued','running','cancelling')").fetchall()
    if asset_id is None:
        return bool(rows)
    import json
    payloads=[json.loads(r['payload']) for r in rows]
    return any(_payload_uses_asset(p, asset_id) for p in payloads)


def _payload_uses_asset(payload, asset_id):
    if payload.get('asset_id') == asset_id or payload.get('clip', {}).get('asset_id') == asset_id:
        return True
    projects = [payload.get('project', {}), payload.get('request', {}).get('project', {})]
    return any(any(c.get('asset_id') == asset_id for c in project.get('clips', [])) for project in projects)


def delete_asset(asset_id):
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("SELECT payload FROM jobs WHERE status IN ('queued','running','cancelling')").fetchall()
        if any(_payload_uses_asset(json.loads(row['payload']), asset_id) for row in rows):
            raise ValueError('Видео используется в очереди. Дождитесь завершения задачи')
        conn.execute('DELETE FROM assets WHERE id=?', (asset_id,))
    shutil.rmtree(store.DATA/'assets'/asset_id, ignore_errors=True)


def delete_export(job_id):
    with store.connect() as conn:
        row = conn.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row and row['status'] in {'queued','running','cancelling'}:
            raise ValueError('Экспорт ещё выполняется')
        conn.execute("DELETE FROM jobs WHERE id=? AND kind='export'", (job_id,))
    (store.DATA/'exports'/f'{job_id}.mp4').unlink(missing_ok=True)


def cleanup():
    # Idle-only cleanup prevents deletion of sources still needed by queued jobs.
    if busy():
        return
    preview_folder = store.DATA/'previews'
    if preview_folder.exists():
        for path in preview_folder.glob('*'):
            try:
                if path.is_file() and path.stat().st_mtime < time.time()-PREVIEW_TTL:
                    path.unlink(missing_ok=True)
                elif path.is_dir() and path.stat().st_mtime < time.time()-PREVIEW_TTL:
                    shutil.rmtree(path, ignore_errors=True)
            except FileNotFoundError:
                pass
    days = int(os.environ.get('CLIP_RETENTION_DAYS', '7'))
    # Zero disables age-based deletion for a user's local desktop library.
    # Server defaults and any positive retention period keep existing behavior.
    if days > 0:
        cutoff = time.time() - days * 86400
        with store.connect() as conn:
            expired = [r['id'] for r in conn.execute('SELECT id FROM assets WHERE created<?', (cutoff,))]
        for asset_id in expired:
            delete_asset(asset_id)
        for path in (store.DATA/'exports').glob('*.mp4'):
            if path.stat().st_mtime < cutoff:
                delete_export(path.stem)
    for path in (store.DATA/'work').iterdir():
        if path.stat().st_mtime < time.time()-86400:
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
    # Expired sources leave editable project plans, not misleading existing-file links.
    with store.connect() as conn:
        conn.execute("DELETE FROM jobs WHERE updated<? AND status IN ('ready','error','cancelled','dismissed')", (time.time()-30*86400,))
