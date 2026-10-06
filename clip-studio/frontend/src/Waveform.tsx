import { useEffect, useMemo, useRef, useState } from 'react';

type Envelope = { start: number; end: number; step: number; has_audio: boolean; peaks: number[]; rms: number[] };
type Key = { time: number };
const cache = new Map<string, Promise<Envelope>>();
const waiting: (() => void)[] = [];
let activeRequests = 0;
async function loadEnvelope(key: string) {
  if (!cache.has(key)) {
    const request = (async () => {
      if (activeRequests >= 2) await new Promise<void>(resolve => waiting.push(resolve));
      else activeRequests++;
      try {
        const response = await fetch(key);
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || 'Не удалось прочитать звук');
        }
        return await response.json() as Envelope;
      } finally {
        const next = waiting.shift();
        if (next) next(); else activeRequests--;
      }
    })();
    cache.set(key, request);
    request.catch(() => cache.delete(key));
    if (cache.size > 40) cache.delete(cache.keys().next().value!);
  }
  return cache.get(key)!;
}
const stamp = (seconds: number) => `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toFixed(1).padStart(4, '0')}`;

export function Waveform({ assetId, start, end, playhead, onSeek, muted = false, compact = false, cameraKeys = [], selection }: {
  assetId: string; start: number; end: number; playhead?: number; onSeek?: (time: number) => void;
  muted?: boolean; compact?: boolean; cameraKeys?: Key[]; selection?: { start?: number; end?: number } | null;
}) {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState(start);
  const [data, setData] = useState<{ key: string; value: Envelope } | null>(null);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const [retry, setRetry] = useState(0);
  const dragging = useRef(false);
  const span = Math.max(.001, (end - start) / zoom);
  const left = Math.max(start, Math.min(end - span, zoom === 1 ? start : offset));
  const right = Math.min(end, left + span);
  const key = `/api/assets/${assetId}/audio/waveform?start=${left.toFixed(4)}&end=${right.toFixed(4)}&bins=${compact ? 160 : 1000}`;
  useEffect(() => { setZoom(1); setOffset(start); }, [assetId, start, end]);
  useEffect(() => {
    let stopped = false;
    if (end <= start) return;
    setError(null);
    loadEnvelope(key).then(value => { if (!stopped) setData({ key, value }); }).catch(e => {
      if (!stopped) setError({ key, message: e instanceof Error ? e.message : 'Звук недоступен' });
    });
    return () => { stopped = true; };
  }, [key, retry]);
  useEffect(() => {
    if (zoom === 1 || playhead === undefined) return;
    if (playhead < left || playhead > right) setOffset(Math.max(start, Math.min(end - span, playhead - span * .15)));
  }, [playhead]);
  const sound = data?.key === key ? data.value : null;
  const path = useMemo(() => {
    if (!sound) return '';
    const n = sound.peaks.length;
    const top = sound.peaks.map((p, i) => `${(i + .5) * 1000 / n},${30 - Math.sqrt(p) * 27}`);
    const bottom = sound.peaks.map((p, i) => `${(i + .5) * 1000 / n},${30 + Math.sqrt(p) * 27}`).reverse();
    return `M0,30 L${top.join(' L')} L1000,30 L${bottom.join(' L')} Z`;
  }, [sound]);
  const pct = (t: number) => Math.max(0, Math.min(100, (t - left) / span * 100));
  const seekPointer = (event: React.PointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    onSeek?.(Math.max(left, Math.min(right, left + (event.clientX - rect.left) / Math.max(1, rect.width) * span)));
  };
  return <div className={`audio-waveform ${compact ? 'compact' : ''} ${muted ? 'muted' : ''}`}>
    {!compact && <div className="waveform-heading"><strong>Звуковая дорожка <small>{muted ? 'Звук выключен' : sound && !sound.has_audio ? 'Без аудио' : 'Клик / перетаскивание → перемотка'}</small></strong><div><button type="button" aria-label="Предыдущий участок звука" disabled={left <= start} onClick={() => setOffset(Math.max(start, left - span * .75))}>←</button><select aria-label="Масштаб звуковой дорожки" value={zoom} onChange={e => { const z = Number(e.target.value); setZoom(z); setOffset(Math.max(start, (playhead ?? left) - (end - start) / z / 2)); }}><option value={1}>Весь участок</option><option value={4}>×4</option><option value={16}>×16</option><option value={64}>×64</option></select><button type="button" aria-label="Следующий участок звука" disabled={right >= end - .001} onClick={() => setOffset(Math.min(end - span, left + span * .75))}>→</button></div></div>}
    <div className="waveform-body">
      <svg viewBox="0 0 1000 60" preserveAspectRatio="none" role={onSeek ? 'slider' : 'img'} tabIndex={onSeek ? 0 : undefined}
        aria-label={compact ? 'Звуковая волна фрагмента' : 'Перемотка по звуковой дорожке'} aria-valuemin={onSeek ? start : undefined} aria-valuemax={onSeek ? end : undefined}
        aria-valuenow={onSeek ? Math.max(start, Math.min(end, playhead ?? start)) : undefined} aria-valuetext={onSeek ? stamp(playhead ?? start) : undefined}
        onPointerDown={onSeek ? e => { dragging.current = true; e.currentTarget.setPointerCapture(e.pointerId); seekPointer(e); } : undefined}
        onPointerMove={onSeek ? e => { if (dragging.current) seekPointer(e); } : undefined}
        onPointerUp={() => { dragging.current = false; }} onPointerCancel={() => { dragging.current = false; }}
        onKeyDown={onSeek ? e => { const amount = e.shiftKey ? 1 : .1; let t = playhead ?? start; if (e.key === 'ArrowLeft') t -= amount; else if (e.key === 'ArrowRight') t += amount; else if (e.key === 'Home') t = start; else if (e.key === 'End') t = end; else return; e.preventDefault(); onSeek(Math.max(start, Math.min(end, t))); } : undefined}>
        <line x1="0" y1="30" x2="1000" y2="30" className="waveform-zero" />
        {selection?.start !== undefined && <rect x={pct(selection.start) * 10} y="0" width={Math.max(0, pct(selection.end ?? right) - pct(selection.start)) * 10} height="60" className="waveform-selection" />}
        <path d={path} className="waveform-fill" />
        {cameraKeys.filter(k => start + k.time >= left && start + k.time <= right).map((k, i) => <line key={i} x1={pct(start + k.time) * 10} x2={pct(start + k.time) * 10} y1="0" y2="12" className="waveform-camera" />)}
        {playhead !== undefined && playhead >= left && playhead <= right && <line x1={pct(playhead) * 10} x2={pct(playhead) * 10} y1="0" y2="60" className="waveform-playhead" />}
      </svg>
      {!sound && <span className="waveform-status" role="status">{error?.key === key ? <>{compact ? 'Звук недоступен' : error.message}{!compact && <button type="button" onClick={() => { cache.delete(key); setRetry(n => n + 1); }}>Повторить</button>}</> : 'Читаем звук…'}</span>}
      {sound && !sound.has_audio && <span className="waveform-status">Без звука</span>}
    </div>
    {!compact && <div className="waveform-ruler">{[0, .25, .5, .75, 1].map(f => <span key={f}>{stamp(left + span * f)}</span>)}</div>}
  </div>;
}
