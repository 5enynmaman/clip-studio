import pytest

from backend import autoframe
from backend.models import Clip
from test_pipeline import data, source


def test_autoframe_geometry_maps_center_and_edges_safely():
    visible_w, visible_h = autoframe._visible_size(1920, 1080, '9:16')
    assert visible_w < .4 and visible_h == 1
    assert autoframe._offset(.5, visible_w) == pytest.approx(.5)
    assert autoframe._offset(0, visible_w) == 0
    assert autoframe._offset(1, visible_w) == 1


def test_autoframe_rejects_missing_proxy_with_useful_message(data):
    asset = source(data)
    clip = {'id': 'clip', 'asset_id': asset['id'], 'start': 0, 'end': 2}
    with pytest.raises(ValueError, match='предпросмотр'):
        autoframe.suggest(clip, {}, 'test-job')


def test_camera_schema_limits_keys_to_clip_and_validates_transitions(data):
    asset = source(data)
    base = {'id': 'clip', 'asset_id': asset['id'], 'start': 1, 'end': 3}
    Clip.model_validate({**base, 'camera': [{'time': 2, 'x': .4, 'y': .5, 'zoom': 1.2, 'transition': 'linear'}]})
    with pytest.raises(ValueError, match='длительность'):
        Clip.model_validate({**base, 'camera': [{'time': 2.1, 'x': .4, 'y': .5, 'zoom': 1.2}]})
    with pytest.raises(ValueError, match='возрастающее'):
        Clip.model_validate({**base, 'camera': [
            {'time': .5, 'x': .4, 'y': .5, 'zoom': 1.2}, {'time': .5, 'x': .5, 'y': .5, 'zoom': 1.2}]})
