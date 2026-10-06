import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend import ai, composition, media, store
from backend.app import app
from backend.models import AISettings, AnalysisRequest, Style
from test_pipeline import data, source, request_for, render


def test_real_rounded_canvas_export(data):
    asset = source(data, audio=False)
    request = request_for(asset, style={"layout": "card", "border": 24, "corner_radius": 90, "background": "#64b9d1", "text_color": "#101116", "title": "Тестовый заголовок", "eyebrow": "Мой комментарий", "account": "@my_account"})
    result = render(request)
    path = data / "exports" / result["url"].split('/')[-1]
    image_path = data / 'frame.png'
    subprocess.run([media.FFMPEG, '-loglevel', 'error', '-i', path.with_suffix('.mp4'), '-frames:v', '1', image_path], check=True, capture_output=True)
    image = Image.open(image_path).convert('RGB')
    x, y, w, h = composition.geometry(request.project.style, 720, 1280, 720)
    background = (100, 185, 209)
    # Rounded corner must expose the canvas; the center must contain video pixels.
    assert max(abs(a-b) for a,b in zip(image.getpixel((x+2,y+2)), background)) < 12
    assert max(abs(a-b) for a,b in zip(image.getpixel((x+w//2,y+h//2)), background)) > 30
    assert result['metadata']['has_audio'] and result['metadata']['primaries'] == 'bt709'


def test_ai_credentials_not_exposed_or_queued(data, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    asset = source(data)
    with TestClient(app) as client:
        rejected = client.post(f"/api/assets/{asset['id']}/analyze", json={"provider":"openai", "end": 2})
        assert rejected.status_code == 400
        response = client.put('/api/ai/settings', json={"provider":"openai", "openai_key":"synthetic-test-key", "auto_analyze":True})
        assert response.status_code == 200 and response.json()['openai_ready']
        assert 'synthetic-test-key' not in response.text
        assert (ai.settings_path().stat().st_mode & 0o777) == 0o600
        client.put('/api/ai/settings', json={"provider":"openai", "auto_analyze":True})
        assert ai.settings().openai_key == 'synthetic-test-key'
        ai.queue_auto(asset['id'])
        job = store.claim_job()
        assert job['kind'] == 'analyze' and 'synthetic-test-key' not in json.dumps(job)
        assert client.get(f"/api/assets/{asset['id']}/analysis/result").json() is None
        assert client.post(f"/api/assets/{asset['id']}/analyze", json={"end": 9}).status_code == 400


@pytest.mark.parametrize('captions', [True, False])
def test_openai_sdk_request_and_offsets_with_real_frames(data, monkeypatch, captions):
    import openai
    asset = source(data)
    media.prepare_asset(asset['id'], data/'assets'/asset['id']/asset['original'], 'Тест')
    if captions:
        (data/'assets'/asset['id']/'captions.srt').write_text('1\n00:00:00,000 --> 00:00:03,000\nТестовая речь\n')
    payload = {"summary":"Интересный момент", "clips":[{"start":.1,"end":1.3,"title":"Момент","reason":"Законченная мысль","hook":"Смотрите","topic":"Тест"}, {"start":0,"end":5000,"title":"Неверный","reason":"","hook":"","topic":""}]}
    real_client = openai.OpenAI
    calls = []
    transcribed = []
    def transport(request):
        if request.url.path.endswith('/audio/transcriptions'):
            assert b'whisper-1' in request.content and b'verbose_json' in request.content
            transcribed.append(True)
            return httpx.Response(200,json={"task":"transcribe","language":"ru","duration":1.8,"text":"Тестовая речь", "segments":[{"id":0,"seek":0,"start":0,"end":1.8,"text":"Тестовая речь","tokens":[],"temperature":0,"avg_logprob":0,"compression_ratio":1,"no_speech_prob":0}]})
        body=json.loads(request.content); calls.append(body)
        return httpx.Response(200,json={"id":"resp_test", "object":"response", "created_at":0,"status":"completed", "model":"gpt-4.1-mini", "output":[{"id":"msg_test", "type":"message", "role":"assistant", "status":"completed", "content":[{"type":"output_text", "text":json.dumps(payload),"annotations":[]}]}]})
    monkeypatch.setattr(openai,'OpenAI',lambda **kw: real_client(**kw,http_client=httpx.Client(transport=httpx.MockTransport(transport))))
    ai.save_settings(AISettings(provider='openai',openai_key='synthetic-key'))
    job={'id':'analysis-test','payload':{'asset_id':asset['id'],'provider':'openai','start':1,'end':2.8}}
    result=ai.analyze(job)
    assert result['clips'][0]['start']==1.1 and result['clips'][0]['end']==2.3 and result['discarded']==1
    assert calls[0]['store'] is False and calls[0]['text']['format']['strict'] is True
    assert bool(transcribed) is not captions
    assert any(c['type']=='input_image' for c in calls[0]['input'][0]['content'])
    assert not (data/'work'/'analysis-test').exists()
    assert json.loads((data/'assets'/asset['id']/'recommendations.json').read_text())['clips']==result['clips']


def test_provider_failure_sanitized_and_remote_file_deleted(data, monkeypatch):
    from google import genai
    asset=source(data)
    media.prepare_asset(asset['id'],data/'assets'/asset['id']/asset['original'],'Тест')
    ai.save_settings(AISettings(provider='gemini',gemini_key='synthetic-secret'))
    deleted=[]
    uploaded=SimpleNamespace(name='files/test',uri='https://example.invalid/test',state=SimpleNamespace(name='ACTIVE'))
    def fail(**kw):
        raise RuntimeError('provider URL?key=synthetic-secret')
    client=SimpleNamespace(files=SimpleNamespace(upload=lambda **kw:uploaded,delete=lambda **kw:deleted.append(kw['name'])),models=SimpleNamespace(generate_content=fail),close=lambda:None)
    monkeypatch.setattr(genai,'Client',lambda **kw:client)
    with pytest.raises(ValueError) as failure:
        ai.analyze({'id':'failed-test','payload':{'provider':'gemini','asset_id':asset['id'],'end':2}})
    assert 'synthetic-secret' not in str(failure.value)
    assert deleted==['files/test'] and not (data/'work'/'failed-test').exists()


def test_analysis_bounds_and_finite_timestamps():
    with pytest.raises(ValueError):
        AnalysisRequest(end=1801)
    result=ai.normalize(ai.Recommendations(summary='test',clips=[ai.Suggestion(start=float('nan'),end=4,title='',reason='',hook='',topic='')]),AnalysisRequest(end=10),10)
    assert result['clips']==[] and result['discarded']==1
