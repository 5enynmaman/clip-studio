"""Local, conservative face and scene based camera suggestions."""
import math
from pathlib import Path
import re
import subprocess

from . import media, store
from .models import Clip, Style


MAX_SAMPLES = 180


def _scene_cuts(proxy, clip_start, duration):
    """Detect hard cuts accurately while reading only the lightweight proxy."""
    command = [media.FFMPEG, '-hide_banner', '-loglevel', 'info', '-nostdin', '-ss', str(clip_start),
               '-t', str(duration), '-i', str(proxy), '-an', '-vf',
               "scale=320:-2,select='gt(scene,0.25)',showinfo", '-f', 'null', '-']
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    timestamps = [float(value) for value in re.findall(r'pts_time:([0-9]+(?:\.[0-9]+)?)', result.stderr)]
    cuts = []
    for timestamp in timestamps:
        if .025 < timestamp < duration and (not cuts or timestamp-cuts[-1] >= .5):
            cuts.append(timestamp)
    return cuts


def _clamp(v, low=0.0, high=1.0):
    return max(low, min(high, float(v)))


def _offset(center, visible):
    span = max(0.0, 1.0 - visible)
    return _clamp((center - visible / 2) / span) if span else 0.5


def _visible_size(frame_w, frame_h, aspect):
    aw, ah = (int(v) for v in aspect.split(':'))
    frame_ratio = frame_w / frame_h
    target = aw / ah
    if frame_ratio > target:
        return target / frame_ratio, 1.0
    return 1.0, frame_ratio / target


