import os
import base64
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import storage, store, worker
from backend.app import app


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path/'studio.sqlite3')
    monkeypatch.delenv('CLIP_PASSWORD', raising=False)
    monkeypatch.delenv('CLIP_REQUIRE_AUTH', raising=False)
    store.init()
    return tmp_path


def test_cancel_queued_job_is_idempotent_and_does_not_expose_payload(data):
    job_id = store.new_job('youtube', {'url': 'https://secret.invalid/video', 'asset_id': 'a' * 32})
    with TestClient(app) as client:
        first = client.post(f'/api/jobs/{job_id}/cancel')
        repeated = client.post(f'/api/jobs/{job_id}/cancel')
        listed = client.get('/api/jobs')
    assert first.status_code == repeated.status_code == 200
    assert first.json()['status'] == repeated.json()['status'] == 'cancelled'
    assert store.job_status(job_id)['status'] == 'cancelled'
    assert 'secret.invalid' not in listed.text
    assert listed.json()[0]['context']['title'] == 'Импорт видео с YouTube'


def test_cancel_all_marks_queued_cancelled_and_running_cancelling(data):
    running_id = store.new_job('running-kind', {'title': 'running'})
    assert store.claim_job()['id'] == running_id
    queued_id = store.new_job('queued-kind', {'title': 'queued'})
    with TestClient(app) as client:
        result = client.post('/api/jobs/cancel-all')
    body = result.json()
    assert result.status_code == 200
    assert queued_id in body['cancelled']
    assert running_id in body['cancelling']
    assert store.job_status(queued_id)['status'] == 'cancelled'
    assert store.job_status(running_id)['status'] == 'cancelling'


def test_cancel_endpoints_require_auth_and_reject_cross_origin(data, monkeypatch):
    job_id = store.new_job('test', {})
    password = 'a-long-test-password-123'
    monkeypatch.setenv('CLIP_PASSWORD', password)
    monkeypatch.setenv('CLIP_USERNAME', 'owner')
    monkeypatch.setenv('CLIP_REQUIRE_AUTH', '1')
    auth = 'Basic ' + base64.b64encode(f'owner:{password}'.encode()).decode()
    with TestClient(app) as client:
        one_anon = client.post(f'/api/jobs/{job_id}/cancel')
        all_anon = client.post('/api/jobs/cancel-all')
        one_cross_origin = client.post(f'/api/jobs/{job_id}/cancel', headers={
            'Authorization': auth, 'Origin': 'https://attacker.example',
        })
        all_cross_origin = client.post('/api/jobs/cancel-all', headers={
            'Authorization': auth, 'Origin': 'https://attacker.example',
        })
    assert one_anon.status_code == all_anon.status_code == 401
    assert one_cross_origin.status_code == all_cross_origin.status_code == 403
    assert store.job_status(job_id)['status'] == 'queued'


def test_completion_write_cannot_overwrite_cancellation_request(data):
    job_id = store.new_job('test', {})
    store.claim_job()
    assert store.request_cancel(job_id) == 'cancelling'
    assert not store.update_job(job_id, status='ready', progress=1, message='Готово')
    assert store.job_status(job_id)['status'] == 'cancelling'


def test_cancelling_job_keeps_its_asset_busy_until_process_tree_is_stopped(data):
    asset_id = 'c'*32
    store.put_asset({'id':asset_id,'original':'source.mp4','title':'Working source'})
    job_id = store.new_job('preview-source', {'clip':{'asset_id':asset_id}})
    store.claim_job()
    assert store.request_cancel(job_id) == 'cancelling'
    assert storage.busy(asset_id)
    with pytest.raises(ValueError, match='очереди'):
        storage.delete_asset(asset_id)
    store.update_job(job_id, status='cancelled')
    assert not storage.busy(asset_id)


