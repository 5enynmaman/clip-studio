import json
import sys

import pytest

from backend import worker
from test_pipeline import data


def test_budget_includes_audio_and_rejects_before_downloading():
    with pytest.raises(ValueError,match='лимит'):
        worker.validate_download_size({'height':1080,'requested_formats':[{'filesize':500*1048576},{'filesize_approx':30*1048576}]},512*1048576)
    worker.validate_download_size({'height':480,'requested_formats':[{'filesize':200*1048576},{'filesize_approx':30*1048576}]},512*1048576)


def test_linux_ffmpeg_and_sdr_selection(data,monkeypatch):
    calls=[]
    monkeypatch.setattr(worker.media,'FFMPEG','ffmpeg')
    monkeypatch.setattr(worker.shutil,'which',lambda value:'/usr/bin/ffmpeg')
    assert worker.youtube_ffmpeg()=='/usr/bin/ffmpeg'
    monkeypatch.setenv('CLIP_MAX_UPLOAD_MB','512')
    def run(command,path,timeout):
        calls.append(command)
        if '--dump-single-json' in command:
            path.write_text(json.dumps({'height':1080,'filesize':800*1048576}))
    monkeypatch.setattr(worker,'download_run',run)
    with pytest.raises(ValueError,match='800 МБ'):
        worker.import_youtube({'id':'job','payload':{'url':'https://youtu.be/nhATdDsP04g','asset_id':'a'*32,'max_height':1080}})
    assert len(calls)==1 and '--skip-download' in calls[0]
    assert calls[0][calls[0].index('-S')+1]=='res,hdr:sdr'
    assert not list((data/'assets'/('a'*32)).glob('*.json'))


def test_403_is_reported_without_guessing_account_access(data,monkeypatch):
    monkeypatch.setattr(worker.storage,'guard',lambda:None)
    with pytest.raises(ValueError,match='HTTP 403') as error:
        worker.download_run([sys.executable,'-c','import sys; print("HTTP Error 403: Forbidden",file=sys.stderr); sys.exit(1)'],data/'download.log',10)
    assert 'входа' not in str(error.value)


def test_sdr_preview_keeps_hdr_original_metadata(data):
    import subprocess
    from backend import media,store
    from test_pipeline import source
    asset=source(data,hdr=True)
    original=data/'assets'/asset['id']/asset['original']
    preview=original.parent/'preview-source.mkv'
    subprocess.run([media.FFMPEG,'-loglevel','error','-y','-i',str(original),'-vf',media.to_sdr(asset)+',scale=160:90','-c:v','libx264','-threads','1','-c:a','aac',str(preview)],check=True,capture_output=True,timeout=60)
    result=media.prepare_asset(asset['id'],original,'HDR original','youtube',preview_source=preview)
    assert result['hdr'] and result['width']==320 and result['original']==original.name
    proxy=media.probe(original.parent/'proxy.mp4')
    assert not proxy['hdr'] and proxy['width']==160 and proxy['has_audio']
    assert abs(proxy['duration']-result['duration'])<.2
    assert store.asset(asset['id'])['hdr']


def test_cached_source_must_match_requested_video(data):
    folder=data/'assets'/('a'*32)
    folder.mkdir()
    (folder/'source.info.json').write_text(json.dumps({'id':'jNQXAC9IVRw'}))
    with pytest.raises(ValueError,match='не соответствует'):
        worker.import_youtube({'id':'retry','payload':{'asset_id':'a'*32,'url':'https://youtu.be/nhATdDsP04g','max_height':1080,'reuse_original':True}})