def suggest(clip_data, style_data, job_id):
    clip = Clip.model_validate(clip_data)
    style = Style.model_validate(style_data or {})
    asset = store.asset(clip.asset_id)
    if not asset:
        raise ValueError('Исходное видео не найдено')
    if clip.end > asset['duration'] + .05:
        raise ValueError('Фрагмент выходит за длительность исходного видео')
    duration = clip.end - clip.start
    if duration > 180:
        raise ValueError('Автокадрирование работает с фрагментами до 180 секунд')
    proxy = store.DATA / 'assets' / clip.asset_id / 'proxy.mp4'
    if not proxy.is_file():
        raise ValueError('Лёгкий предпросмотр исходника не найден')

    try:
        import cv2
        import numpy as np
    except ImportError:
        raise ValueError('Локальный анализ кадра недоступен: установите OpenCV headless') from None
    cv2.setNumThreads(1)
    try:
        cv2.ocl.setUseOpenCL(False)
    except Exception:
        pass

    cap = cv2.VideoCapture(str(proxy))
    if cap is None or not cap.isOpened():
        raise ValueError('Не удалось прочитать лёгкий предпросмотр для анализа')
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    proxy_duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps
    first = max(0.0, clip.start)
    last = min(clip.end, proxy_duration)
    scene_cuts = _scene_cuts(proxy, clip.start, duration)
    count = min(MAX_SAMPLES, max(1, int(math.ceil(last - first)) + 1))
    times = [first + (last - first) * i / max(1, count - 1) for i in range(count)]
    anchors = [min(last, clip.start+cut+.25) for cut in scene_cuts]
    times = sorted(set(times + anchors))
    if len(times) > MAX_SAMPLES:
        anchor_set = set(anchors)
        regular = [t for t in times if t not in anchor_set]
        keep = max(0, MAX_SAMPLES-len(anchor_set))
        if keep and regular:
            regular = [regular[round(i*(len(regular)-1)/max(1,keep-1))] for i in range(keep)]
        else:
            regular = []
        times = sorted(set(regular+anchors))[:MAX_SAMPLES]
    cascade_path = Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'
    detector = cv2.CascadeClassifier(str(cascade_path)) if cascade_path and cascade_path.is_file() else None
    if detector is not None and detector.empty():
        detector = None

    samples = []
    previous_hist = None
    scene_changes = []
    frame_width = frame_height = 0
    try:
        for absolute in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, absolute * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            frame_width, frame_height = width, height
            scale = min(1.0, 640 / width)
            if scale < 1:
                frame = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
            roi = frame[int(frame.shape[0]*.2):int(frame.shape[0]*.86), int(frame.shape[1]*.08):int(frame.shape[1]*.92)]
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            changed = False
            if previous_hist is not None:
                hist_score = cv2.compareHist(previous_hist[0], hist, cv2.HISTCMP_BHATTACHARYYA)
                pixel_score = cv2.absdiff(previous_hist[1], roi).mean()/255
                changed = (hist_score > .20 and pixel_score > .13) or pixel_score > .16
            previous_hist = (hist, roi.copy())
            rel = max(0.0, absolute - clip.start)
            if changed:
                scene_changes.append(rel)
            faces = []
            if detector is not None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                found = detector.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(24, 24))
                fh, fw = gray.shape
                faces = [(x, y, w, h) for x, y, w, h in found if w * h >= fw * fh * .006]
            samples.append({'time': rel, 'faces': faces, 'w': frame.shape[1], 'h': frame.shape[0], 'scene': changed})
    finally:
        cap.release()
    if not samples:
        raise ValueError('Не удалось получить кадры для анализа')
    if not scene_cuts:
        scene_cuts = scene_changes

    from . import composition
    output_w, output_h = media.output_size(style, 1080)
    left, top, inner_w, inner_h = composition.geometry(style, output_w, output_h, 1080)
    vis_w, vis_h = _visible_size(frame_width, frame_height, f'{inner_w}:{inner_h}')
    # One robust face-position cluster per scene avoids a key-per-second camera
    # hunt. Suggestions hold their composition until the next detected hard cut.
    boundaries = [0.0] + scene_cuts
    keys = []
    face_samples = 0
    for index, scene_start in enumerate(boundaries):
        scene_end = boundaries[index+1] if index+1 < len(boundaries) else duration + .01
        observations = []
        for sample in samples:
            if not scene_start <= sample['time'] < scene_end:
                continue
            if sample['faces']:
                face_samples += 1
            for x, y, w, h in sample['faces']:
                cx, cy = (x+w/2)/sample['w'], (y+h*.42)/sample['h']
                zoom = _clamp(min(1.35, .38/max(w/sample['w'], h/sample['h'])), 1.0, 1.35)
                observations.append((cx, cy, zoom))
        if observations:
            clusters = {}
            for observation in observations:
                cell = (round(observation[0]/.2), round(observation[1]/.2))
                clusters.setdefault(cell, []).append(observation)
            dominant = max(clusters.values(), key=len)
            cx = sorted(item[0] for item in dominant)[len(dominant)//2]
            cy = sorted(item[1] for item in dominant)[len(dominant)//2]
            zoom = sorted(item[2] for item in dominant)[len(dominant)//2]
            key = {'time': scene_start, 'x': _offset(cx, vis_w/zoom), 'y': _offset(cy, vis_h/zoom), 'zoom': zoom}
        else:
            key = {'time': scene_start, 'x': .5, 'y': .5, 'zoom': 1.0}
        key['transition'] = 'cut' if not keys or scene_cuts else 'linear'
        keys.append(key)
    if len(keys) > 60:
        step = (len(keys)-1)/59
        keys = [keys[round(i*step)] for i in range(60)]
    # Keep required structure and deterministic key ordering.
    frame_aligned = []
    for key in keys:
        time_seconds = min(duration, round(key['time']*30)/30)
        converted = {'time': time_seconds, 'x': round(key['x'], 4), 'y': round(key['y'], 4),
                     'zoom': round(key['zoom'], 3), 'transition': key['transition']}
        if frame_aligned and converted['time'] <= frame_aligned[-1]['time']:
            frame_aligned[-1] = converted
        else:
            frame_aligned.append(converted)
    keys = frame_aligned
    if face_samples:
        method = 'face-and-scene'
        note = f"Центрирование по преобладающему кластеру лиц внутри сцены; кадров с лицом: {face_samples}, смен сцен: {len(scene_cuts)}. Это не определение говорящего. Проверьте кадрирование вручную."
    else:
        method = 'scene-only'
        note = f"Лица не обнаружены; центральное кадрирование удерживается между сценами ({len(scene_cuts)} смен). Проверьте вручную."
    return {'camera': keys, 'method': method, 'note': note}