def test_running_ffmpeg_is_killed_and_next_job_completes(data, monkeypatch):
    first_id = store.new_job('long-test', {})
    next_id = store.new_job('next-test', {})
    first_job = store.claim_job()
    marker = data/'work'/first_id/'ffmpeg.pid'

    def fake_process(job):
        if job['id'] == first_id:
            command = [worker.media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-re',
                       '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30:duration=60',
                       '-f', 'null', '-']
            child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            marker.write_text(f'{child.pid},{os.getpgrp()}')
            child.wait()
            return {'unexpected': 'completed'}
        return {'ok': True}

    monkeypatch.setattr(worker, 'process', fake_process)
    cancel_result = {}

    def cancel_after_ffmpeg_starts():
        deadline = time.monotonic()+10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(.02)
        if marker.exists():
            cancel_result['ffmpeg_pid'], cancel_result['group_id'] = map(int, marker.read_text().split(','))
        with TestClient(app) as client:
            cancel_result['status'] = client.post(f'/api/jobs/{first_id}/cancel').json()['status']

    canceller = threading.Thread(target=cancel_after_ffmpeg_starts)
    canceller.start()
    with (data/'worker.lock').open('a') as lock:
        worker._execute_claimed_job(first_job, lock.fileno())
        canceller.join(timeout=10)
        assert not canceller.is_alive()
        assert cancel_result['status'] == 'cancelling'
        assert store.job_status(first_id)['status'] == 'cancelled'
        assert not worker._job_group_has_live_processes(cancel_result['group_id'], after_kill=True)
        next_job = store.claim_job()
        assert next_job['id'] == next_id
        worker._execute_claimed_job(next_job, lock.fileno())
    assert store.job_status(next_id)['status'] == 'ready'
    assert store.job_status(next_id)['result'] == '{"ok": true}'


