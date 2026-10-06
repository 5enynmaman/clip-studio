import time

import pytest
from fastapi.testclient import TestClient

from backend import store, worker
from backend.app import app


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA', tmp_path)
    monkeypatch.setattr(store, 'DB', tmp_path/'studio.sqlite3')
    monkeypatch.setattr(worker, 'STOP_REQUESTED', False)
    monkeypatch.delenv('CLIP_PASSWORD', raising=False)
    monkeypatch.delenv('CLIP_REQUIRE_AUTH', raising=False)
    store.init()
    return tmp_path


def test_lanes_only_admit_bounded_work_and_prioritize_frames(data):
    heavy = store.new_job('prepare-playback', {})
    long_window = store.new_job('preview-source', {'clip': {'start':0, 'end':60}})
    short_window = store.new_job('preview-source', {'clip': {'start':200, 'end':215}})
    frame = store.new_job('preview-frame', {})
    assert store.claim_job('quick')['id'] == frame
    assert store.claim_job('main')['id'] == heavy
    assert store.claim_job('quick')['id'] == short_window
    assert store.claim_job('quick') is None
    assert store.claim_job('main')['id'] == long_window
    assert {j['id']:j['lane'] for j in store.jobs()} == {
        heavy:'main',long_window:'main',short_window:'quick',frame:'quick'}


@pytest.mark.parametrize('cancel_quick', [False, True])
def test_quick_job_completes_or_cancels_while_heavy_job_keeps_running(data, monkeypatch, cancel_quick):
    heavy = store.new_job('prepare-playback', {})
    quick = store.new_job('preview-frame', {})
    marker = data/'heavy-running'
    release = data/'release-heavy'

    def process(job):
        if job['id'] == heavy:
            marker.write_text('running')
            while not release.exists():
                time.sleep(.02)
            return {'heavy':True}
        if cancel_quick:
            while True:
                time.sleep(.02)
        return {'quick':True}

    monkeypatch.setattr(worker, 'process', process)
    with (data/'worker.lock').open('a') as lock:
        main_run = worker._job_lifecycle(store.claim_job('main'), lock.fileno())
        quick_run = worker._job_lifecycle(store.claim_job('quick'), lock.fileno())
        next(main_run)
        next(quick_run)
        if cancel_quick:
            store.request_cancel(quick)
        deadline = time.monotonic()+10
        try:
            while time.monotonic()<deadline:
                next(main_run)
                try:
                    next(quick_run)
                except StopIteration:
                    break
                time.sleep(.02)
            else:
                pytest.fail('Quick lane was blocked by heavy processing')
            assert marker.exists()
            assert store.job_status(heavy)['status'] == 'running'
            assert store.job_status(quick)['status'] == ('cancelled' if cancel_quick else 'ready')
        finally:
            release.write_text('finish')
            for _ in main_run:
                time.sleep(.02)
    assert store.job_status(heavy)['status'] == 'ready'


def test_proxy_revision_changes_when_completed_file_is_replaced(data):
    asset_id='a'*32
    folder=data/'assets'/asset_id
    folder.mkdir()
    store.put_asset({'id':asset_id,'title':'Тест','original':'source.mp4','codec':'av1','hdr':True})
    proxy=folder/'proxy.mp4'
    proxy.write_bytes(b'old proxy')
    with TestClient(app) as client:
        first=client.get('/api/assets').json()[0]['preview_revision']
        temporary=folder/'replacement.mp4'
        temporary.write_bytes(b'completed upgraded proxy')
        temporary.replace(proxy)
        second=client.get('/api/assets').json()[0]['preview_revision']
    assert first and second and first != second


def test_active_process_remains_visible_after_many_quick_results(data):
    heavy=store.new_job('prepare-playback', {})
    store.claim_job()
    for _ in range(85):
        job=store.new_job('preview-frame', {})
        store.update_job(job,status='ready')
    assert any(job['id']==heavy for job in store.jobs())
