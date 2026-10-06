"""Experimental active-speaker framing using audio energy and local mouth motion.

This is a conservative audiovisual heuristic, not voice diarization. It never
assigns a voice to a person based on the mixed audio waveform alone.
"""
import math
from pathlib import Path

import numpy as np

from . import audio, autoframe, camera, composition, media, store
from .models import Clip, Style

SAMPLE_FPS = 10
WINDOW = .6
MIN_SHOT = 1.8


def mouth_motion(previous, current, cv2):
    """Subtract rigid head/camera motion before measuring lower-face motion."""
    flow = cv2.calcOpticalFlowFarneback(previous, current, None, .5, 2, 15, 2, 5, 1.1, 0)
    upper = flow[8:34, 10:54]
    rigid = np.median(upper.reshape(-1, 2), axis=0)
    residual = np.linalg.norm(flow - rigid, axis=2)
    mouth = float(np.percentile(residual[38:58, 14:50], 75))
    reference = float(np.percentile(residual[8:34, 10:54], 75))
    return max(0.0, mouth - reference)


def plan(observations, envelope, initial, duration, visible):
    """Require sustained, unambiguous evidence and hold through silence/overlap."""
    keys = [{**initial, 'time': 0.0, 'transition': 'cut'}]
    segments = []
    levels = np.asarray(envelope['rms'])
    threshold = max(.003, float(np.percentile(levels, 90)) * .12) if len(levels) else .003
    current_track = None
    pending_track = None
    pending_since = 0.0
    last_switch = -MIN_SHOT
    for window_index in range(math.ceil(duration / WINDOW)):
        start, end = window_index * WINDOW, min(duration, (window_index + 1) * WINDOW)
        samples = [s for s in observations if start <= s['time'] < end]
        if any(s.get('scene') for s in samples):
            pending_track = None
            current_track = None
            scene_time = round(next(s['time'] for s in samples if s.get('scene')) * 30) / 30
            if scene_time > keys[-1]['time'] and len(keys) < 60:
                keys.append({'time': scene_time, 'x': .5, 'y': .5, 'zoom': 1.0, 'transition': 'cut'})
            last_switch = scene_time - MIN_SHOT
            # Evidence from before the cut must not identify a face in the new shot.
            samples = [s for s in samples if s['time'] >= scene_time]
        scores, positions = {}, {}
        for sample in samples:
            i = min(len(levels) - 1, int(sample['time'] / envelope['step']))
            if i < 0 or levels[i] < threshold:
                continue
            for face in sample['faces']:
                scores.setdefault(face['track'], []).append(face['motion'])
                positions.setdefault(face['track'], []).append(face)
        ranked = sorted(((float(np.median(v)), track) for track, v in scores.items() if len(v) >= 3), reverse=True)
        candidate = None
        if ranked:
            best, track = ranked[0]
            second = ranked[1][0] if len(ranked) > 1 else 0.0
            if best >= .16 and best >= max(.1, second) * 1.65:
                candidate = track
        if candidate is None or candidate == current_track:
            pending_track = None
            continue
        if pending_track != candidate:
            pending_track = candidate
            pending_since = max(start, samples[0]['time']) if samples else start
            continue
        # Confirmation arrives in the following window. Place the cut at the
        # beginning of the confirmed turn, quantized to the export's 30 fps.
        if start - pending_since < WINDOW - 1e-6 or pending_since - last_switch < MIN_SHOT:
            continue
        face = positions[candidate][len(positions[candidate]) // 2]
        zoom = min(1.35, max(1.0, .38 / max(face['w'], face['h'])))
        key_time = min(duration, round(pending_since * 30) / 30)
        key = {'time': key_time, 'x': round(autoframe._offset(face['x'], visible[0] / zoom), 4),
               'y': round(autoframe._offset(face['y'], visible[1] / zoom), 4),
               'zoom': round(zoom, 3), 'transition': 'cut'}
        if key_time == keys[-1]['time']:
            keys[-1] = key
        elif key_time < keys[-1]['time']:
            pending_track = None
            continue
        elif len(keys) < 60:
            keys.append(key)
        else:
            break
        segments.append({'time': key_time, 'participant': candidate + 1})
        current_track, last_switch, pending_track = candidate, key_time, None
    return keys, segments


def suggest(clip_data, style_data, job_id):
    import cv2
    cv2.setNumThreads(1)
    clip, style = Clip.model_validate(clip_data), Style.model_validate(style_data or {})
    asset = store.asset(clip.asset_id)
    if not asset or clip.end > asset['duration'] + .05:
        raise ValueError('Фрагмент или исходное видео недоступны')
    if not asset.get('has_audio'):
        raise ValueError('Для переключения на говорящего нужна звуковая дорожка')
    if style.fit != 'cover':
        raise ValueError('Для переключения камеры выберите кадрирование «Заполнить»')
    proxy = store.DATA / 'assets' / clip.asset_id / 'proxy.mp4'
    if not proxy.is_file():
        raise ValueError('Предпросмотр для анализа лиц не найден')
    duration = clip.end - clip.start
    store.update_job(job_id, progress=.05, message='Анализ звука выбранного фрагмента')
    sound = audio.envelope(asset, clip.start, clip.end, math.ceil(duration * SAMPLE_FPS))
    cuts = autoframe._scene_cuts(proxy, clip.start, duration)
    detector = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'))
    if detector.empty():
        raise ValueError('Детектор лиц недоступен')
    cap = cv2.VideoCapture(str(proxy))
    if not cap.isOpened():
        cap.release()
        raise ValueError('Не удалось прочитать видео для анализа говорящего')
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    cap.set(cv2.CAP_PROP_POS_MSEC, clip.start * 1000)
    tracks, next_track, observations = [], 0, []
    frame_width, frame_height = asset['width'], asset['height']
    sample_every = max(1, round(fps / SAMPLE_FPS))
    cut_index = 0
    try:
        for frame_index in range(math.ceil(duration * fps)):
            if not cap.grab():
                break
            if frame_index % sample_every:
                continue
            ok, frame = cap.retrieve()
            if not ok:
                continue
            t = frame_index / fps
            scene = cut_index < len(cuts) and t >= cuts[cut_index]
            if scene:
                while cut_index < len(cuts) and cuts[cut_index] <= t:
                    cut_index += 1
                tracks = []
            height, width = frame.shape[:2]
            frame_width, frame_height = width, height
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            analysis_width = min(640, width)
            gray = cv2.resize(gray, (analysis_width, max(64, round(height * analysis_width / width))))
            gh, gw = gray.shape
            # Refresh boxes at 5 Hz; motion samples remain at 10 Hz.
            if frame_index % (sample_every * 2) == 0 or scene:
                boxes = detector.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(32, 32))
                boxes = sorted(boxes, key=lambda b: int(b[2]) * int(b[3]), reverse=True)[:6]
                unmatched = list(tracks)
                refreshed = []
                for x, y, w, h in boxes:
                    cx, cy = (x + w / 2) / gw, (y + h / 2) / gh
                    nearest = min(unmatched, key=lambda track: math.hypot(cx - track['cx'], cy - track['cy']), default=None)
                    if nearest and math.hypot(cx - nearest['cx'], cy - nearest['cy']) < .12:
                        track = nearest
                        unmatched = [other for other in unmatched if other['id'] != nearest['id']]
                    else:
                        track = {'id': next_track, 'previous': None}
                        next_track += 1
                    track.update(box=(int(x), int(y), int(w), int(h)), cx=cx, cy=cy)
                    refreshed.append(track)
                tracks = refreshed
            faces = []
            for track in tracks:
                x, y, w, h = track['box']
                crop = gray[y:y + h, x:x + w]
                if crop.size == 0:
                    continue
                crop = cv2.resize(crop, (64, 64))
                motion = mouth_motion(track['previous'], crop, cv2) if track['previous'] is not None else 0
                track['previous'] = crop
                faces.append({'track': track['id'], 'motion': motion, 'x': (x + w / 2) / gw,
                              'y': (y + h * .42) / gh, 'w': w / gw, 'h': h / gh})
            observations.append({'time': t, 'faces': faces, 'scene': scene})
            if len(observations) % 20 == 0:
                store.update_job(job_id, progress=.15 + .75 * t / duration,
                                 message=f'Сопоставляем звук и движение рта · {t:.0f} / {duration:.0f} сек.')
    finally:
        cap.release()
    if not observations:
        raise ValueError('Не удалось получить кадры для анализа говорящего')
    out_w, out_h = media.output_size(style, 1080)
    _, _, inner_w, inner_h = composition.geometry(style, out_w, out_h, 1080)
    visible = autoframe._visible_size(frame_width, frame_height, f'{inner_w}:{inner_h}')
    keys, segments = plan(observations, sound, camera.at(clip, 0), duration, visible)
    note = (f'Звук + движение рта: найдено {len(segments)} уверенных планов. ' if segments else
            'Уверенно определить говорящего не удалось; оставлен базовый план кадрирования. ')
    note += 'Экспериментальный режим: проверьте перед применением. Музыка, закадровая речь, перекрытие голосов и лица в профиль могут мешать.'
    return {'camera': keys, 'segments': segments, 'method': 'audio-mouth-motion', 'note': note,
            'switches': max(0, len(keys) - 1), 'experimental': True}