def test_cancellation_reaps_stubborn_descendant_after_job_leader_exits(data, monkeypatch):
    first_id = store.new_job('leader-exit-test', {})
    next_id = store.new_job('next-test', {})
    first_job = store.claim_job()
    marker = data/'descendant.txt'

    def fake_process(job):
        if job['id'] != first_id:
            return {'ok': True}
        descendant = subprocess.Popen([sys.executable, '-c',
            'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marker.write_text(f'{descendant.pid},{os.getpgrp()}')
        store.request_cancel(first_id)
        os._exit(0)

    monkeypatch.setattr(worker, 'process', fake_process)
    with (data/'worker.lock').open('a') as lock:
        worker._execute_claimed_job(first_job, lock.fileno())
        assert store.job_status(first_id)['status'] == 'cancelled'
        _, group_id = map(int, marker.read_text().split(','))
        assert not worker._job_group_has_live_processes(group_id, after_kill=True)
        next_job = store.claim_job()
        assert next_job['id'] == next_id
        worker._execute_claimed_job(next_job, lock.fileno())
    assert store.job_status(next_id)['status'] == 'ready'


def test_cancelled_proxy_upgrade_preserves_current_playable_proxy(data, monkeypatch):
    asset_id = 'd'*32
    folder = data/'assets'/asset_id
    folder.mkdir(parents=True)
    old_proxy = folder/'proxy.mp4'
    old_bytes = b'previous seekable proxy'
    old_proxy.write_bytes(old_bytes)
    store.put_asset({'id':asset_id,'title':'Старый исходник','original':'source.mp4',
                     'width':1920,'height':1080,'duration':60,
                     'preview_width':426,'preview_height':240})
    first_id = store.new_job('prepare-playback', {'asset_id':asset_id})
    next_id = store.new_job('next-test', {})
    first_job = store.claim_job()
    marker = data/'work'/first_id/'upgrade-child.pid'

    def slow_upgrade(source_id, work, progress=None):
        partial = work/'proxy-upgrade.mp4'
        partial.write_bytes(b'partial replacement')
        child = subprocess.Popen([worker.media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-re',
            '-f','lavfi','-i','testsrc2=size=640x360:rate=30:duration=60','-f','null','-'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marker.write_text(str(child.pid))
        child.wait()
        partial.replace(old_proxy)
        return {'asset_id':source_id}

    monkeypatch.setattr(worker.media, 'upgrade_playback_proxy', slow_upgrade)
    original_process = worker.process
    monkeypatch.setattr(worker, 'process', lambda job: {'ok':True} if job['id'] == next_id else original_process(job))
    cancellation = {}

    def cancel_after_child_starts():
        deadline = time.monotonic()+10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(.02)
        with TestClient(app) as client:
            cancellation['status'] = client.post(f'/api/jobs/{first_id}/cancel').json()['status']

    thread = threading.Thread(target=cancel_after_child_starts)
    thread.start()
    with (data/'worker.lock').open('a') as lock:
        worker._execute_claimed_job(first_job, lock.fileno())
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert cancellation['status'] == 'cancelling'
        assert store.job_status(first_id)['status'] == 'cancelled'
        assert old_proxy.read_bytes() == old_bytes
        assert not (data/'work'/first_id).exists()
        next_job = store.claim_job()
        assert next_job['id'] == next_id
        worker._execute_claimed_job(next_job, lock.fileno())
    assert store.job_status(next_id)['status'] == 'ready'


def test_cancel_import_also_cancels_queued_automatic_analysis(data):
    parent = store.new_job('youtube', {'asset_id': 'a' * 32})
    store.claim_job()
    child = store.new_job_if_parent_running(parent, 'analyze', {'asset_id': 'a' * 32})
    assert child
    assert store.request_cancel(parent) == 'cancelling'
    assert store.job_status(child)['status'] == 'cancelled'


def test_preview_frame_context_uses_the_clip_at_requested_timeline_time(data):
    first_id, second_id = 'a'*32, 'b'*32
    store.put_asset({'id': first_id, 'title': 'Первый исходник'})
    store.put_asset({'id': second_id, 'title': 'Второй исходник'})
    project = {'name': 'Проект', 'clips': [
        {'id':'one','asset_id':first_id,'start':3,'end':6,'label':'Первый фрагмент'},
        {'id':'two','asset_id':second_id,'start':10,'end':13,'label':'Второй фрагмент'},
    ]}
    store.new_job('preview-frame', {'request':{'project':project}, 'time':3.0})
    context = store.jobs()[0]['context']
    assert context['title'] == 'Второй исходник'
    assert context['clip_label'] == 'Второй фрагмент'
    assert context['source_time'] == 10
    assert context['clip_range'] == {'start':10,'end':13}


def test_export_context_names_project_instead_of_first_source(data):
    project = {'name':'Сводный ролик', 'clips':[
        {'id':'one','asset_id':'a'*32,'start':0,'end':4},
        {'id':'two','asset_id':'b'*32,'start':8,'end':12},
    ]}
    store.new_job('preview-video', {'project':project})
    context = store.jobs()[0]['context']
    assert context == {'title':'Сводный ролик','clip_count':2}


def test_stale_recovery_fails_closed_when_process_metadata_is_missing(data):
    job_id = store.new_job('test', {})
    store.claim_job()
    with pytest.raises(RuntimeError, match='identity is missing'):
        worker._recover_stale_jobs()
    assert store.job_status(job_id)['status'] == 'running'


def test_stale_recovery_does_not_signal_live_group_with_wrong_identity(data, monkeypatch):
    job_id = store.new_job('test', {})
    store.claim_job()
    pid = 987654
    pid_path = data/'work'/job_id/'job.pid.json'
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text('{"pid": 987654, "start": "old-identity"}')
    monkeypatch.setattr(worker, '_process_start_token', lambda candidate: 'different-identity')
    monkeypatch.setattr(worker, '_job_group_has_live_processes', lambda pgid: True)
    monkeypatch.setattr(worker, '_terminate_job_group', lambda *args, **kwargs: pytest.fail('must not signal an unverified group'))
    with pytest.raises(RuntimeError, match='identity cannot be verified'):
        worker._recover_stale_jobs()
    assert store.job_status(job_id)['status'] == 'running'
