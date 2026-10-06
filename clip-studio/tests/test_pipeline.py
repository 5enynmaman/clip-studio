import base64
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import media, store
from backend.app import app
from backend.models import ExportRequest, Project
from backend.worker import youtube_url


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "studio.sqlite3")
    monkeypatch.delenv("CLIP_PASSWORD", raising=False)
    monkeypatch.delenv("CLIP_REQUIRE_AUTH", raising=False)
    store.init()
    return tmp_path


def source(data: Path, hdr=False, audio=True):
    asset_id = uuid.uuid4().hex
    folder = data / "assets" / asset_id
    folder.mkdir()
    path = folder / "source.mkv"
    args = [media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3"]
    if hdr:
        args += ["-vf", "zscale=pin=bt709:tin=bt709:min=bt709:p=bt2020:t=arib-std-b67:m=bt2020nc,format=yuv420p10le", "-c:v", "libx265", "-x265-params", "pools=1:frame-threads=1", "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc"]
    else:
        args += ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac"]
    subprocess.run([*args, str(path)], check=True, capture_output=True, timeout=60)
    meta = media.probe(path)
    asset = {"id": asset_id, "original": path.name, "title": "Тестовый исходник", **meta}
    store.put_asset(asset)
    return asset


def request_for(asset, **changes):
    return ExportRequest.model_validate({"project": {"name": "Тест", "clips": [{"id": "one", "asset_id": asset["id"], "start": .5, "end": 1.5}], **changes}, "resolution": 720})


def render(request):
    job_id = store.new_job("export", request.model_dump())
    job = store.claim_job()
    assert job["id"] == job_id
    return media.export_video(job)


def test_real_sdr_export_with_text_and_silent_insert(data):
    a = source(data)
    b = source(data, audio=False)
    (data / "assets" / a["id"] / "captions.srt").write_text("1\n00:00:00,400 --> 00:00:01,400\nПроверяем субтитры\n", encoding="utf-8")
    request = request_for(a, style={"title": "Тест: HDR / %{literal}", "border": 24, "subtitles": True})
    request.project.clips.append(request.project.clips[0].model_copy(update={"id": "two", "asset_id": b["id"]}))
    result = render(request)
    meta = result["metadata"]
    assert (meta["width"], meta["height"]) == (720, 1280)
    assert abs(meta["duration"] - 2) < .2
    assert meta["codec"] == "h264" and meta["has_audio"] and not meta["hdr"]
    assert meta["primaries"] == "bt709" and meta["matrix"] == "bt709"
    assert not list((data / "work").glob("*/part*.mp4"))


def test_real_hlg_export_and_tonemapped_proxy(data):
    asset = source(data, hdr=True)
    assert asset["hdr"] and asset["bit_depth"] == 10
    original = data / "assets" / asset["id"] / asset["original"]
    media.prepare_asset(asset["id"], original, "Синтетический HLG")
    proxy = media.probe(original.parent / "proxy.mp4")
    assert not proxy["hdr"] and proxy["bit_depth"] == 8 and proxy["width"] <= 960
    assert not any(item.get('side_data_type') in {'Mastering display metadata', 'Content light level metadata'}
                   for item in proxy.get('side_data', []))
    from backend.models import Clip
    source_preview = data/'hlg-source-preview.mp4'
    media.render_source_preview(Clip(id='preview', asset_id=asset['id'], start=.5, end=1.5),
                                720, source_preview, data/'work'/'hlg-source-preview')
    source_meta = media.probe(source_preview)
    assert not source_meta['hdr']
    assert not any(item.get('side_data_type') in {'Mastering display metadata', 'Content light level metadata'}
                   for item in source_meta.get('side_data', []))
    request = request_for(asset)
    request.mode = "hdr"
    meta = render(request)["metadata"]
    assert meta["hdr"] and meta["codec"] == "hevc"
    assert meta["transfer"] == "arib-std-b67" and meta["bit_depth"] == 10
    assert (meta["width"], meta["height"]) == (720, 1280)


def test_hdr_rejects_false_hdr_and_unmanaged_overlays(data):
    sdr = source(data)
    request = request_for(sdr)
    request.mode = "hdr"
    with pytest.raises(ValueError, match="HDR-исходники"):
        media.validate_export(request)
    hdr = {**sdr, "id": uuid.uuid4().hex, "hdr": True, "transfer": "arib-std-b67"}
    store.put_asset(hdr)
    request = request_for(hdr, style={"title": "Текст"})
    request.mode = "hdr"
    with pytest.raises(ValueError, match="Текст"):
        media.validate_export(request)


def test_pq_mastering_metadata_is_preserved(data):
    asset_id = uuid.uuid4().hex
    folder = data / 'assets' / asset_id
    folder.mkdir()
    path = folder / 'source.mkv'
    master = 'G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)'
    subprocess.run([media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:duration=2:rate=30', '-vf', 'format=yuv420p10le', '-c:v', 'libx265', '-pix_fmt', 'yuv420p10le', '-x265-params', f'pools=1:frame-threads=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display={master}:max-cll=1000,400', '-color_primaries', 'bt2020', '-color_trc', 'smpte2084', '-colorspace', 'bt2020nc', str(path)], check=True, capture_output=True, timeout=60)
    asset = {'id': asset_id, 'original': path.name, 'title': 'Синтетический PQ', **media.probe(path)}
    store.put_asset(asset)
    request = request_for(asset)
    request.mode = 'hdr'
    output = render(request)['metadata']
    assert output['hdr'] and output['transfer'] == 'smpte2084'
    mastering = next(s for s in output['side_data'] if s['side_data_type'] == 'Mastering display metadata')
    assert float(media.Fraction(mastering['max_luminance'])) == 1000
    cll = next(s for s in output['side_data'] if s['side_data_type'] == 'Content light level metadata')
    assert (cll['max_content'], cll['max_average']) == (1000, 400)


@pytest.mark.parametrize("url", ["http://www.youtube.com/watch?v=jfKfPfyJRdk", "https://localhost/watch?v=jfKfPfyJRdk", "https://youtube.com.evil.test/watch?v=jfKfPfyJRdk", "https://www.youtube.com/playlist?list=foo", "https://user@youtu.be/jfKfPfyJRdk", "https://youtu.be/not-an-id"])
def test_youtube_rejects_non_video_urls(url):
    with pytest.raises(ValueError):
        youtube_url(url)


def test_youtube_canonicalizes():
    assert youtube_url("https://youtu.be/jfKfPfyJRdk?t=10") == "https://www.youtube.com/watch?v=jfKfPfyJRdk"


def test_api_project_auth_csrf_ranges_and_validation(data, monkeypatch):
    a = source(data)
    folder = data / "assets" / a["id"]
    (folder / "proxy.mp4").write_bytes(b"0123456789")
    with TestClient(app) as client:
        response = client.get(f"/api/assets/{a['id']}/proxy", headers={"Range": "bytes=2-4"})
        assert response.status_code == 206 and response.content == b"234"
        assert client.put(f"/api/projects/{uuid.uuid4()}", json=Project().model_dump(), headers={"Origin": "https://evil.test"}).status_code == 403
        request = request_for(a).model_dump()
        request["project"]["clips"][0]["end"] = 100
        assert client.post("/api/exports", json=request).status_code == 400
        assert client.post("/api/assets/youtube", json={"url": "https://localhost/private"}).status_code == 400
        response = client.post(f"/api/assets/{a['id']}/transcript", files={"file": ("captions.srt", b"no timestamps")})
        assert response.status_code == 400
        monkeypatch.setenv("CLIP_USERNAME", "creator")
        monkeypatch.setenv("CLIP_PASSWORD", "long-test-password")
        assert client.get("/api/assets").status_code == 401
        token = base64.b64encode(b"creator:long-test-password").decode()
        assert client.get("/api/assets", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_subtitle_overlap_and_file_upload(data):
    a = source(data)
    with TestClient(app) as client:
        srt = "1\n00:00:00,000 --> 00:00:01,000\nкамера\n\n2\n00:00:01,000 --> 00:00:03,000\n{\\pos(0,0)}HDR\n"
        response = client.post(f"/api/assets/{a['id']}/transcript", files={"file": ("x.srt", srt.encode())})
        assert response.status_code == 201
        assert len(client.get(f"/api/assets/{a['id']}/transcript/cues").json()) == 2
        path = data / "offset.srt"
        media.clip_subtitles(a["id"], .5, 2, path)
        content = path.read_text()
        assert "00:00:00,000 --> 00:00:00,500" in content
        assert "{\\pos" not in content
        original = data / "assets" / a["id"] / a["original"]
        with original.open("rb") as input_file:
            result = client.post("/api/assets/upload", files={"file": ("test.mp4", input_file, "video/mp4")})
        assert result.status_code == 202
        assert store.claim_job()["kind"] == "prepare"
