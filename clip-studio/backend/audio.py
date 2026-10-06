"""Bounded, streaming audio envelopes shared by the editor and camera analysis."""
import math
import os
import select
import subprocess
import time

import numpy as np

from . import media, store

SAMPLE_RATE = 8000


def envelope(asset, start, end, bins):
    duration = end - start
    if not all(math.isfinite(v) for v in (start, end)) or start < 0 or duration <= 0 or end > asset['duration'] + .05:
        raise ValueError('Звуковая дорожка выходит за границы видео')
    bins = max(1, min(4000, int(bins)))
    frames_per_bin = max(1, math.ceil(duration * SAMPLE_RATE / bins))
    step = frames_per_bin / SAMPLE_RATE
    count = math.ceil(duration / step)
    result = {'start': start, 'end': end, 'step': step, 'has_audio': bool(asset.get('has_audio')),
              'peaks': [0.0] * count, 'rms': [0.0] * count}
    if not result['has_audio']:
        return result
    folder = store.DATA / 'assets' / asset['id']
    # Use the original audio: the preview's stereo-to-mono conversion can hide
    # out-of-phase channels, and rebuilding a proxy must not alter the envelope.
    source = folder / asset['original']
    if not source.is_file():
        raise ValueError('Исходная звуковая дорожка не найдена')
    command = [media.FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', '-ss', str(start),
               '-i', str(source), '-t', str(duration), '-map', '0:a:0', '-vn',
               '-af', f'aresample={SAMPLE_RATE}:async=1:first_pts=0', '-ac', '2',
               '-ar', str(SAMPLE_RATE), '-f', 'f32le', 'pipe:1']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    pending = bytearray()
    bin_bytes = frames_per_bin * 2 * 4
    index = 0
    deadline = time.monotonic() + 120

    def record(raw):
        nonlocal index
        values = np.frombuffer(raw, dtype='<f4')
        values = np.nan_to_num(values, nan=0, posinf=1, neginf=-1)
        result['peaks'][index] = round(min(1.0, float(np.max(np.abs(values)))), 5)
        result['rms'][index] = round(min(1.0, float(np.sqrt(np.mean(values * values)))), 5)
        index += 1

    try:
        while index < count:
            if time.monotonic() >= deadline:
                raise ValueError('Анализ звука занял слишком много времени. Выберите более короткий участок.')
            if not select.select([process.stdout], [], [], .5)[0]:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            pending.extend(chunk)
            while len(pending) >= bin_bytes and index < count:
                record(bytes(pending[:bin_bytes]))
                del pending[:bin_bytes]
        if pending and index < count:
            usable = len(pending) // 8 * 8
            if usable:
                record(bytes(pending[:usable]))
        # Drain the pipe if the container has a few trailing AAC samples.
        process.communicate(timeout=max(.1, deadline - time.monotonic()))
        if process.returncode:
            raise ValueError('Не удалось прочитать звуковую дорожку')
    except subprocess.TimeoutExpired:
        raise ValueError('Не удалось завершить анализ звука') from None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
    return result
