import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import ai,store
from backend.app import app
from backend.models import AnalysisRequest,AISettings
from test_pipeline import data


def test_rolling_subtitles_compact_without_losing_new_words():
    result=ai.compact_transcript([{'start':0,'end':1,'text':'Первая мысль'},{'start':.5,'end':2,'text':'Первая мысль Вторая мысль'},{'start':1,'end':3,'text':'Вторая мысль'}])
    assert ' '.join(c['text'] for c in result)=='Первая мысль Вторая мысль'
    assert result[0]['start']==0 and result[-1]['end']>=2


def test_batches_bound_large_pasted_subtitle():
    text='Длинный текст '*2000
    batches=ai.transcript_batches([{'start':0,'end':60,'text':text}])
    assert len(batches)>1
    assert all(len(json.dumps(b,ensure_ascii=False))<=8000 for b in batches)
    assert ''.join(c['text'] for b in batches for c in b)==text


def test_groq_long_transcript_batches_and_rate_limit(data,monkeypatch):
    import openai
    transcript=[{'start':i*20,'end':i*20+10,'text':str(i)+' текст '*55} for i in range(60)]
    monkeypatch.setattr(ai.media,'subtitle_path',lambda _:None)
    monkeypatch.setattr(ai.media,'read_cues',lambda _:transcript)
    waits=[];monkeypatch.setattr(ai.time,'sleep',waits.append)
    calls=[];real=openai.OpenAI
    def transport(req):
        body=json.loads(req.content);calls.append(body)
        if len(calls)==2:return httpx.Response(429,headers={'retry-after':'2'},json={'error':{'message':'synthetic throttle','code':'rate_limit_exceeded'}})
        batch=json.loads(body['messages'][0]['content'].split('\n')[-1])
        assert len(json.dumps(batch,ensure_ascii=False))<=8000
        first=batch[0]
        result={'summary':'Часть','clips':[{'start':first['start'],'end':first['start']+10,'title':'Момент','reason':'Речь','hook':'Мысль','topic':'Тема'}]}
        return httpx.Response(200,json={'id':'batch','object':'chat.completion','created':0,'model':body['model'],'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(result)}}]})
    monkeypatch.setattr(openai,'OpenAI',lambda **kw:real(**kw,http_client=httpx.Client(transport=httpx.MockTransport(transport))))
    result=ai.groq_analysis(AISettings(groq_key='synthetic'),AnalysisRequest(end=1200),{'id':'a'*32,'has_audio':False},data,lambda *_:None)
    assert len(calls)>2 and waits==[2]
    assert len(result.clips)>1 and result.clips[-1].start>600
    assert max(c['max_completion_tokens'] for c in calls)==2000


def test_dismiss_preserves_history_and_cannot_stop_work(data):
    job=store.new_job('youtube',{})
    with TestClient(app) as c:
        assert c.post(f'/api/jobs/{job}/dismiss').status_code==409
        store.update_job(job,status='error',message='Historical failure')
        assert c.post(f'/api/jobs/{job}/dismiss').status_code==200
        row=next(j for j in store.jobs() if j['id']==job)
        assert row['status']=='dismissed' and row['message']=='Historical failure'
