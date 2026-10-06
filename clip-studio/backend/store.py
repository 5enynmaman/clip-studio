import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DATA = Path(os.environ.get("CLIP_DATA_DIR", Path(__file__).resolve().parents[1] / "data")).resolve()
DB = DATA / "studio.sqlite3"

# Only bounded, interactive work may share the machine with a long render.
QUICK_JOB_SQL = "COALESCE((kind IN ('preview-frame','youtube-info') OR (kind='preview-source' AND json_extract(payload,'$.clip.end')-json_extract(payload,'$.clip.start')<=15.05)),0)"


def job_lane(kind, payload):
    clip = payload.get('clip') or {}
    if kind in {'preview-frame', 'youtube-info'}:
        return 'quick'
    if kind == 'preview-source' and float(clip.get('end', 1e9))-float(clip.get('start', 0)) <= 15.05:
        return 'quick'
    return 'main'


@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    for folder in ("assets", "exports", "work", "graphics"):
        (DATA / folder).mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL, progress REAL DEFAULT 0, message TEXT DEFAULT '', result TEXT,
              created REAL NOT NULL, updated REAL NOT NULL);
        """)


def put_asset(asset):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO assets VALUES (?, ?, ?)", (asset["id"], json.dumps(asset, ensure_ascii=False), time.time()))


def asset(asset_id):
    with connect() as conn:
        row = conn.execute("SELECT body FROM assets WHERE id=?", (asset_id,)).fetchone()
    return json.loads(row["body"]) if row else None


def assets():
    with connect() as conn:
        return [json.loads(row["body"]) for row in conn.execute("SELECT body FROM assets ORDER BY created DESC")]


def project_list():
    with connect() as conn:
        return [{"id": row["id"], **json.loads(row["body"])} for row in conn.execute("SELECT * FROM projects ORDER BY updated DESC")]


def save_project(project_id, body):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO projects VALUES (?, ?, ?)", (project_id, json.dumps(body, ensure_ascii=False), time.time()))


def new_job(kind, payload):
    job_id = uuid.uuid4().hex
    now = time.time()
    with connect() as conn:
        conn.execute("INSERT INTO jobs (id, kind, payload, status, created, updated) VALUES (?, ?, ?, 'queued', ?, ?)", (job_id, kind, json.dumps(payload, ensure_ascii=False), now, now))
    return job_id


def new_job_if_parent_running(parent_job_id, kind, payload):
    """Enqueue a child task only while its importing/prepare job is active."""
    job_id = uuid.uuid4().hex
    now = time.time()
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        parent = conn.execute('SELECT status FROM jobs WHERE id=?', (parent_job_id,)).fetchone()
        if not parent or parent['status'] != 'running':
            return None
        conn.execute("INSERT INTO jobs (id,kind,payload,status,created,updated) VALUES (?,?,?,'queued',?,?)",
                     (job_id, kind, json.dumps({**payload, 'parent_job_id': parent_job_id}, ensure_ascii=False), now, now))
    return job_id


def update_job(job_id, **changes):
    allowed = {"status", "progress", "message", "result"}
    assert set(changes) <= allowed
    if "result" in changes:
        changes["result"] = json.dumps(changes["result"], ensure_ascii=False)
    changes["updated"] = time.time()
    with connect() as conn:
        # Progress/error/ready writes from a finishing worker must not overwrite
        # a cancellation request or a terminal status.
        if changes.get('status') == 'cancelled':
            allowed_statuses = ('queued', 'running', 'cancelling')
        else:
            allowed_statuses = ('queued', 'running')
        marks = ','.join('?' for _ in allowed_statuses)
        cursor = conn.execute(
            f"UPDATE jobs SET {', '.join(f'{key}=?' for key in changes)} WHERE id=? AND status IN ({marks})",
            (*changes.values(), job_id, *allowed_statuses))
        return cursor.rowcount > 0


def request_cancel(job_id):
    now = time.time()
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            return None
        status = row['status']
        if status == 'queued':
            conn.execute("UPDATE jobs SET status='cancelled',message='Отменено пользователем',updated=? WHERE id=? AND status='queued'", (now, job_id))
        elif status == 'running':
            conn.execute("UPDATE jobs SET status='cancelling',message='Останавливаем обработку',updated=? WHERE id=? AND status='running'", (now, job_id))
        if status in {'queued', 'running'}:
            # Automatic follow-on analysis must not survive cancellation of the
            # import/prepare job that created it.
            for child in conn.execute("SELECT id,payload FROM jobs WHERE status='queued'").fetchall():
                try:
                    payload = json.loads(child['payload'])
                except (TypeError, json.JSONDecodeError):
                    continue
                if payload.get('parent_job_id') == job_id:
                    conn.execute("UPDATE jobs SET status='cancelled',message='Отменённая задача не запускает анализ',updated=? WHERE id=? AND status='queued'", (now, child['id']))
            return 'cancelled' if status == 'queued' else 'cancelling'
        return status


def cancel_all_active():
    now = time.time()
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("SELECT id,status FROM jobs WHERE status IN ('queued','running','cancelling')").fetchall()
        queued = [row['id'] for row in rows if row['status'] == 'queued']
        running = [row['id'] for row in rows if row['status'] in {'running','cancelling'}]
        conn.execute("UPDATE jobs SET status='cancelled',message='Отменено пользователем',updated=? WHERE status='queued'", (now,))
        conn.execute("UPDATE jobs SET status='cancelling',message='Останавливаем обработку',updated=? WHERE status='running'", (now,))
    return {'cancelled': queued, 'cancelling': running, 'count': len(queued) + len(running)}


def job_status(job_id):
    with connect() as conn:
        row = conn.execute('SELECT id,kind,status,progress,message,result,created,updated FROM jobs WHERE id=?', (job_id,)).fetchone()
    return dict(row) if row else None


def jobs():
    with connect() as conn:
        rows = conn.execute("SELECT id, kind, payload, status, progress, message, result, created FROM jobs ORDER BY CASE WHEN status IN ('queued','running','cancelling') THEN 0 ELSE 1 END, created DESC LIMIT 80").fetchall()
    output = []
    for row in rows:
        payload = json.loads(row['payload'])
        context = _safe_job_context(row['kind'], payload)
        output.append({**{key: row[key] for key in ('id','kind','status','progress','message','created')},
                       'result': json.loads(row['result']) if row['result'] else None,
                       'lane': job_lane(row['kind'], payload),
                       'context': context})
    return output


def _safe_job_context(kind, payload):
    project = payload.get('request', {}).get('project') or payload.get('project') or {}
    clips = project.get('clips') or []
    if kind in {'export', 'preview-video'}:
        return {'title': project.get('name') or ('Экспорт ролика' if kind == 'export' else 'Предпросмотр ролика'),
                'clip_count': len(clips)}
    if kind == 'preview-frame' and clips:
        frame_index = max(0, int(max(0.0, float(payload.get('time', 0))) * 30 + 1e-9))
        frame_counts = [max(1, int((float(c['end'])-float(c['start']))*30 + .999999999)) for c in clips]
        frame_index = min(frame_index, sum(frame_counts)-1)
        start_frame = 0
        selected = 0
        for index, count in enumerate(frame_counts):
            if frame_index < start_frame + count:
                selected = index
                break
            start_frame += count
        clip = clips[selected]
        local_time = (frame_index-start_frame)/30
        source_time = float(clip['start']) + local_time
    else:
        clip = payload.get('clip') or (clips[0] if clips else {})
        source_time = None
    asset_id = clip.get('asset_id') or payload.get('asset_id')
    start = clip.get('start', payload.get('start'))
    end = clip.get('end', payload.get('end'))
    asset_record = asset(asset_id) if asset_id else None
    title = (asset_record or {}).get('title') or payload.get('title')
    if not title:
        title = {'youtube':'Импорт видео с YouTube','youtube-info':'Проверка видео YouTube',
                 'export':'Экспорт ролика','preview-video':'Предпросмотр ролика',
                 'preview-frame':'Кадр предпросмотра','preview-source':'Предпросмотр исходника',
                 'reframe':'Автокадрирование','analyze':'Анализ видео','prepare':'Подготовка видео',
                 'prepare-playback':'Улучшение предпросмотра'}.get(kind, 'Обработка видео')
    context = {'title': title}
    if asset_id:
        context['asset_id'] = asset_id
    if start is not None and end is not None:
        context['range'] = {'start': start, 'end': end}
    if clip.get('label'):
        context['clip_label'] = clip['label']
    if source_time is not None:
        context['source_time'] = source_time
        context['clip_range'] = {'start': start, 'end': end}
    return context


def claim_job(lane=None):
    if lane not in (None, 'quick', 'main'):
        raise ValueError('Unknown job lane')
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        condition = '' if lane is None else f' AND {QUICK_JOB_SQL}' if lane == 'quick' else f' AND NOT {QUICK_JOB_SQL}'
        priority = "CASE kind WHEN 'preview-frame' THEN 0 ELSE 1 END, " if lane == 'quick' else ''
        row = conn.execute(f"SELECT * FROM jobs WHERE status='queued'{condition} ORDER BY {priority}created LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE jobs SET status='running', updated=? WHERE id=?", (time.time(), row["id"]))
            return {**dict(row), "payload": json.loads(row["payload"])}
    return None
