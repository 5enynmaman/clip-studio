import subprocess

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import audio, camera, media, speaker, store, worker
from backend.app import app
from backend.models import Clip
from test_pipeline import data, source


def test_waveform_real_audio_range_and_silent_video(data):
    asset = source(data)
    client = TestClient(app)
    result = client.get(f"/api/assets/{asset['id']}/audio/waveform?start=1&end=2&bins=100")
    assert result.status_code == 200
    wave = result.json()
    assert wave['has_audio'] and wave['start'] == 1 and wave['end'] == 2
    assert len(wave['peaks']) == 100 and wave['step'] == pytest.approx(.01)
    assert .05 < max(wave['peaks']) < .25
    assert 0 < max(wave['rms']) < max(wave['peaks'])
    assert client.get(f"/api/assets/{asset['id']}/audio/waveform?end=9").status_code == 400
    assert client.get(f"/api/assets/{asset['id']}/audio/waveform?end=nan").status_code == 400
    assert client.get(f"/api/assets/{asset['id']}/audio/waveform?bins=100000").status_code == 422
    silent = audio.envelope(source(data, audio=False), 0, 1, 50)
    assert not silent['has_audio'] and not any(silent['peaks'])


def test_waveform_keeps_out_of_phase_stereo_and_source_clock(data):
    asset = source(data)
    original = data / 'assets' / asset['id'] / asset['original']
    target = original.with_name('stereo.mkv')
    subprocess.run([media.FFMPEG, '-y', '-loglevel', 'error', '-i', str(original), '-f', 'lavfi', '-i',
                    "aevalsrc=if(between(t\\,1\\,2)\\,0.5*sin(2*PI*440*t)\\,0)|if(between(t\\,1\\,2)\\,-0.5*sin(2*PI*440*t)\\,0):s=8000:d=3",
                    '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'pcm_s16le', str(target)],
                   capture_output=True, check=True, timeout=30)
    store.put_asset({**asset, 'original': target.name})
    wave = audio.envelope(store.asset(asset['id']), .5, 2.5, 100)
    assert max(wave['peaks'][:20]) < .005
    assert max(wave['peaks'][30:70]) > .45
    assert max(wave['peaks'][80:]) < .005


def samples(duration=12, silent=False, overlap=False, brief=False):
    observations, rms = [], []
    for i in range(round(duration * 10)):
        t = i / 10
        talker = 0 if t < 3 or t >= 9 else 1
        if brief and 1.2 <= t < 1.8:
            talker = 1
        quiet = silent and 3 <= t < 9
        faces = [{'track': track, 'motion': .8 if track == talker or overlap else .03,
                  'x': .25 if track == 0 else .75, 'y': .4, 'w': .14, 'h': .25} for track in (0, 1)]
        observations.append({'time': t, 'faces': faces})
        rms.append(0 if quiet else .1)
    return observations, {'rms': rms, 'step': .1}


def make_plan(**options):
    observations, wave = samples(**options)
    return speaker.plan(observations, wave, {'x': .5, 'y': .5, 'zoom': 1}, 12, (.316, 1))


def test_speaker_plan_alternating_turns_is_valid_and_holds_through_silence():
    keys, segments = make_plan()
    assert [s['participant'] for s in segments] == [1, 2, 1]
    assert [k['time'] for k in keys] == pytest.approx([0, 3, 9])
    clip = Clip(id='turns', asset_id='a' * 32, start=20, end=32, camera=keys)
    assert camera.at(clip, 4)['x'] > .8 and camera.at(clip, 10)['x'] < .2
    held, _ = make_plan(silent=True)
    assert len(held) == 1 and held[0]['x'] < .2
    ambiguous, segments = make_plan(overlap=True)
    assert len(ambiguous) == 1 and not segments
    brief, segments = make_plan(brief=True)
    assert not any(1 < key['time'] < 2 for key in brief)


def test_mouth_motion_rejects_static_and_rigid_translation():
    rng = np.random.default_rng(1)
    face = rng.integers(0, 255, size=(64, 64), dtype=np.uint8)
    face = cv2.GaussianBlur(face, (5, 5), 0)
    assert speaker.mouth_motion(face, face, cv2) == pytest.approx(0, abs=.001)
    moved = cv2.warpAffine(face, np.float32([[1, 0, 1], [0, 1, 0]]), (64, 64))
    assert speaker.mouth_motion(face, moved, cv2) < .16
    mouth = face.copy()
    mouth[38:58, 14:50] = np.roll(face[38:58, 14:50], 3, axis=0)
    assert speaker.mouth_motion(face, mouth, cv2) > .16


def test_scene_cut_between_windows_does_not_duplicate_or_reorder_keys():
    observations, wave = samples()
    for sample in observations:
        if sample['time'] == 3.1:
            sample['scene'] = True
    keys, segments = speaker.plan(observations, wave, {'x': .5, 'y': .5, 'zoom': 1}, 12, (.316, 1))
    Clip(id='scene-turns', asset_id='a' * 32, start=0, end=12, camera=keys)
    assert any(k['time'] == pytest.approx(3.1) and k['x'] > .8 for k in keys)
    assert all(b['time'] > a['time'] for a, b in zip(keys, keys[1:]))


def test_speaker_job_uses_new_mode_and_no_face_result_is_conservative(data):
    asset = source(data)
    original = data / 'assets' / asset['id'] / asset['original']
    media.prepare_asset(asset['id'], original, 'Говорящий')
    client = TestClient(app)
    body = {'clip': {'id': 'one', 'asset_id': asset['id'], 'start': .5, 'end': 2.5}, 'mode': 'speaker'}
    assert client.post('/api/reframe/suggest', json={**body, 'mode': 'invalid'}).status_code == 422
    assert client.post('/api/reframe/suggest', json={**body, 'style': {'fit': 'contain'}}).status_code == 400
    response = client.post('/api/reframe/suggest', json=body)
    assert response.status_code == 202
    job = store.claim_job()
    assert job['payload']['mode'] == 'speaker'
    result = worker.process(job)
    assert result['method'] == 'audio-mouth-motion' and result['experimental']
    assert len(result['camera']) == 1 and not result['segments']
    assert 'не удалось' in result['note']
    Clip.model_validate({**body['clip'], 'camera': result['camera']})
    silent = source(data, audio=False)
    assert client.post('/api/reframe/suggest', json={**body, 'clip': {**body['clip'], 'asset_id': silent['id']}}).status_code == 400
