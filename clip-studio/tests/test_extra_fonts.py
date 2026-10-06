import subprocess

from fastapi.testclient import TestClient
from PIL import Image

from backend import composition,media
from backend.app import app
from backend.models import Style
from test_pipeline import data,source,request_for,render


def test_font_catalog_serves_same_bundled_files(data):
    with TestClient(app) as client:
        for name in composition.FONTS:
            assert Style(font=name).font==name
            response=client.get(f'/api/fonts/{name}')
            assert response.status_code==200
            assert response.content==(composition.FONT_DIR/f'{name}.ttf').read_bytes()
            assert composition.font(40,name).getlength('Красивые истории Жизнь')>0
            assert (composition.FONT_DIR/f'{name}-OFL.txt').is_file()
        assert client.get('/api/fonts/unknown').status_code==404


def test_elegant_font_is_in_actual_video(data):
    asset=source(data,audio=False)
    result=render(request_for(asset,style={'layout':'card','font':'prata','title':'Живые истории','background':'#f5f3ef','text_color':'#101116','header_height':300}))
    target=data/'exports'/f"{result['url'].split('/')[-1]}.mp4"
    frame=data/'font-frame.png'
    subprocess.run([media.FFMPEG,'-loglevel','error','-i',str(target),'-frames:v','1',str(frame)],check=True,capture_output=True,timeout=60)
    img=Image.open(frame).convert('RGB')
    header=img.crop((40,10,680,180))
    assert sum(1 for rgb in header.getdata() if max(rgb)<80)>100
    assert result['metadata']['width']==720 and result['metadata']['height']==1280
