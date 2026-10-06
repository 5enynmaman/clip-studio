import subprocess
import uuid

import pytest
from PIL import Image, ImageChops, ImageStat

from backend import camera, media, store
from backend.models import Clip, ExportRequest


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "studio.sqlite3")
    store.init()
    return tmp_path


def make_source(data):
    asset_id = uuid.uuid4().hex
    folder = data / "assets" / asset_id
    folder.mkdir(parents=True)
    path = folder / "source.mkv"
    subprocess.run([media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=25:duration=2", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True, timeout=60)
    metadata = media.probe(path)
    asset = {"id": asset_id, "original": path.name, "title": "synthetic", **metadata}
    store.put_asset(asset)
    return asset


def test_camera_evaluator_cut_linear_and_legacy():
    clip = Clip(id="c", asset_id="a" * 32, start=0, end=4, crop_x=.2, camera=[
        {"time": 1, "x": .1, "y": .2, "zoom": 1.2},
        {"time": 3, "x": .9, "y": .8, "zoom": 2.8, "transition": "linear"},
    ])
    assert camera.at(clip, .5) == {"x": .2, "y": .5, "zoom": 1.0}
    assert camera.at(clip, 1.5) == pytest.approx({"x": .3, "y": .35, "zoom": 1.6})
    assert camera.at(clip, 2) == pytest.approx({"x": .5, "y": .5, "zoom": 2.0})
    assert camera.at(clip, 3) == {"x": .9, "y": .8, "zoom": 2.8}
    old = Clip(id="old", asset_id="b" * 32, start=0, end=2, crop_x=.73)
    assert camera.at(old, 1) == {"x": .73, "y": .5, "zoom": 1.0}


@pytest.mark.parametrize("camera_points", [
    [{"time": 0, "x": .5, "y": .5, "zoom": 1}, {"time": 0, "x": .1, "y": .2, "zoom": 2}],
    [{"time": 3, "x": .5, "y": .5, "zoom": 1}],
])
def test_camera_rejects_duplicate_or_out_of_range_times(camera_points):
    with pytest.raises(ValueError):
        Clip(id="c", asset_id="a" * 32, start=1, end=3, camera=camera_points)


def test_rendered_camera_frame_matches_exported_frame(data):
    asset = make_source(data)
    (data / "assets" / asset["id"] / "captions.srt").write_text(
        "1\n00:00:00,900 --> 00:00:01,400\nКамера синхронно\n", encoding="utf-8")
    request = ExportRequest.model_validate({
        "resolution": 720,
        "project": {"name": "frame check", "clips": [
            {"id": "first", "asset_id": asset["id"], "start": .37, "end": .87, "crop_x": 0},
            {"id": "second", "asset_id": asset["id"], "start": .87, "end": 1.87,
             "camera": [
                 {"time": 0, "x": 0, "y": 0, "zoom": 2},
                 {"time": .2666666666667, "x": 1, "y": 0, "zoom": 1.4},
                 {"time": .5, "x": 1, "y": 1, "zoom": 1.5, "transition": "linear"},
             ]},
            ], "style": {"title": "Кадр тест", "border": 24, "subtitles": True}},
    })
    job_id = store.new_job("export", request.model_dump())
    job = store.claim_job()
    result = media.export_video(job)
    assert result["metadata"]["transfer"] == "bt709"
    assert result["metadata"]["primaries"] == "bt709"
    assert result["metadata"]["matrix"] == "bt709"
    exported = data / "exports" / f"{job_id}.mp4"
    # Check old crop_x behavior, the camera start, then a hard cut followed by linear motion.
    for requested_time in (.3, .6, .7666666666667, .8):
        sample = data / f"sample-{requested_time}.png"
        info = media.render_frame(request, requested_time, sample, data / "frame-work")
        reference = data / f"reference-{requested_time}.png"
        subprocess.run([media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(info["time"]), "-i", exported,
                        "-frames:v", "1", "-update", "1", reference], check=True, capture_output=True, timeout=60)
        assert info["width"] == 720 and info["height"] == 1280 and info["mode"] == "sdr"
        assert info["time"] == pytest.approx(requested_time)
        with Image.open(sample).convert("RGB") as rendered, Image.open(reference).convert("RGB") as encoded:
            assert rendered.size == encoded.size == (720, 1280)
            # H.264 introduces small color error, while a different crop/layout causes a large one.
            difference = ImageStat.Stat(ImageChops.difference(rendered, encoded)).mean
            assert max(difference) < 3, f"sample {info['time']}: {difference}"
    assert result["mode"] == "sdr"


def test_render_frame_rejects_hdr(data):
    request = ExportRequest.model_validate({"mode": "hdr", "project": {"clips": []}})
    with pytest.raises(ValueError, match="только SDR"):
        media.render_frame(request, 0, data / "frame.png", data / "work")


def test_camera_contain_frame_matches_export(data):
    asset = make_source(data)
    request = ExportRequest.model_validate({"resolution": 720, "project": {
        "clips": [{"id": "contain", "asset_id": asset["id"], "start": .37, "end": 1.37,
                   "camera": [{"time": 0, "x": .5, "y": .5, "zoom": 1},
                              {"time": .5, "x": .8, "y": .2, "zoom": 2, "transition": "linear"}]}],
        "style": {"fit": "contain", "background": "#224466", "aspect": "9:16"},
    }})
    job_id = store.new_job("export", request.model_dump())
    media.export_video(store.claim_job())
    exported = data / "exports" / f"{job_id}.mp4"
    sample, reference = data / "contain.png", data / "contain-reference.png"
    media.render_frame(request, .7, sample, data / "contain-work")
    subprocess.run([media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.7", "-i", exported,
                    "-frames:v", "1", "-update", "1", reference], check=True, capture_output=True, timeout=60)
    with Image.open(sample).convert("RGB") as rendered, Image.open(reference).convert("RGB") as encoded:
        assert max(ImageStat.Stat(ImageChops.difference(rendered, encoded)).mean) < 10


def test_zoompan_bounds_extreme_custom_viewport():
    clip = Clip(id="narrow", asset_id="a" * 32, start=0, end=2,
                camera=[{"time": 0, "x": .5, "y": .5, "zoom": 3}])
    assert camera.zoompan(clip, 1920, 1080, 22, 1280, "cover", "black") is None


def test_frame_sample_and_index_tolerate_float_boundary_rounding():
    time = 8 / 30
    selector = media.frame_sample_filter(time)
    threshold = float(selector.split(",", 1)[1].rstrip("')"))
    assert threshold < time and time >= threshold
    assert media.frame_index_for_time(123 / 30) == 123
    assert media.frame_index_for_time(123 / 30 - .000001) == 122


@pytest.mark.parametrize("zoom", [float("inf"), float("nan")])
def test_camera_rejects_nonfinite_keyframe_values(zoom):
    with pytest.raises(ValueError):
        Clip(id="bad", asset_id="a" * 32, start=0, end=1,
             camera=[{"time": 0, "x": .5, "y": .5, "zoom": zoom}])


def test_hdr_camera_export_retains_10bit_pixels(data):
    asset_id = uuid.uuid4().hex
    folder = data / "assets" / asset_id
    folder.mkdir(parents=True)
    path = folder / "source.mkv"
    subprocess.run([media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=30:duration=1", "-vf",
                    "zscale=pin=bt709:tin=bt709:min=bt709:p=bt2020:t=arib-std-b67:m=bt2020nc,format=yuv420p10le",
                    "-c:v", "libx265", "-x265-params", "pools=1:frame-threads=1", "-pix_fmt", "yuv420p10le",
                    "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc", str(path)],
                   check=True, capture_output=True, timeout=60)
    asset = {"id": asset_id, "original": path.name, "title": "synthetic HLG", **media.probe(path)}
    store.put_asset(asset)
    request = ExportRequest.model_validate({"mode": "hdr", "resolution": 720, "project": {"clips": [
        {"id": "hdr-camera", "asset_id": asset_id, "start": 0, "end": 1,
         "camera": [{"time": 0, "x": .2, "y": .7, "zoom": 2}]}
    ]}})
    filters, _, _, _ = media.video_filters(request, request.project.clips[0], asset, 0, data / "work")
    assert "zoompan" not in ",".join(filters)
    assert any("eval=frame" in part for part in filters)
    job_id = store.new_job("export", request.model_dump())
    output = media.export_video(store.claim_job())["metadata"]
    assert output["hdr"] and output["transfer"] == "arib-std-b67"
    assert output["pix_fmt"] == "yuv420p10le" and output["bit_depth"] == 10
