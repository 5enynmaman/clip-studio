import json
import subprocess
import time

import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from backend import ai, composition, media, storage, store
from backend.models import AISettings
from backend.app import app
from test_pipeline import data, source, request_for, render


def test_private_deployment_rejects_local_development_origin(data,monkeypatch):
    monkeypatch.setenv('CLIP_REQUIRE_AUTH','1')
    monkeypatch.setenv('CLIP_USERNAME','creator')
    monkeypatch.setenv('CLIP_PASSWORD','synthetic-password-32')
    with TestClient(app) as client:
        assert client.get('/api/storage').status_code==401
        client.auth=('creator','synthetic-password-32')
        assert client.get('/api/storage').status_code==200
        assert client.post('/api/youtube/inspect',headers={'Origin':'http://127.0.0.1:5173'},json={'url':'https://youtu.be/jNQXAC9IVRw'}).status_code==403


def test_dragged_geometry_photo_font_reaches_actual_mp4(data):
    asset=source(data,audio=False)
    graphic='a'*32
    Image.new('RGB',(80,40),'#ee2233').save(data/'graphics'/f'{graphic}.webp')
    request=request_for(asset,style={'layout':'card','header_image':graphic,'font':'russoone','header_box':{'x':.1,'y':.02,'w':.8,'h':.1},'video_box':{'x':.2,'y':.25,'w':.6,'h':.55},'corner_radius':60,'background':'#64b9d1','title':'Ручной монтаж','title_box':{'x':.2,'y':.13,'w':.6,'h':.1}})
    result=render(request)
    assert result['metadata']['width']==720 and result['metadata']['height']==1280
    path=data/'frame.png'
    subprocess.run([media.FFMPEG,'-loglevel','error','-i',data/'exports'/f"{result['url'].split('/')[-1]}.mp4",'-frames:v','1',path],check=True,capture_output=True)
    image=Image.open(path).convert('RGB')
    assert image.getpixel((360,80))[0]>200 and image.getpixel((360,80))[1]<70
    assert max(abs(a-b) for a,b in zip(image.getpixel((80,600)),(100,185,209)))<12
    assert max(abs(a-b) for a,b in zip(image.getpixel((360,600)),(100,185,209)))>30
    assert composition.font(40,'russoone').getlength('Кириллица')>0


def test_shared_budget_and_retention_preserve_active_sources(data,monkeypatch):
    asset=source(data)
    job_id=store.new_job('export',{'project':{'clips':[{'asset_id':asset['id']}]}})
    with pytest.raises(ValueError):storage.delete_asset(asset['id'])
    with store.connect() as conn:conn.execute('UPDATE assets SET created=?',(time.time()-8*86400,))
    storage.cleanup();assert store.asset(asset['id'])
    store.update_job(job_id,status='ready')
    storage.cleanup();assert store.asset(asset['id']) is None
    monkeypatch.setenv('CLIP_DATA_BUDGET_MB','0')
    with pytest.raises(ValueError):storage.guard(1)


def test_groq_real_sdk_transcription_and_json_request(data,monkeypatch):
    import openai
    asset=source(data)
    media.prepare_asset(asset['id'],data/'assets'/asset['id']/asset['original'],'Тест')
    real_client=openai.OpenAI;calls=[]
    def transport(request):
        assert request.url.host=='api.groq.com'
        if request.url.path.endswith('transcriptions'):
            assert b'whisper-large-v3-turbo' in request.content
            return httpx.Response(200,json={'text':'Тестовая мысль','segments':[{'start':0,'end':2,'text':'Тестовая мысль'}]})
        body=json.loads(request.content);calls.append(body)
        return httpx.Response(200,json={'id':'chat-test','object':'chat.completion','created':0,'model':body['model'],'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({'summary':'Речь','clips':[{'start':0,'end':2,'title':'Мысль','reason':'Завершённая речь','hook':'Слушайте','topic':'Тест'}]})}}]})
    monkeypatch.setattr(openai,'OpenAI',lambda **kw:real_client(**kw,http_client=httpx.Client(transport=httpx.MockTransport(transport))))
    ai.save_settings(AISettings(groq_key='synthetic-key'))
    result=ai.analyze({'id':'groq-test','payload':{'asset_id':asset['id'],'end':2}})
    assert result['provider']=='groq' and result['clips'][0]['end']==2
    assert calls[0]['model']=='openai/gpt-oss-20b' and calls[0]['response_format']['json_schema']['strict']
    assert not (data/'work'/'groq-test').exists()
