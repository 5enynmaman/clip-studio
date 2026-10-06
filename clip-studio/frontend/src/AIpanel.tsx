import { useEffect, useState } from 'react';
import { GearSix, Play, Plus, Sparkle, SpinnerGap } from '@phosphor-icons/react';

export type Proposal = { id: string; start: number; end: number; title: string; reason: string; hook: string; topic: string };
type Config = { provider: 'openai' | 'gemini' | 'groq'; groq_ready:boolean; groq_model:string; openai_ready: boolean; gemini_ready: boolean; openai_model: string; gemini_model: string; auto_analyze: boolean };
type Result = { summary: string; clips: Proposal[]; provider: string; start: number; end: number; discarded: number };
const clock = (n: number) => `${Math.floor(n / 60).toString().padStart(2, '0')}:${Math.floor(n % 60).toString().padStart(2, '0')}`;
async function request<T>(path: string, body?: unknown, method = 'POST'): Promise<T> {
  const response = await fetch(`/api${path}`, body ? { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте поля запроса');
  return data;
}

export function AIpanel({ asset, revision, busy, onPreview, onAdd, onTitle, onError, onQueued }: { asset?: { id: string; duration: number }; revision: string; busy: boolean; onPreview: (p: Proposal) => void; onAdd: (p: Proposal) => void; onTitle: (text: string) => void; onError: (e: unknown) => void; onQueued: () => void }) {
  const [config, setConfig] = useState<Config>();
  const [showSettings, setShowSettings] = useState(false);
  const [key, setKey] = useState('');
  const [focus, setFocus] = useState('');
  const [start, setStart] = useState('0');
  const [end, setEnd] = useState('0');
  const [seconds, setSeconds] = useState(60);
  const [result, setResult] = useState<Result | null>(null);
  const [saving, setSaving] = useState(false);
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState('');
  useEffect(() => { request<Config>('/ai/settings').then(setConfig).catch(onError); }, []);
  useEffect(() => { setStart('0'); setEnd(String(Math.floor(Math.min(asset?.duration || 0, 1800) * 100) / 100)); setResult(null); }, [asset?.id]);
  useEffect(() => {
    let cancelled = false;
    if (asset) request<Result | null>(`/assets/${asset.id}/analysis/result`).then(r => { if (!cancelled) setResult(r); }).catch(onError);
    return () => { cancelled = true; };
  }, [asset?.id, revision]);
  const providerReady = config?.[`${config.provider}_ready`];
  async function save() {
    if (!config) return;
    setSaving(true); setNotice('');
    try {
      setConfig(await request<Config>('/ai/settings', { provider: config.provider, openai_model: config.openai_model, gemini_model: config.gemini_model, groq_model:config.groq_model, auto_analyze: config.auto_analyze, [`${config.provider}_key`]: key }, 'PUT'));
      setKey(''); setNotice('Настройки сохранены. Доступ к модели проверится при анализе.');
    } catch (e) { onError(e); } finally { setSaving(false); }
  }
  async function analyze() {
    if (!asset || !config) return;
    const begin = Number(start), finish = Number(end);
    if (!Number.isFinite(begin) || !Number.isFinite(finish) || begin < 0 || finish <= begin || finish > asset.duration || finish-begin > 1800) { onError(new Error('Выберите участок в пределах видео, не длиннее 30 минут')); return; }
    setSending(true);
    try { await request(`/assets/${asset.id}/analyze`, { provider: config.provider, focus, start: begin, end: finish, clip_seconds: seconds }); onQueued(); }
    catch (e) { onError(e); } finally { setSending(false); }
  }
  return <div className="ai-panel">
    <div className="ai-intro"><Sparkle size={23} /><h3>Найдём сильные моменты</h3><p>ИИ предложит фрагменты. Вы сможете просмотреть каждый и решить, что добавить в монтаж.</p></div>
    {config && <><div className="setting-group"><label>Провайдер анализа</label><div className="segmented">{(['groq', 'gemini', 'openai'] as const).map(p => <button key={p} className={config.provider === p ? 'active' : ''} onClick={() => { setConfig({ ...config, provider: p }); setKey(''); }}>{p === 'groq'?'Groq':p === 'gemini' ? 'Gemini' : 'OpenAI'}</button>)}</div><p className="hint">{config.provider==='groq'?'Распознаёт речь и выбирает фрагменты по смыслу. Есть бесплатный тариф с квотами; визуальные действия не анализирует.':config.provider === 'gemini' ? 'Смотрит видео и слышит звук. Подходит для поиска действий.' : 'Анализирует речь и кадры. Быстрые действия лучше проверить вручную.'}</p></div>
    <button className="text-button" onClick={() => setShowSettings(!showSettings)}><GearSix size={16} />{showSettings ? 'Скрыть настройки' : 'Ключ и настройки ИИ'}<span className={`ai-key-status ${providerReady ? 'ready' : ''}`}>{providerReady ? 'Ключ есть' : 'Нужен ключ'}</span></button>
    {showSettings && <div className="ai-config"><label htmlFor="ai-key">API-ключ {config.provider==='groq'?'Groq':config.provider === 'gemini' ? 'Gemini' : 'OpenAI'}</label><input id="ai-key" type="password" autoComplete="new-password" placeholder={providerReady ? 'Оставьте пустым, чтобы сохранить ключ' : 'Вставьте API-ключ'} value={key} onChange={e => setKey(e.target.value)} /><label htmlFor="ai-model">Модель</label><input id="ai-model" value={config[`${config.provider}_model`]} onChange={e => setConfig({ ...config, [`${config.provider}_model`]: e.target.value })} /><label className="ai-auto"><input type="checkbox" checked={config.auto_analyze} onChange={e => setConfig({ ...config, auto_analyze: e.target.checked })} />Анализировать новые загрузки автоматически</label><p className="hint">Автоанализ: первые 30 минут через сохранённого провайдера. Видео или речь отправляются в его API; расходуется квота вашего ключа. Автоматического перехода к другому провайдеру нет. Ключ хранится на сервере.</p><button className="secondary full-width" disabled={saving} onClick={save}>{saving && <SpinnerGap size={16} className="spin" />}Сохранить настройки ИИ</button>{notice && <p role="status" className="hint">{notice}</p>}</div>}
    <div className="setting-group"><label htmlFor="ai-focus">Что искать</label><textarea id="ai-focus" rows={3} maxLength={800} value={focus} onChange={e => setFocus(e.target.value)} placeholder="Например: смешные реакции, спор о камере, момент с распаковкой" /></div>
    <div className="ai-window"><label>Начало, сек.<input type="number" min={0} step={.1} value={start} onChange={e => setStart(e.target.value)} /></label><label>Конец, сек.<input type="number" min={0} step={.1} value={end} onChange={e => setEnd(e.target.value)} /></label></div><p className="hint">До 30 минут за один анализ. Для длинного видео выберите следующий участок.</p>
    <div className="setting-group"><label>Длина предложений</label><div className="segmented">{[30, 60, 90].map(s => <button className={seconds === s ? 'active' : ''} onClick={() => setSeconds(s)} key={s}>{s} сек.</button>)}</div></div>
    <button className="primary full-width" disabled={!asset || !providerReady || busy || sending} onClick={analyze}>{busy || sending ? <SpinnerGap size={18} className="spin" /> : <Sparkle size={18} />}{busy ? 'ИИ анализирует видео…' : 'Предложить клипы'}</button>
    {!providerReady && <p className="hint">Добавьте ключ в настройках выше, чтобы запустить анализ.</p>}
    {result && <div className="ai-results"><h3>Предложения ИИ <span>{result.clips.length}</span></h3><p className="hint">{result.summary}</p><p className="hint">{result.provider==='groq'?'Groq':result.provider === 'gemini' ? 'Gemini' : 'OpenAI'} · {clock(result.start)}—{clock(result.end)}</p>{result.clips.map(p => <article className="proposal" key={p.id}><div className="proposal-meta"><span>{p.topic}</span><strong>{clock(p.start)}—{clock(p.end)}</strong></div><h4>{p.title}</h4><p>{p.reason}</p>{p.hook && <blockquote>{p.hook}<button className="text-button" onClick={() => onTitle(p.hook)}>В заголовок</button></blockquote>}<div className="proposal-actions"><button className="secondary" onClick={() => onPreview(p)}><Play size={15} />Просмотр</button><button className="primary" onClick={() => onAdd(p)}><Plus size={15} />В монтаж</button></div></article>)}{!result.clips.length && <p className="hint">Подходящие моменты не найдены. Уточните тему или выберите другой участок.</p>}{result.discarded > 0 && <p className="hint">Некорректные таймкоды исключены: {result.discarded}.</p>}</div>}</>}
  </div>;
}
