import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowDown, ArrowUp, ArrowsOut, Check, CheckCircle, Clock, CloudArrowUp, DownloadSimple, FilmStrip, FolderOpen, FrameCorners, GearSix, InstagramLogo, Link, MagnifyingGlass, Pause, Play, Plus, Scissors, SlidersHorizontal, SpeakerHigh, SpeakerSlash, Sparkle, SpinnerGap, Stack, Subtitles, TextT, TiktokLogo, Trash, VideoCamera, X } from '@phosphor-icons/react';
import { AIpanel, type Proposal } from './AIpanel';
import { Canvas } from './Canvas';
import { Waveform } from './Waveform';
import { cameraValueAt, resizeClip } from './editor';
import './style.css';
import '@fontsource/manrope/400.css';
import '@fontsource/manrope/500.css';
import '@fontsource/manrope/600.css';
import '@fontsource/manrope/700.css';
import '@fontsource/manrope/800.css';

export type Asset = { id: string; title: string; source: string; original?:string; original_mime?:string; original_codec?:string; browser_playable?:boolean; has_audio?:boolean; duration: number; width: number; height: number; hdr: boolean; transfer: string; bit_depth: number; codec: string; size: number; preview_height?:number; preview_width?:number; preview_hdr?:boolean; preview_revision?:string };
type RuntimeHealth={runtime:'desktop'|'server';renderer?:string;parallel_quick_jobs?:boolean;preview?:{original?:string;proxy?:string;source?:string}};
export type CameraKey = { time: number; x: number; y: number; zoom: number; transition: 'cut' | 'linear' };
export type Clip = { id: string; asset_id: string; start: number; end: number; label: string; crop_x: number; camera?: CameraKey[] };
export type Box = {x:number;y:number;w:number;h:number};
export type Style = { font: 'manrope'|'montserrat'|'russoone'|'lora'|'robotomono'|'cormorantgaramond'|'cormorantitalic'|'prata'|'tenorsans'|'poiretone'|'greatvibes'|'badscript'|'unbounded'; header_image?:string|null; video_box?:Box|null; header_box?:Box|null; title_box?:Box|null; eyebrow_box?:Box|null; account_box?:Box|null; layout: 'plain' | 'card'; corner_radius: number; header_height: number; account: string; eyebrow: string; text_color: string; aspect: '9:16' | '1:1' | '16:9'; fit: 'cover' | 'contain'; border: 0 | 24 | 48; background: string; title: string; title_position: 'top' | 'center' | 'bottom'; font_size: number; subtitles: boolean; mute: boolean };
type Project = { name: string; clips: Clip[]; style: Style; caption: string; color_mode?: 'sdr'|'hdr' };
type SavedProject = Project & { id: string };
type Job = { lane?:'main'|'quick'; id: string; kind: string; status: 'queued' | 'running' | 'cancelling' | 'ready' | 'error' | 'cancelled' | 'dismissed'; progress: number; message: string; context?:{title?:string;asset_id?:string;clip_label?:string;range?:{start:number;end:number};clip_range?:{start:number;end:number};source_time?:number;clip_count?:number}; result?: { asset_id?: string; url?: string; name?: string; mode?: string; metadata?: Asset; title?:string; duration?:number; qualities?:{height:number;hdr:boolean;codec:string;estimated_bytes?:number}[]; camera?: CameraKey[]; method?:string; note?:string; segments?:{time:number;participant:number}[]; time?:number; width?:number; height?:number } };
type Cue = { start: number; end: number; text: string };
const fresh = (): Project => ({ name: 'Новый ролик', clips: [], style: { font:'manrope', layout: 'plain', corner_radius: 0, header_height: 300, account: '', eyebrow: '', text_color: '#ffffff', aspect: '9:16', fit: 'cover', border: 0, background: '#101116', title: '', title_position: 'bottom', font_size: 48, subtitles: false, mute: false }, caption: '', color_mode:'sdr' });
const EMPTY_CAMERA_KEYS: CameraKey[] = [];
const time = (n: number) => `${Math.floor(n / 60).toString().padStart(2, '0')}:${Math.floor(n % 60).toString().padStart(2, '0')}`;
const size = (n: number) => `${(n / 1048576).toFixed(1)} МБ`;
const mediaURL = (id: string, variant: string) => `/api/assets/${id}/${variant}`;

function TrimInput({ label, value, onCommit }: { label: string; value: number; onCommit: (value: number) => void }) {
  const [draft, setDraft] = useState(String(Number(value.toFixed(2))));
  useEffect(() => setDraft(String(Number(value.toFixed(2)))), [value]);
  return <label>{label}<input type="number" min={0} step={.1} value={draft} onChange={e => setDraft(e.target.value)} onBlur={() => { if (draft.trim() && Number.isFinite(Number(draft))) onCommit(Number(draft)); setDraft(String(Number(value.toFixed(2)))); }} onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }} /></label>;
}

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`/api${url}`, options); }
  catch { throw new Error('Сервер недоступен. Проверьте подключение и запуск сервера.'); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const message = Array.isArray(data.detail) ? data.detail.map((x: { msg: string }) => x.msg).join('. ') : data.detail;
    throw new Error(message || `Не удалось выполнить запрос (${response.status})`);
  }
  return response.json();
}
const json = (body: unknown, method = 'POST'): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

function Studio() {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [project, setProjectState] = useState<Project>(fresh);
  const history=useRef<{past:Project[];future:Project[]}>({past:[],future:[]});
  const lastEdit=useRef({group:'',at:0});
  const [selectionTarget,setSelectionTarget]=useState<'clip'|'key'|'range'|null>(null);
  const [editRange,setEditRange]=useState<{clipId:string;start?:number;end?:number}|null>(null);
  const [projectId, setProjectId] = useState(() => localStorage.getItem('clip-project') || crypto.randomUUID());
  const [saved, setSaved] = useState<SavedProject[]>([]);
  const [ready, setReady] = useState(false);
  const [online, setOnline] = useState(false);
  const [runtimeHealth,setRuntimeHealth]=useState<RuntimeHealth|null>(null);
  const [originalFallbackAsset,setOriginalFallbackAsset]=useState('');
  const [saveState, setSaveState] = useState('');
  const [view, setView] = useState<'edit' | 'projects' | 'exports'>('edit');
  const [panel, setPanel] = useState<'frame' | 'text' | 'captions' | 'ai'>('frame');
  const [assetId, setAssetId] = useState('');
  const [analysisPreview, setAnalysisPreview] = useState<{ start: number; end: number } | null>(null);
  const [selected, setSelected] = useState('');
  const [url, setUrl] = useState('');
  const [importing, setImporting] = useState(false);
  const [inspectId,setInspectId]=useState('');
  const [quality,setQuality]=useState<360|480|720|1080|2160>(1080);
  const [preferHdr,setPreferHdr]=useState(false);
  const [autoAnalyze,setAutoAnalyze]=useState(true);
  const [disk,setDisk]=useState<{used:number;budget:number;max_upload_mb:number;retention_days:number}>();
  const [confirmDelete,setConfirmDelete]=useState<Asset|null>(null);
  const photoInput=useRef<HTMLInputElement>(null);
  const inspected=jobs.find(j=>j.id===inspectId);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const toastTimer=useRef<number|undefined>(undefined);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [previewScope,setPreviewScope]=useState<'source'|'clip'>('source');
  const [sourceMarks,setSourceMarks]=useState<{assetId:string;start?:number;end?:number}|null>(null);
  const [workflowStep,setWorkflowStep]=useState<1|2|3|4>(1);
  const [previewQuality,setPreviewQuality]=useState<'max'|'draft'>('max');
  const [sourcePreview,setSourcePreview]=useState<{key:string;url:string;source_start:number;source_end:number;width:number;height:number}|null>(null);
  const [sourcePreviewStatus,setSourcePreviewStatus]=useState<{key:string;jobId?:string;status:'loading'|'error';message?:string}|null>(null);
  const [sourcePreviewRetry,setSourcePreviewRetry]=useState(0);
  const [usingSourcePreview,setUsingSourcePreview]=useState(false);
  const [sourceWindowAnchor,setSourceWindowAnchor]=useState(0);
  const [cameraAdvanced,setCameraAdvanced]=useState(false);
  const [previewPreparationEnabled,setPreviewPreparationEnabled]=useState(false);
  const [jobsOpen,setJobsOpen]=useState(false);
  const [cues, setCues] = useState<Cue[]>([]);
  const [query, setQuery] = useState('');
  const [exportOpen, setExportOpen] = useState(false);
  const mode=project.color_mode||'sdr';
  const setMode=(value:'sdr'|'hdr')=>setProject(p=>({...p,color_mode:value}));
  const [resolution, setResolution] = useState<720 | 1080>(1080);
  const [monitorZoom, setMonitorZoom] = useState<'fit'|1|2>('fit');
  const [precise, setPrecise] = useState<{key:string;url:string}|null>(null);
  const [preciseState, setPreciseState] = useState<{key:string;status:'loading'|'error'}|null>(null);
  const [cameraKeyIndex, setCameraKeyIndex] = useState(-1);
  const [cameraEdit, setCameraEdit] = useState(false);
  const [layerEdit,setLayerEdit]=useState(false);
  const [suggestJob, setSuggestJob] = useState('');
  const [suggestSubmitting, setSuggestSubmitting] = useState(false);
  const [suggestion, setSuggestion] = useState<{camera:CameraKey[];method?:string;note?:string;segments?:{time:number;participant:number}[];clipId:string;fingerprint:string}|null>(null);
  const [suggestRequest,setSuggestRequest]=useState<{jobId:string;clipId:string;fingerprint:string}|null>(null);
  const [exactRequested,setExactRequested]=useState<{jobId:string;key:string}|null>(null);
  const [exactOpen,setExactOpen]=useState(false);
  const [exactResult,setExactResult]=useState<{key:string;url:string}|null>(null);
  const [exactJobStatus,setExactJobStatus]=useState<Job|null>(null);
  const [exactError,setExactError]=useState('');
  const [exactSubmitting,setExactSubmitting]=useState(false);
  const [preciseRetry,setPreciseRetry]=useState(0);
  const [suggestJobStatus,setSuggestJobStatus]=useState<Job|null>(null);
  const preciseCache = useRef(new Map<string,string>());
  const requestedFrames = useRef(new Set<string>());
  const frameJobIds = useRef(new Map<string,string>());
  const frameTimer = useRef<number|undefined>(undefined);
  const latestPreciseKey = useRef('');
  const [exporting, setExporting] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<'media' | 'settings' | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const srtInput = useRef<HTMLInputElement>(null);
  const booted = useRef(false);
  const latestProject = useRef(project);
  latestProject.current=project;
  const continuePlayback = useRef(false);
  const playheadRef = useRef(playhead);
  playheadRef.current=playhead;
  const advancedClip = useRef('');
  const jobEpoch = useRef(0);
  const selectedClip = project.clips.find(c => c.id === selected);
  const cameraKeys = selectedClip?.camera || EMPTY_CAMERA_KEYS;
  const clipIndex = project.clips.findIndex(c=>c.id===selected);
  const clipFrames30=(clip:Clip)=>Math.max(1,Math.ceil((clip.end-clip.start)*30-1e-8));
  const currentOutputTime = project.clips.slice(0,Math.max(0,clipIndex)).reduce((frames,c)=>frames+clipFrames30(c),0)/30
    + (selectedClip&&previewScope==='clip'?Math.min(clipFrames30(selectedClip)-1,Math.max(0,Math.round((playhead-selectedClip.start)*30)))/30:0);
  const localCameraTime = previewScope==='clip'?Math.max(0,playhead-(selectedClip?.start||0)):Math.max(0,playhead);
  const currentCamera = cameraValueAt(selectedClip,localCameraTime);
  const activeAsset = assets.find(a => a.id === (selectedClip?.asset_id || assetId));
  const originalVideoMime=(asset:Asset)=>{const ext=(asset.original||'').split('.').pop()?.toLowerCase();return ({mp4:'video/mp4',m4v:'video/mp4',mov:'video/quicktime',webm:'video/webm',mkv:'video/x-matroska'} as Record<string,string>)[ext||'']||'';};
  const originalCodec=(asset:Asset)=>({h264:'avc1.42E01E',avc:'avc1.42E01E',hevc:'hvc1.1.6.L93.B0',h265:'hvc1.1.6.L93.B0',av1:'av01.0.08M.08',vp9:'vp09.00.10.08',vp8:'vp8'} as Record<string,string>)[asset.codec.toLowerCase()]||'';
  const originalMime=activeAsset?.original_mime||(activeAsset?originalVideoMime(activeAsset):'');
  const originalCodecString=activeAsset?originalCodec(activeAsset.original_codec?{...activeAsset,codec:activeAsset.original_codec}:activeAsset):'';
  const originalBrowserSupported=Boolean(activeAsset?.browser_playable&&originalMime&&originalCodecString&&document.createElement('video').canPlayType(`${originalMime}; codecs="${originalCodecString}"`));
  const useDesktopOriginal=Boolean(previewQuality==='max'&&runtimeHealth?.runtime==='desktop'&&runtimeHealth.preview?.original==='byte-range'&&activeAsset&&originalBrowserSupported&&originalFallbackAsset!==activeAsset.id);
  const originalUrl=useDesktopOriginal&&activeAsset?mediaURL(activeAsset.id,'original'):undefined;
  const duration = project.clips.reduce((n, c) => n + c.end - c.start, 0);
  const activeJobs = jobs.filter(j => ['queued','running','cancelling'].includes(j.status));
  const previewKinds=new Set(['preview-source','preview-frame','preview-video','reframe']);
  const jobTitle=(kind:string)=>({export:'Экспорт видео',analyze:'Анализ видео',prepare:'Подготовка исходника','prepare-playback':'Улучшение предпросмотра',youtube:'Загрузка с YouTube','youtube-info':'Проверка YouTube','preview-source':'Высококачественное окно исходника','preview-frame':'Точный кадр на паузе','preview-video':'Полный точный просмотр',reframe:'План кадрирования'} as Record<string,string>)[kind]||'Обработка видео';
  const jobStatus=(job:Job)=>job.status==='queued'?'В очереди':job.status==='running'?'Выполняется':job.status==='cancelling'?'Отменяется':job.status==='cancelled'?'Отменено':job.status==='ready'?'Готово':job.status==='error'?'Ошибка':'Закрыто';
  const exports = jobs.filter(j => j.kind === 'export');
  const style = project.style;
  const outputWidth=style.aspect==='9:16'?resolution:style.aspect==='1:1'?resolution:Math.round(resolution*16/9);
  const outputHeight=style.aspect==='9:16'?Math.round(resolution*16/9):resolution;
  const rangeStart = previewScope==='clip'&&selectedClip?selectedClip.start:analysisPreview?.start??0;
  const rangeEnd = previewScope==='clip'&&selectedClip?selectedClip.end:analysisPreview?.end??activeAsset?.duration??0;
  const currentSourceMarks=sourceMarks?.assetId===activeAsset?.id?sourceMarks:null;
  const currentEditRange=editRange?.clipId===selectedClip?.id?editRange:null;
  const hasEditRange=Boolean(currentEditRange?.start!==undefined&&currentEditRange.end!==undefined&&currentEditRange.end>currentEditRange.start);
  const previewShortEdge=activeAsset?.preview_width&&activeAsset?.preview_height?Math.min(activeAsset.preview_width,activeAsset.preview_height):activeAsset?.preview_height?Math.round(activeAsset.preview_height*(activeAsset.height>activeAsset.width?activeAsset.width/Math.max(1,activeAsset.height):1)):0;
  const playbackUpgradeJob=jobs.find(j=>j.kind==='prepare-playback'&&j.context?.asset_id===activeAsset?.id&&['queued','running','cancelling'].includes(j.status));
  const needsPlaybackUpgrade=Boolean(runtimeHealth?.runtime==='desktop'&&activeAsset&&previewShortEdge>0&&previewShortEdge<Math.min(1080,activeAsset.width,activeAsset.height));
  const boundedWindowStart=previewScope==='clip'&&selectedClip?Math.max(selectedClip.start,Math.min(selectedClip.end-.05,sourceWindowAnchor)):Math.max(0,Math.min(rangeEnd-.05,sourceWindowAnchor));
  const sourceWindowEnd=Math.min(rangeEnd,boundedWindowStart+15);
  const sourcePreviewClip:Clip|undefined=activeAsset?{id:previewScope==='clip'&&selectedClip?selectedClip.id:activeAsset.id,asset_id:activeAsset.id,start:boundedWindowStart,end:Math.max(boundedWindowStart+.05,sourceWindowEnd),label:previewScope==='clip'&&selectedClip?selectedClip.label:'Исходник',crop_x:previewScope==='clip'&&selectedClip?selectedClip.crop_x:.5,camera:[]}:undefined;
  const sourceKey=sourcePreviewClip?`${sourcePreviewClip.asset_id}:${sourcePreviewClip.start}:${sourcePreviewClip.end}:${resolution}`:'';
  const hqSourceUrl=sourcePreview?.key===sourceKey&&previewQuality==='max'&&(!playing||usingSourcePreview)?sourcePreview.url:undefined;
  const proxyUrl=activeAsset?mediaURL(activeAsset.id,'proxy')+`?v=${activeAsset.preview_revision||`${activeAsset.preview_width}x${activeAsset.preview_height}`}`:undefined;
  const monitorSourceUrl=originalUrl||hqSourceUrl||proxyUrl;
  const expectedMonitorSource=activeAsset?new URL(monitorSourceUrl||mediaURL(activeAsset.id,'proxy'),window.location.href).href:'';
  const expectedMonitorOffset=monitorSourceUrl&&monitorSourceUrl===hqSourceUrl?sourcePreview?.source_start||0:0;
  const expectedMediaRef=useRef({src:'',offset:0});
  expectedMediaRef.current={src:expectedMonitorSource,offset:expectedMonitorOffset};
  const currentCue = cues.find(c => c.start <= playhead && c.end > playhead);
  const subtitleMatches = cues.filter(c => !query || c.text.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const hdrReason = !project.clips.length ? 'Сначала добавьте фрагмент.' : project.clips.some(c => !assets.find(a => a.id === c.asset_id)?.hdr) ? 'В таймлайне есть SDR-исходники.' : new Set(project.clips.map(c => assets.find(a => a.id === c.asset_id)?.transfer)).size > 1 ? 'Нельзя смешивать PQ и HLG.' : style.title || style.subtitles || style.border || style.layout === 'card' || style.corner_radius || style.header_image || style.account || style.background !== '#101116' ? 'Для HDR уберите текст, субтитры и цветную рамку.' : '';

  const report = (e: unknown) => setError(e instanceof Error ? e.message : 'Не удалось выполнить действие');
  function sourcePreviewLoaded(){return Boolean(previewQuality==='max'&&(useDesktopOriginal&&video.current?.src===new URL(originalUrl!,window.location.href).href||sourcePreview?.key===sourceKey&&video.current?.src===new URL(sourcePreview.url,window.location.href).href));}
  async function cancelLateJob(jobId:string){try{await api(`/jobs/${jobId}/cancel`,json({}));}catch{/* It may have completed before cancellation arrived. */}}
  async function upgradePlaybackPreview(){
    if(!activeAsset)return;
    const assetId=activeAsset.id,epoch=jobEpoch.current;
    try{const result=await api<{ready?:boolean;job_id?:string}>(`/assets/${assetId}/playback/prepare`,{method:'POST'});if(epoch!==jobEpoch.current){if(result.job_id)void cancelLateJob(result.job_id);return;}await refresh();if(result.ready)notify('Предпросмотр уже подготовлен в разрешении исходника');else notify('Улучшение предпросмотра добавлено в процессы');}
    catch(e){report(e);}
  }
  async function cancelJob(job:Job){
    if(previewKinds.has(job.kind)){jobEpoch.current++;setPreviewPreparationEnabled(false);continuePlayback.current=false;if(job.kind==='preview-source')setSourcePreviewStatus({key:sourceKey,status:'error',message:'Подготовка отменена'});if(job.kind==='preview-frame'){for(const [key,id] of frameJobIds.current){if(id===job.id){frameJobIds.current.delete(key);requestedFrames.current.delete(key);preciseCache.current.delete(key);if(latestPreciseKey.current===key)setPreciseState({key,status:'error'});}}}if(exactRequested?.jobId===job.id){setExactRequested(null);setExactResult(null);setExactError('Просмотр отменён');}if(suggestRequest?.jobId===job.id)setSuggestRequest(null);}
    try{const result=await api<{job_id:string;status:Job['status'];message:string}>(`/jobs/${job.id}/cancel`,json({}));setJobs(current=>current.map(j=>j.id===job.id?{...j,status:result.status,message:result.message}:j));await refresh();}
    catch(e){report(e);}
  }
  async function cancelAllJobs(){
    jobEpoch.current++;setPreviewPreparationEnabled(false);continuePlayback.current=false;setSourcePreviewStatus(null);setExactRequested(null);setExactResult(null);setSuggestRequest(null);requestedFrames.current.clear();frameJobIds.current.clear();setPreciseState(null);
    try{await api('/jobs/cancel-all',json({}));await refresh();}
    catch(e){report(e);}
  }
  async function stopPreviewPreparation(){
    jobEpoch.current++;setPreviewPreparationEnabled(false);continuePlayback.current=false;
    const owned=new Set<string>();if(sourcePreviewStatus?.jobId)owned.add(sourcePreviewStatus.jobId);for(const id of frameJobIds.current.values())owned.add(id);
    frameJobIds.current.clear();requestedFrames.current.clear();setSourcePreviewStatus(null);setPreciseState(null);
    const results=await Promise.allSettled([...owned].map(id=>api(`/jobs/${id}/cancel`,json({}))));
    if(results.some(r=>r.status==='rejected'))report(new Error('Не удалось отменить одну или несколько задач предпросмотра'));
    if(owned.size)await refresh().catch(report);
  }
  function togglePreviewPreparation(){if(previewPreparationEnabled)void stopPreviewPreparation();else setPreviewPreparationEnabled(true);}
  function handleMonitorError(source:string){
    if(source===expectedMediaRef.current.src&&useDesktopOriginal&&activeAsset){continuePlayback.current=playing||Boolean(video.current&&!video.current.paused);setOriginalFallbackAsset(activeAsset.id);setUsingSourcePreview(false);setError('');return;}
    if(source===expectedMediaRef.current.src)setError('Предпросмотр не загрузился. Проверьте доступность сервера.');
  }
  const notify = (message: string) => {window.clearTimeout(toastTimer.current);setToast(message);toastTimer.current=window.setTimeout(() => setToast(''), 4500);};
  function setProject(change:Project|((p:Project)=>Project),group=''){
    const previous=latestProject.current,next=typeof change==='function'?change(previous):change;
    if(next===previous)return;
    if(ready){const now=performance.now();if(!group||group!==lastEdit.current.group||now-lastEdit.current.at>600){history.current.past.push(previous);if(history.current.past.length>100)history.current.past.shift();}history.current.future=[];lastEdit.current={group,at:now};}
    latestProject.current=next;setProjectState(next);
  }
  function restoreEdit(redo=false){
    const from=redo?history.current.future:history.current.past,to=redo?history.current.past:history.current.future,next=from.pop();if(!next)return;
    to.push(latestProject.current);latestProject.current=next;setProjectState(next);lastEdit.current={group:'',at:0};setEditRange(null);setSelectionTarget('clip');
    if(!next.clips.some(c=>c.id===selected)){setSelected(next.clips[0]?.id||'');setPreviewScope(next.clips.length?'clip':'source');}
    setCameraKeyIndex(-1);notify(redo?'Изменение повторено':'Изменение отменено');
  }
  const updateStyle = (change: Partial<Style>) => setProject(p => ({ ...p, style: { ...p.style, ...change } }),`style:${Object.keys(change).join(',')}`);
  const updateClip = (id: string, change: Partial<Clip>, group='') => setProject(p => ({ ...p, clips: p.clips.map(c => c.id === id ? { ...c, ...change } : c) }),group);
  function enableCameraEdit(){
    if(!selectedClip||style.fit==='contain')return;
    video.current?.pause();setPlaying(false);setPreviewScope('clip');setExactResult(null);setLayerEdit(false);setMonitorZoom('fit');
    seek(Math.max(selectedClip.start,Math.min(selectedClip.end-1/30,playhead)));setCameraEdit(true);
  }

  async function refresh() {
    const [nextAssets, nextJobs, nextDisk] = await Promise.all([api<Asset[]>('/assets'), api<Job[]>('/jobs'), api<{used:number;budget:number;max_upload_mb:number;retention_days:number}>('/storage')]);
    setDisk(nextDisk);
    setAssets(nextAssets); setJobs(nextJobs); setOnline(true);
    if (nextAssets.length) setAssetId(id => id || nextAssets[0].id);
  }
  useEffect(() => {
    Promise.all([api<Asset[]>('/assets'), api<Job[]>('/jobs'), api<SavedProject[]>('/projects')]).then(([a, j, p]) => {
      setAssets(a); setJobs(j); setSaved(p); setOnline(true);
      const existing = p.find(x => x.id === projectId) || (!localStorage.getItem('clip-project') ? p[0] : undefined);
      if (existing) setProjectId(existing.id);
      if (existing) { const { id: _, ...body } = existing; setProject({ ...body, style: { ...fresh().style, ...body.style } }); setSelected(body.clips[0]?.id || '');setPreviewScope(body.clips.length?'clip':'source'); }
      if (a.length) setAssetId(existing?.clips[0]?.asset_id || a[0].id);
    }).catch(report).finally(() => { booted.current = true; setReady(true); });
    const timer = setInterval(() => { if (booted.current) refresh().catch(() => setOnline(false)); }, 2500);
    return () => clearInterval(timer);
  }, []);
  useEffect(()=>{api<RuntimeHealth>('/health').then(health=>{if(video.current&&!video.current.paused&&health.runtime==='desktop')continuePlayback.current=true;setRuntimeHealth(health);}).catch(()=>setRuntimeHealth(null));},[]);
  useEffect(()=>{history.current={past:[],future:[]};lastEdit.current={group:'',at:0};},[projectId]);
  useEffect(()=>{setCameraKeyIndex(-1);setEditRange(null);setSelectionTarget(selected?'clip':null);},[selected]);
  useEffect(()=>{if(previewScope!=='clip'||!selectedClip||style.fit==='contain')setCameraEdit(false);},[previewScope,selectedClip?.id,style.fit]);
  const observedProxy=useRef({assetId:'',revision:''});
  useEffect(()=>{
    const next={assetId:activeAsset?.id||'',revision:activeAsset?.preview_revision||''},old=observedProxy.current;observedProxy.current=next;
    if(old.assetId===next.assetId&&old.revision&&next.revision&&old.revision!==next.revision){continuePlayback.current=playing;setPrecise(null);setPreciseState(null);setSourcePreview(null);setSourcePreviewStatus(null);notify('Улучшенный предпросмотр готов и уже включён');}
  },[activeAsset?.id,activeAsset?.preview_revision]);

  useEffect(() => {
    latestProject.current = project;
    if (!ready || !online) return;
    localStorage.setItem('clip-project', projectId);
    setSaveState('Сохранение…');
    const timer = setTimeout(() => {
      api(`/projects/${projectId}`, json(project, 'PUT')).then(() => setSaveState('Сохранено')).catch(e => { setSaveState('Не сохранено'); report(e); });
    }, 700);
    return () => clearTimeout(timer);
  }, [project, projectId, ready, online]);

  useEffect(() => {
    let cancelled = false;
    setCues([]); setPlaying(false); setQuery('');
    if (activeAsset) api<Cue[]>(`/assets/${activeAsset.id}/transcript/cues`).then(c => { if (!cancelled) setCues(c); }).catch(report);
    return () => { cancelled = true; };
  }, [activeAsset?.id]);

  useEffect(() => { advancedClip.current='';setSourceWindowAnchor(previewScope==='clip'?selectedClip?.start??0:0); if (video.current) { video.current.pause(); seek(rangeStart); const source=video.current.currentSrc||video.current.src;if(continuePlayback.current&&source===expectedMediaRef.current.src&&video.current.readyState>=1){video.current.play().catch(()=>{});continuePlayback.current=false;}else setPlaying(false); } }, [selected, activeAsset?.id, analysisPreview?.start, analysisPreview?.end]);
  useEffect(() => { if (mode === 'hdr' && hdrReason) setMode('sdr'); }, [hdrReason]);
  useEffect(()=>{if(previewQuality==='draft')setUsingSourcePreview(false);else if(video.current&&playing){video.current.pause();setPlaying(false);}},[previewQuality]);
  useEffect(()=>{
    if(!previewPreparationEnabled||useDesktopOriginal||!sourcePreviewClip||previewQuality==='draft'||!online)return;
    const key=sourceKey,requestClip=sourcePreviewClip;let stopped=false,timer:number|undefined;
    if(sourcePreview?.key===key)return;
    setSourcePreview(null);setSourcePreviewStatus({key,status:'loading',message:'Готовим предпросмотр из оригинала'});
    const poll=(jobId:string)=>{const check=async()=>{if(stopped)return;try{const j=await api<Job>(`/jobs/${jobId}`);if(stopped)return;
      if(j.status==='ready'&&j.result?.url){const m=j.result.metadata as unknown as {width?:number;height?:number;source_start?:number;source_end?:number}|undefined;const url=j.result.url.startsWith('/api/')?j.result.url:`/api${j.result.url}`;setSourcePreview({key,url,source_start:m?.source_start??requestClip.start,source_end:m?.source_end??requestClip.end,width:m?.width??activeAsset?.width??0,height:m?.height??activeAsset?.height??0});setSourcePreviewStatus(null);return;}
      if(j.status==='error'||j.status==='cancelled'){setSourcePreviewStatus({key,status:'error',message:j.message||'Не удалось подготовить видео из оригинала'});return;}timer=window.setTimeout(check,800);
    }catch{if(!stopped)timer=window.setTimeout(check,1200);}};check();};
    const epoch=jobEpoch.current;
    api<{job_id:string}>('/preview/source',json({clip:requestClip,resolution:1080})).then(r=>{if(stopped||epoch!==jobEpoch.current){void cancelLateJob(r.job_id);return;}setSourcePreviewStatus({key,jobId:r.job_id,status:'loading',message:'Готовим окно 15 секунд из оригинала'});poll(r.job_id);}).catch(e=>{if(!stopped&&epoch===jobEpoch.current)setSourcePreviewStatus({key,status:'error',message:e instanceof Error?e.message:'Не удалось подготовить оригинал'});});
    return()=>{stopped=true;window.clearTimeout(timer);};
  },[sourceKey,previewQuality,online,sourcePreviewRetry,previewPreparationEnabled,useDesktopOriginal]);
  useEffect(() => {
    function key(e: KeyboardEvent) {
      if(e.defaultPrevented)return;
      if (e.key === 'Escape') { setExportOpen(false);setJobsOpen(false);setExactOpen(false);setMobilePanel(null);setCameraEdit(false);setLayerEdit(false);setEditRange(null);setSourceMarks(null);setSelectionTarget(selected?'clip':null);return; }
      if ((e.ctrlKey || e.metaKey) && e.code === 'KeyS') {
        e.preventDefault(); api(`/projects/${projectId}`, json(latestProject.current, 'PUT')).then(() => notify('Проект сохранён')).catch(report);
        return;
      }
      const target=e.target as HTMLElement;
      if(target.closest('input,textarea,select,[contenteditable="true"],[role="textbox"]')||view!=='edit'||exportOpen||jobsOpen||exactOpen||confirmDelete||mobilePanel)return;
      if((e.ctrlKey||e.metaKey)&&e.code==='KeyZ'){e.preventDefault();restoreEdit(e.shiftKey);return;}
      if(e.ctrlKey&&e.code==='KeyY'){e.preventDefault();restoreEdit(true);return;}
      if(e.ctrlKey||e.metaKey||e.altKey)return;
      if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();deleteSelection();return;}
      if(e.code==='Space'){e.preventDefault();void togglePlay();return;}
      if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();video.current?.pause();seek(Math.max(rangeStart,Math.min(rangeEnd,playhead+(e.key==='ArrowLeft'?-1:1)*(e.shiftKey?1:1/30))));return;}
      if(e.code==='KeyI'||e.code==='KeyO'){e.preventDefault();const which=e.code==='KeyI'?'start':'end';if(previewScope==='clip')markEditRange(which);else markSourceRange(which);return;}
      if(e.code==='KeyC'&&previewScope==='clip'){e.preventDefault();splitSelectedClip();return;}
      if(e.code==='KeyK'&&previewScope==='clip'){e.preventDefault();addOrUpdateCameraKey();return;}
      if(e.code==='KeyV'){e.preventDefault();if(cameraEdit)setCameraEdit(false);else enableCameraEdit();}
    }
    const undo=()=>{if(view==='edit'&&!exportOpen&&!jobsOpen&&!exactOpen&&!confirmDelete)restoreEdit();},redo=()=>{if(view==='edit'&&!exportOpen&&!jobsOpen&&!exactOpen&&!confirmDelete)restoreEdit(true);};
    window.addEventListener('keydown', key);window.addEventListener('clipstudio-undo',undo);window.addEventListener('clipstudio-redo',redo);
    return () => {window.removeEventListener('keydown', key);window.removeEventListener('clipstudio-undo',undo);window.removeEventListener('clipstudio-redo',redo);};
  });

  async function upload(file: File) {
    setImporting(true); setError('');
    const data = new FormData(); data.append('file', file);
    try { await api('/assets/upload', { method: 'POST', body: data }); notify('Видео отправлено на обработку'); await refresh(); }
    catch (e) { report(e); } finally { setImporting(false); }
  }
  async function importURL() { setImporting(true); setError('');
    try { await api('/assets/youtube', json({ url, max_height:quality, prefer_hdr:preferHdr, analyze:autoAnalyze })); setUrl(''); setInspectId(''); notify('Импорт добавлен в очередь'); await refresh(); }
    catch (e) { report(e); } finally { setImporting(false); }
  }
  async function inspectURL(e:React.FormEvent) {
    e.preventDefault();setImporting(true);setError('');
    try {const r=await api<{job_id:string}>('/youtube/inspect',json({url}));setInspectId(r.job_id);await refresh();}
    catch(e){report(e);}finally{setImporting(false);}
  }
  async function uploadPhoto(file:File) {
    const data=new FormData();data.append('file',file);
    try {const r=await api<{id:string}>('/graphics',{method:'POST',body:data});updateStyle({layout:'card',header_image:r.id});notify('Фото добавлено в шапку');}
    catch(e){report(e);}
  }
  async function deleteAsset() {
    if(!confirmDelete)return;
    try {await api(`/assets/${confirmDelete.id}`,{method:'DELETE'});setProject(p=>({...p,clips:p.clips.filter(c=>c.asset_id!==confirmDelete.id)}));setSelected('');setAssetId('');setConfirmDelete(null);await refresh();notify('Исходник и предпросмотр удалены');}
    catch(e){report(e);}
  }
  function addClip(start = 0, end?: number, target = activeAsset) {
    if (!target) return;
    const last = Math.min(end ?? start + 60, target.duration, start + Math.max(0, 180 - duration));
    if (last <= start) { setError('В ролике уже 180 секунд. Укоротите или удалите фрагмент.'); return; }
    const clip: Clip = { id: crypto.randomUUID(), asset_id: target.id, start, end: last, label: `Фрагмент ${project.clips.length + 1}`, crop_x: 0.5 };
    setProject(p => ({ ...p, clips: [...p.clips, clip] })); setSelected(clip.id);setPreviewScope('clip'); setAssetId(target.id); setMobilePanel(null);setWorkflowStep(2);
  }
  function chooseAsset(asset: Asset) { setAnalysisPreview(null); setSelected('');setPreviewScope('source');setCameraEdit(false);setSelectionTarget(null); setAssetId(asset.id); setMobilePanel(null); }
  function markSourceRange(which:'start'|'end'){
    if(!activeAsset)return;
    const at=Math.max(0,Math.min(activeAsset.duration,playhead));
    setSourceMarks(previous=>{const old=previous?.assetId===activeAsset.id?previous:{assetId:activeAsset.id};if(which==='start')return {assetId:activeAsset.id,start:at,end:old.end!==undefined&&old.end>at?old.end:undefined};return {assetId:activeAsset.id,start:old.start??0,end:Math.max(old.start??0,at)};});
    setSelectionTarget('range');
  }
  function addMarkedSourceRange(){if(!activeAsset||currentSourceMarks?.start===undefined||currentSourceMarks.end===undefined||currentSourceMarks.end<=currentSourceMarks.start)return;addClip(currentSourceMarks.start,currentSourceMarks.end,activeAsset);setSourceMarks(null);}
  function seek(value: number) { playheadRef.current=value;setPlayhead(value);setPrecise(null);setPreciseState(null);const previewReady=sourcePreview?.key===sourceKey;const currentSrc=video.current?.currentSrc||video.current?.src;const sourceElement=previewReady&&currentSrc===new URL(sourcePreview!.url,window.location.href).href;const originalElement=useDesktopOriginal&&currentSrc===new URL(originalUrl!,window.location.href).href;const inWindow=previewReady&&value>=sourcePreview!.source_start-.02&&value<=sourcePreview!.source_end+.02;if(!inWindow){const origin=previewScope==='clip'?selectedClip?.start??0:0;setSourceWindowAnchor(Math.max(origin,Math.min(rangeEnd-.05,origin+Math.floor(Math.max(0,value-origin)/10)*10)));}if(video.current){if(originalElement)video.current.currentTime=value;else if((usingSourcePreview||sourceElement)&&inWindow)video.current.currentTime=value-sourcePreview!.source_start;else if(usingSourcePreview&&!inWindow){continuePlayback.current=playing&&!video.current.paused;setUsingSourcePreview(false);video.current.pause();}else video.current.currentTime=value;} }
  async function togglePlay() {
    if (!video.current) return;
    setPrecise(null);setPreciseState(null);
    try {
      if (video.current.paused) { advancedClip.current=''; if (playhead >= rangeEnd || playhead < rangeStart) seek(rangeStart); await video.current.play(); }
      else video.current.pause();
    } catch { setError('Браузер не смог воспроизвести предпросмотр. Попробуйте загрузить видео заново.'); }
  }
  function progress(mediaTime:number,mediaSource:string) {
    if (!video.current) return;
    const mountedSource=video.current.currentSrc||video.current.src;
    if(mediaSource!==mountedSource||mediaSource!==expectedMediaRef.current.src)return;
    const now = mediaTime+expectedMediaRef.current.offset;playheadRef.current=now;setPlayhead(now);
    if (now >= rangeEnd && rangeEnd > 0 && !video.current.paused) {if(previewScope==='source'){video.current.pause();playheadRef.current=rangeEnd;setPlayhead(rangeEnd);}else advanceClip();}
  }
  function updateCamera(change:Partial<CameraKey>, at=localCameraTime) {
    if(!selectedClip)return;
    if(Object.values(change).some(v=>typeof v==='number'&&!Number.isFinite(v)))return;
    if(change.x!==undefined)change.x=Math.max(0,Math.min(1,change.x));if(change.y!==undefined)change.y=Math.max(0,Math.min(1,change.y));if(change.zoom!==undefined)change.zoom=Math.max(1,Math.min(3,change.zoom));
    const existing=[...(selectedClip.camera||[])];
    const index=existing.findIndex(k=>Math.abs(k.time-at)<.015);
    if(index<0&&existing.length>=60){setError('У фрагмента уже 60 точек кадрирования. Удалите точку перед добавлением новой.');return;}
    const base=index>=0?existing[index]:cameraValueAt(selectedClip,at);
    const updated={...base,...change,time:Math.max(0,Math.min(selectedClip.end-selectedClip.start,at))};
    if(index>=0)existing[index]=updated;else existing.push(updated);
    existing.sort((a,b)=>a.time-b.time);
    updateClip(selectedClip.id,{camera:existing},`camera:${selectedClip.id}:${at.toFixed(2)}`);setCameraKeyIndex(existing.indexOf(updated));setSelectionTarget('key');
  }
  function addOrUpdateCameraKey() { if(!selectedClip)return;video.current?.pause();setPlaying(false);updateCamera({},localCameraTime); notify('Точка кадрирования сохранена'); }
  function removeCameraKey(index:number) {if(!selectedClip||!cameraKeys[index])return;updateClip(selectedClip.id,{camera:cameraKeys.filter((_,i)=>i!==index)});setCameraKeyIndex(-1);setSelectionTarget('clip');notify('Точка удалена · ⌘Z / Ctrl+Z — отменить');}
  function selectCameraKey(index:number){if(!selectedClip)return;video.current?.pause();setPlaying(false);setCameraKeyIndex(index);setSelectionTarget('key');seek(selectedClip.start+cameraKeys[index].time);}
  function retimeCameraKey(value:number){
    if(!selectedClip||!cameraKeys[cameraKeyIndex]||!Number.isFinite(value))return;
    const at=Math.max(0,Math.min(selectedClip.end-selectedClip.start,value));
    if(cameraKeys.some((k,i)=>i!==cameraKeyIndex&&Math.abs(k.time-at)<.015)){setError('На этом времени уже есть точка кадрирования');return;}
    const changed={...cameraKeys[cameraKeyIndex],time:at},keys=cameraKeys.map((k,i)=>i===cameraKeyIndex?changed:k).sort((a,b)=>a.time-b.time);
    updateClip(selectedClip.id,{camera:keys});setCameraKeyIndex(keys.indexOf(changed));setSelectionTarget('key');seek(selectedClip.start+at);
  }
  function markEditRange(which:'start'|'end'){
    if(!selectedClip)return;
    const at=Math.max(selectedClip.start,Math.min(selectedClip.end,playhead));
    setEditRange(previous=>{const old=previous?.clipId===selectedClip.id?previous:{clipId:selectedClip.id};return which==='start'?{clipId:selectedClip.id,start:at,end:old.end!==undefined&&old.end>at?old.end:undefined}:{clipId:selectedClip.id,start:old.start??selectedClip.start,end:at};});
    setSelectionTarget('range');
  }
  function extendClip(which:'start'|'end',seconds:number){
    if(!selectedClip||!activeAsset)return;
    const room=Math.max(0,180-duration),available=which==='start'?selectedClip.start:activeAsset.duration-selectedClip.end,extra=Math.min(seconds,room,available);
    if(extra<=0){notify(room<=0?'В ролике уже 180 секунд':'Достигнута граница исходника');return;}
    trim(which,which==='start'?selectedClip.start-extra:selectedClip.end+extra);notify(`Добавлено ${Number(extra.toFixed(2))} сек. ${which==='start'?'перед фрагментом':'после фрагмента'}`);
  }
  function keepEditRange(){
    if(!hasEditRange||!selectedClip||!currentEditRange)return;
    try{updateClip(selectedClip.id,resizeClip(selectedClip,currentEditRange.start!,currentEditRange.end!));seek(currentEditRange.start!);setEditRange(null);setSelectionTarget('clip');notify('Границы применены · ⌘Z / Ctrl+Z — отменить');}
    catch(e){report(e);}
  }
  function removeSelectedClip(){
    if(!selectedClip)return;
    const index=project.clips.findIndex(c=>c.id===selected),clips=project.clips.filter(c=>c.id!==selected),next=clips[Math.min(index,clips.length-1)];
    setProject(p=>({...p,clips}));setSelected(next?.id||'');setPreviewScope(next?'clip':'source');setEditRange(null);setSelectionTarget(next?'clip':null);notify('Фрагмент удалён · ⌘Z / Ctrl+Z — отменить');
  }
  function splitSelectedClip(){
    if(!selectedClip||playhead<=selectedClip.start+1/30||playhead>=selectedClip.end-1/30){notify('Поставьте курсор внутри фрагмента');return;}
    try{const left=resizeClip(selectedClip,selectedClip.start,playhead),right={...resizeClip(selectedClip,playhead,selectedClip.end),id:crypto.randomUUID(),label:selectedClip.label.slice(0,64)+' · продолжение'};
      setProject(p=>({...p,clips:p.clips.flatMap(c=>c.id===selectedClip.id?[left,right]:[c])}));setSelected(right.id);setEditRange(null);setSelectionTarget('clip');notify('Фрагмент разделён');
    }catch(e){report(e);}
  }
  function deleteSelection(target=selectionTarget){
    if(target==='key'){removeCameraKey(cameraKeyIndex);return;}
    if(target==='range'){
      if(previewScope==='source'){setSourceMarks(null);setSelectionTarget(null);return;}
      if(!hasEditRange||!currentEditRange||!selectedClip)return;
      const start=currentEditRange.start!,end=currentEditRange.end!;
      try{const remaining:Clip[]=[];if(start>selectedClip.start)remaining.push(resizeClip(selectedClip,selectedClip.start,start));if(end<selectedClip.end)remaining.push({...resizeClip(selectedClip,end,selectedClip.end),id:remaining.length?crypto.randomUUID():selectedClip.id});
        const next=remaining[0]||project.clips.find(c=>c.id!==selectedClip.id);
        setProject(p=>({...p,clips:p.clips.flatMap(c=>c.id===selectedClip.id?remaining:[c])}));setSelected(next?.id||'');setEditRange(null);setSelectionTarget(next?'clip':null);if(!next)setPreviewScope('source');else seek(next.start);notify('Выделенный участок удалён · ⌘Z / Ctrl+Z — отменить');
      }catch(e){report(e);}return;
    }
    if(target==='clip'&&previewScope==='clip')removeSelectedClip();
  }
  async function suggestCamera(mode: 'scene' | 'speaker' = 'scene') {
    if(!selectedClip||suggestSubmitting)return;
    setSuggestSubmitting(true);setSuggestion(null);setSuggestJobStatus(null);setSuggestRequest(null);setError('');
    const clipId=selectedClip.id,fingerprint=JSON.stringify([selectedClip,style]);
    const epoch=jobEpoch.current;
    try {const r=await api<{job_id:string}>('/reframe/suggest',json({clip:selectedClip,style,mode}));if(epoch!==jobEpoch.current){void cancelLateJob(r.job_id);return;}setSuggestJob(r.job_id);setSuggestRequest({jobId:r.job_id,clipId,fingerprint});}
    catch(e){report(e);}finally{setSuggestSubmitting(false);}
  }
  const exactKey=`${JSON.stringify(project)}:${mode}:${resolution}`;
  const suggestionFingerprint=JSON.stringify([selectedClip,style]);
  async function renderExactPlayback() {
    setError('');setExactError('');setExactResult(null);setExactJobStatus(null);setExactOpen(false);
    const requestedKey=exactKey,epoch=jobEpoch.current;
    setExactSubmitting(true);
    try {const r=await api<{job_id:string}>('/preview/video',json({project,mode,resolution}));if(epoch!==jobEpoch.current){void cancelLateJob(r.job_id);return;}setExactRequested({jobId:r.job_id,key:requestedKey});}
    catch(e){setExactError(e instanceof Error?e.message:'Не удалось подготовить точный просмотр');report(e);}finally{setExactSubmitting(false);}
  }
  useEffect(()=>{
    if(!suggestRequest)return;
    let timer:number|undefined,stopped=false;
    const poll=async()=>{try{const job=await api<Job>(`/jobs/${suggestRequest.jobId}`);if(stopped)return;setSuggestJobStatus(job);
      if(job.status==='ready'&&job.result?.camera)setSuggestion({camera:job.result.camera,method:job.result.method,note:job.result.note,segments:job.result.segments,clipId:suggestRequest.clipId,fingerprint:suggestRequest.fingerprint});
      else if(job.status==='error'||job.status==='cancelled'){setSuggestRequest(null);if(job.status==='error')setError(job.message||'Не удалось предложить кадрирование');}
      else timer=window.setTimeout(poll,900);
    }catch{if(!stopped)timer=window.setTimeout(poll,1200);}};poll();
    return()=>{stopped=true;window.clearTimeout(timer);};
  },[suggestRequest]);
  useEffect(()=>{
    if(!exactRequested)return;
    let timer:number|undefined,stopped=false;
    const poll=async()=>{try{const j=await api<Job>(`/jobs/${exactRequested.jobId}`);if(stopped)return;setExactJobStatus(j);
      if(j.status==='ready'&&j.result?.url){const u=j.result.url.startsWith('/api/')?j.result.url:`/api${j.result.url}`;setExactResult({key:exactRequested.key,url:u});}
      else if(j.status==='error'){const message=j.message||'Не удалось подготовить точный просмотр';setExactError(message);setError(message);}
      else timer=window.setTimeout(poll,900);
    }catch(e){if(!stopped){setExactError(e instanceof Error?e.message:'Не удалось проверить точный просмотр');timer=window.setTimeout(poll,1200);}}};poll();
    return()=>{stopped=true;window.clearTimeout(timer);};
  },[exactRequested]);

  const preciseKey = `${projectId}:${currentOutputTime.toFixed(3)}:${resolution}:${JSON.stringify(project)}`;
  latestPreciseKey.current=preciseKey;
  function retryPreciseFrame(){requestedFrames.current.delete(preciseKey);frameJobIds.current.delete(preciseKey);preciseCache.current.delete(preciseKey);setPrecise(null);setPreciseRetry(n=>n+1);}
  useEffect(()=>{
    window.clearTimeout(frameTimer.current);
    if(!previewPreparationEnabled||playing||cameraEdit||layerEdit||previewScope!=='clip'||!selectedClip||!project.clips.length||!ready||!online)return;
    const cached=preciseCache.current.get(preciseKey);
    if(cached){setPrecise({key:preciseKey,url:cached});setPreciseState(null);return;}
    setPrecise(null);setPreciseState({key:preciseKey,status:'loading'});
    let stopped=false,pollTimer:number|undefined;
    const startPoll=(jobId:string)=>{
      const poll=async()=>{if(stopped)return;try{const j=await api<Job>(`/jobs/${jobId}`);
        if(j?.status==='ready'&&j.result?.url){const url=j.result.url.startsWith('/api/')?j.result.url:`/api${j.result.url}`;preciseCache.current.set(preciseKey,url);requestedFrames.current.delete(preciseKey);frameJobIds.current.delete(preciseKey);if(latestPreciseKey.current===preciseKey){setPrecise({key:preciseKey,url});setPreciseState(null);}return;}
        if(j?.status==='error'||j?.status==='cancelled'){requestedFrames.current.delete(preciseKey);frameJobIds.current.delete(preciseKey);if(latestPreciseKey.current===preciseKey)setPreciseState({key:preciseKey,status:'error'});return;}
        pollTimer=window.setTimeout(poll,700);
      }catch{pollTimer=window.setTimeout(poll,1200);}};poll();
    };
    const existingJob=frameJobIds.current.get(preciseKey);
    if(existingJob)startPoll(existingJob);
    frameTimer.current=window.setTimeout(async()=>{
      if(requestedFrames.current.has(preciseKey))return;
      requestedFrames.current.add(preciseKey);
      const epoch=jobEpoch.current;
      try {const r=await api<{job_id:string}>('/preview/frame',json({project,mode:'sdr',resolution,time:currentOutputTime}));
        if(stopped||epoch!==jobEpoch.current){requestedFrames.current.delete(preciseKey);void cancelLateJob(r.job_id);return;}
        frameJobIds.current.set(preciseKey,r.job_id);startPoll(r.job_id);
      }catch{requestedFrames.current.delete(preciseKey);if(latestPreciseKey.current===preciseKey)setPreciseState({key:preciseKey,status:'error'});}
    },450);
    return()=>{stopped=true;window.clearTimeout(frameTimer.current);window.clearTimeout(pollTimer);if(!frameJobIds.current.has(preciseKey))requestedFrames.current.delete(preciseKey);};
  },[preciseKey,playing,cameraEdit,layerEdit,ready,online,preciseRetry,selectedClip?.id,previewPreparationEnabled,previewScope]);
  function advanceClip() {
    if (!video.current) return;
    if(advancedClip.current===selected)return;
    advancedClip.current=selected;
    const index = project.clips.findIndex(c => c.id === selected);
    const next = project.clips[index + 1];
    video.current.pause();
    if (selected && next) { continuePlayback.current = true;setPreviewScope('clip'); setSelected(next.id); setAssetId(next.asset_id); }
    else { playheadRef.current=rangeEnd;setPlayhead(rangeEnd); setPlaying(false); }
  }
  function monitorEnded(source:string){
    if(source!==expectedMediaRef.current.src)return;
    if(previewQuality==='max'&&usingSourcePreview&&sourcePreview?.key===sourceKey&&sourcePreview.source_end<rangeEnd-.02){continuePlayback.current=true;const origin=previewScope==='clip'?selectedClip?.start??0:0;setSourceWindowAnchor(origin+Math.floor(Math.max(0,sourcePreview.source_end-origin)/10)*10);playheadRef.current=sourcePreview.source_end;setPlayhead(sourcePreview.source_end);return;}
    if(previewScope==='source'){playheadRef.current=rangeEnd;setPlayhead(rangeEnd);setPlaying(false);}else advanceClip();
  }
  function trim(field: 'start' | 'end', value: number) {
    if (!selectedClip || !activeAsset) return;
    const start = field === 'start' ? value : selectedClip.start;
    const end = field === 'end' ? value : selectedClip.end;
    if (start < 0 || end <= start || end > activeAsset.duration || duration - (selectedClip.end - selectedClip.start) + end - start > 180) {
      setError('Проверьте границы: конец должен быть позже начала, в пределах видео. Общая длительность — до 180 секунд.'); return;
    }
    try {const resized=resizeClip(selectedClip,start,end);updateClip(selectedClip.id,resized);seek(Math.max(start,Math.min(end-1/30,playhead)));setEditRange(null);setSelectionTarget('clip');}
    catch(e){report(e);}
  }
  function moveClip(id: string, offset: number) {
    setProject(p => { const clips = [...p.clips]; const i = clips.findIndex(c => c.id === id); if (i + offset < 0 || i + offset >= clips.length) return p; [clips[i], clips[i + offset]] = [clips[i + offset], clips[i]]; return { ...p, clips }; });
  }
  function addProposal(proposal: Proposal) {
    if (!activeAsset) return;
    if (duration + proposal.end - proposal.start > 180) { setError('В монтаж помещается до 180 секунд. Удалите или укоротите фрагмент.'); return; }
    const clip: Clip = { id: crypto.randomUUID(), asset_id: activeAsset.id, start: proposal.start, end: proposal.end, label: proposal.title, crop_x: .5 };
    setProject(p => ({ ...p, clips: [...p.clips, clip] })); setAnalysisPreview(null);setPreviewScope('clip'); setSelected(clip.id); setMobilePanel(null);
  }
  async function newProject() {
    try { await api(`/projects/${projectId}`, json(project, 'PUT')); setProjectId(crypto.randomUUID()); setProject(fresh()); setSelected('');setPreviewScope('source');setWorkflowStep(1);setCameraEdit(false);setLayerEdit(false);setPreviewQuality('max');setUsingSourcePreview(false); setView('edit'); }
    catch (e) { report(e); }
  }
  async function openProjects() { try { setSaved(await api<SavedProject[]>('/projects')); setView('projects'); } catch (e) { report(e); } }
  async function submitExport() {
    setExporting(true); setError('');
    try { await api('/exports', json({ project, mode, resolution })); setExportOpen(false); setView('exports'); notify('Экспорт добавлен в очередь'); await refresh(); }
    catch (e) { report(e); } finally { setExporting(false); }
  }
  async function uploadSRT(file: File) {
    if (!activeAsset) return;
    const data = new FormData(); data.append('file', file);
    try { await api(`/assets/${activeAsset.id}/transcript`, { method: 'POST', body: data }); setCues(await api<Cue[]>(`/assets/${activeAsset.id}/transcript/cues`)); notify('Субтитры загружены'); }
    catch (e) { report(e); }
  }

  return <div className="app-shell">
    <header className="topbar">
      <button className="brand" onClick={() => setView('edit')} aria-label="Открыть редактор"><span className="brand-mark"><FilmStrip size={25} weight="fill" /></span><span>clip<span className="brand-light">studio</span><small>Ваш монтажный стол</small></span></button>
      <nav className="main-nav" aria-label="Разделы"><button className={view === 'edit' ? 'active' : ''} onClick={() => setView('edit')}><Scissors size={19} />Монтаж</button><button className={view === 'projects' ? 'active' : ''} onClick={openProjects}><FolderOpen size={19} />Проекты</button><button className={jobsOpen?'active':''} onClick={()=>setJobsOpen(true)}><Stack size={19}/>Процессы{activeJobs.length>0&&<span className="count">{activeJobs.length}</span>}</button><button className={view === 'exports' ? 'active' : ''} onClick={() => setView('exports')}><DownloadSimple size={19} />Экспорты</button></nav>
      <div className="top-actions"><span className={`connection ${online ? '' : 'offline'}`}><span />{online ? runtimeHealth?.runtime==='desktop' ? `На этом Mac${runtimeHealth.renderer==='videotoolbox'?' · VideoToolbox':''}` : 'Сервер доступен' : 'Нет соединения'}</span><button className="primary" onClick={() => { setExportOpen(true);setWorkflowStep(4); }} disabled={!project.clips.length || !online}><CloudArrowUp size={20} />Экспортировать</button></div>
    </header>
    {error && <div className="error-banner" role="alert"><span>{error}</span><button className="icon-button" onClick={() => setError('')} aria-label="Закрыть ошибку"><X size={20} /></button></div>}
    {toast && <div className="toast" role="status"><CheckCircle size={20} />{toast}</div>}
    <input ref={fileInput} type="file" accept="video/*,.mkv,.webm" hidden onChange={e => { if (e.target.files?.[0]) upload(e.target.files[0]); e.target.value = ''; }} />
    <input ref={photoInput} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={e=>{if(e.target.files?.[0])uploadPhoto(e.target.files[0]);e.target.value='';}} />
    <input ref={srtInput} type="file" accept=".srt,.vtt" hidden onChange={e => { if (e.target.files?.[0]) uploadSRT(e.target.files[0]); e.target.value = ''; }} />

    {view === 'edit' && <>
      <div className="project-bar"><div className="project-name"><input aria-label="Название ролика" value={project.name} maxLength={100} onChange={e => setProject(p => ({ ...p, name: e.target.value }))} /><span className="save-status">{saveState === 'Сохранено' && <Check size={14} />}{saveState}</span></div><div className="project-tools"><span className="duration-chip"><Clock size={15} />{time(duration)} <span>/ 03:00</span></span><button className="text-button" onClick={newProject}><Plus size={17} />Новый ролик</button></div></div>
      <div className="mobile-tools"><button onClick={() => setMobilePanel(mobilePanel === 'media' ? null : 'media')}><Stack size={18} />Исходники</button><button onClick={() => setMobilePanel(mobilePanel === 'settings' ? null : 'settings')}><SlidersHorizontal size={18} />Оформление</button></div>
      <nav className="workflow-steps" aria-label="Этапы монтажа">{([{n:1,label:'Исходник',hint:'Выберите видео'}, {n:2,label:'Моменты',hint:`${project.clips.length} фрагментов`}, {n:3,label:'Кадр / оформление',hint:'Положение и стиль'}, {n:4,label:'Экспорт',hint:'Проверить и сохранить'}] as const).map(s=><button key={s.n} className={workflowStep===s.n?'active':''} onClick={()=>{setWorkflowStep(s.n);if(s.n===4&&project.clips.length)setExportOpen(true);}}><span>{s.n}</span><strong>{s.label}</strong><small>{s.hint}</small></button>)}</nav>
      <main className="workspace">
        <aside className={`media-panel ${mobilePanel === 'media' ? 'mobile-open' : ''}`}><div className="panel-heading"><h2>Исходники</h2><span>{assets.length}</span><button className="icon-button mobile-close" onClick={() => setMobilePanel(null)} aria-label="Закрыть"><X size={18} /></button></div>
          <form className="import-form" onSubmit={inspectURL}><label htmlFor="youtube">Ссылка на YouTube</label><div className="url-field"><Link size={18} /><input id="youtube" type="url" placeholder="https://youtube.com/watch…" value={url} onChange={e=>{setUrl(e.target.value);setInspectId('');}} required /></div><button className="secondary full-width" disabled={importing||!online||!url}>{importing?<SpinnerGap className="spin" size={18}/>:<MagnifyingGlass size={18}/>}Проверить качество</button>
          {inspected?.status==='ready'&&inspected.result&&<div className="youtube-options"><strong>{inspected.result.title}</strong><p>{time(inspected.result.duration||0)} · доступно: {[...new Set(inspected.result.qualities?.map(q=>`${q.height}p${q.hdr?' HDR':''}`))].join(', ')}</p><label htmlFor="youtube-quality">Скачать не выше</label><select id="youtube-quality" value={quality} onChange={e=>setQuality(Number(e.target.value) as 360|480|720|1080|2160)}><option value={360}>360p · длинные выпуски</option><option value={480}>480p · экономия места</option><option value={720}>720p · меньше места</option><option value={1080}>1080p</option><option value={2160}>2160p · 4K</option></select><label className="check-row"><input type="checkbox" checked={preferHdr} onChange={e=>setPreferHdr(e.target.checked)}/>Предпочитать HDR, если доступен</label><label className="check-row"><input type="checkbox" checked={autoAnalyze} onChange={e=>setAutoAnalyze(e.target.checked)}/>Сразу предложить клипы через ИИ</label><p>Размер заранее приблизителен. Если выбранного качества нет, загрузится ближайшее ниже. ИИ анализирует первые 30 минут.</p><button type="button" className="primary full-width" disabled={importing} onClick={importURL}>Скачать и обработать</button></div>}</form>
          <button className="upload-zone" disabled={importing || !online} onClick={() => fileInput.current?.click()} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (!importing && online && e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); }}><CloudArrowUp size={27} /><strong>{importing ? 'Загрузка на сервер…' : 'Загрузить файл'}</strong><span>или перетащите сюда · до {disk?.max_upload_mb || 2048} МБ</span></button>
          <div className="asset-list">{assets.map(a => <div className="asset-item" key={a.id}><button className={`asset-card ${activeAsset?.id === a.id ? 'selected' : ''}`} onClick={() => chooseAsset(a)}><div className="asset-thumb"><img src={mediaURL(a.id, 'thumbnail')} alt="" /><span>{time(a.duration)}</span></div><div className="asset-info"><strong>{a.title}</strong><span>Оригинал: {a.width} × {a.height}<b className={a.hdr ? 'hdr-tag' : ''}>{a.hdr ? 'HDR' : 'SDR'}</b></span></div></button><button className="delete-source" onClick={()=>setConfirmDelete(a)} aria-label={`Удалить исходник: ${a.title}`}><Trash size={14}/>Удалить исходник</button></div>)}</div>
          {!assets.length && <div className="library-empty"><FilmStrip size={28} /><p>Здесь появятся ваши видео</p><span>Оригинал останется на сервере.<br />В редакторе — лёгкий предпросмотр.</span></div>}
          <div className="media-footer"><VideoCamera size={17} /><span>{disk?`${size(disk.used)} / ${size(disk.budget)} · ${disk.retention_days>0?`хранение ${disk.retention_days} дней`:'до удаления'}`:'Оригиналы сохраняются отдельно'}</span></div>
        </aside>

        <section className={`edit-panel ${cameraEdit?'editing-camera':''}`}><div className="monitor-dock">
          <div className="preview-heading"><span>Монитор проекта · {style.aspect}</span><div className="preview-scope-switch"><button className={previewScope==='source'?'active':''} onClick={()=>{setPreviewScope('source');setAnalysisPreview(null);setSourceWindowAnchor(Math.floor(Math.max(0,playhead)/10)*10);setPrecise(null);}}>Исходник · всё видео</button><button className={previewScope==='clip'?'active':''} disabled={!selectedClip} onClick={()=>{if(selectedClip){setPreviewScope('clip');setSourceWindowAnchor(selectedClip.start);seek(Math.max(selectedClip.start,Math.min(selectedClip.end-1/30,playhead)));}}}>Монтаж · выбранный фрагмент</button></div><div><span className="monitor-zoom-controls">{(['fit',1,2] as const).map(z=><button key={z} className={monitorZoom===z?'active':''} onClick={()=>setMonitorZoom(z)}>{z==='fit'?'Вписать':`${z*100}%`}</button>)}</span><button className="icon-button" disabled={!activeAsset} onClick={() => document.querySelector('.preview-stage')?.requestFullscreen().catch(report)} aria-label="Полноэкранный просмотр"><ArrowsOut size={17} /></button></div></div>
          <div className="monitor-tools"><div className="monitor-status">{useDesktopOriginal?'Оригинал · '+activeAsset?.width+' × '+activeAsset?.height+' · браузер воспроизводит файл напрямую':runtimeHealth?.runtime==='desktop'?`Прокси ${activeAsset?.preview_width&&activeAsset?.preview_height?`${activeAsset.preview_width} × ${activeAsset.preview_height}`:previewShortEdge?`${previewShortEdge}px по короткой стороне`:'готов'} · можно сразу воспроизводить и перематывать`:previewQuality==='draft'?'Черновой прокси · SDR':!previewPreparationEnabled?'Качественный просмотр не подготовлен · монитор показывает черновой прокси':sourcePreviewStatus?.key===sourceKey?sourcePreviewStatus.status==='loading'?<><SpinnerGap className="spin" size={15}/>{sourcePreviewStatus.message}</>:<>{sourcePreviewStatus.message}<button className="text-button" onClick={()=>setSourcePreviewRetry(n=>n+1)}>Повторить</button><button className="text-button" onClick={()=>setPreviewQuality('draft')}>Быстрый черновик</button></>:sourcePreview?.key===sourceKey?<>{playing&&!usingSourcePreview?'Идёт черновой прокси · оригинал '+sourcePreview.width+' × '+sourcePreview.height+' готов; пауза переключит монитор на оригинал':<>Исходник {sourcePreview.width} × {sourcePreview.height} · SDR {sourcePreview.width<outputWidth||sourcePreview.height<outputHeight?'· детализация ограничена оригиналом':''} · окно 15 сек.</>}</>:sourcePreview?'Готовим следующее окно оригинала…':selectedClip?'Готовим предпросмотр из оригинала…':'Выберите исходник или фрагмент'}</div><div className="mode-tools"><details className="monitor-options"><summary>Качество и просмотр</summary><div className="monitor-options-body"><label className="monitor-resolution">Качество исходника <select aria-label="Качество монитора" value={previewQuality} onChange={e=>setPreviewQuality(e.target.value as 'max'|'draft')}><option value="max">Максимальное</option><option value="draft">Быстрый черновик</option></select></label><label className="monitor-resolution">Разрешение вывода <select aria-label="Разрешение монитора" value={resolution} onChange={e=>setResolution(Number(e.target.value) as 720|1080)}><option value={720}>720p</option><option value={1080}>1080p</option></select><small>{outputWidth} × {outputHeight} px</small></label>{needsPlaybackUpgrade&&<button className="prepare-preview" disabled={!!playbackUpgradeJob} onClick={upgradePlaybackPreview}>{playbackUpgradeJob?'Готовим улучшенный предпросмотр…':`Улучшить предпросмотр до ${Math.min(1080,activeAsset?.width||1080,activeAsset?.height||1080)}p`}</button>}<button className={`prepare-preview ${previewPreparationEnabled?'enabled':''}`} onClick={togglePreviewPreparation}>{previewPreparationEnabled?'Остановить подготовку':'Подготовить качественный просмотр'}</button><button className={`color-mode ${mode==='hdr'?'hdr':''}`} disabled={mode==='sdr'&&Boolean(hdrReason)} title={mode==='sdr'?hdrReason||'Сохранить HDR':'Вернуться к SDR'} onClick={()=>setMode(mode==='hdr'?'sdr':'hdr')}>{mode==='sdr'?'HDR выключен · SDR':'HDR включён'}</button>{mode==='sdr'&&hdrReason&&<span className="color-reason">HDR: {hdrReason}</span>}</div></details><button className={`secondary ${cameraEdit?'active':''}`} disabled={!selectedClip||style.fit==='contain'} onClick={()=>{if(cameraEdit)setCameraEdit(false);else enableCameraEdit();}}>{cameraEdit?'Завершить перемещение кадра':'Перемещать кадр'}</button><button className={`secondary ${layerEdit?'active':''}`} onClick={()=>{video.current?.pause();setPlaying(false);setExactResult(null);setCameraEdit(false);setLayerEdit(v=>!v);}}>{layerEdit?'Завершить оформление':'Двигать оформление'}</button></div></div>
          {selectedClip&&!cameraEdit&&!layerEdit&&exactResult?.key===exactKey ? <div className="exact-inline"><div><strong>Точный просмотр экспорта · {resolution}p · {mode.toUpperCase()}</strong><button className="text-button" onClick={()=>document.querySelector('.exact-inline video')?.requestFullscreen().catch(report)}><ArrowsOut size={15}/>На весь экран</button><button className="text-button" onClick={()=>setExactResult(null)}>Вернуться к монтажу</button></div><video controls playsInline preload="metadata" src={exactResult.url}/>{mode==='hdr'&&<p>Плеер браузера может не показать HDR цвет корректно.</p>}</div> : <Canvas style={style} update={updateStyle} video={video} assetId={activeAsset?.id} sourceUrl={monitorSourceUrl} sourceStart={monitorSourceUrl&&monitorSourceUrl===hqSourceUrl?sourcePreview?.source_start||0:0} assetWidth={activeAsset?.width} assetHeight={activeAsset?.height} crop={previewScope==='clip'?selectedClip?.crop_x??.5:.5} cameraKeys={previewScope==='clip'?cameraKeys:[]} cameraStart={previewScope==='clip'?selectedClip?.start??0:0} cameraSampleTime={localCameraTime} cameraPlaying={playing} editing={layerEdit} cameraEdit={cameraEdit} onCameraChange={xy=>updateCamera(xy)} preciseUrl={previewScope==='clip'&&selectedClip&&precise?.key===preciseKey&&!playing&&!cameraEdit&&!layerEdit?precise.url:undefined} monitorZoom={monitorZoom} outputWidth={outputWidth} outputHeight={outputHeight} onPreciseError={()=>{preciseCache.current.delete(preciseKey);requestedFrames.current.delete(preciseKey);frameJobIds.current.delete(preciseKey);setPrecise(null);setPreciseState({key:preciseKey,status:'error'});}} muted={style.mute} onLoaded={(_mediaTime,mediaSource)=>{if(mediaSource!==expectedMediaRef.current.src)return;const loadedHQ=Boolean(sourcePreview?.key===sourceKey&&mediaSource===new URL(sourcePreview.url,window.location.href).href);setUsingSourcePreview(loadedHQ);seek(playheadRef.current);if(continuePlayback.current){continuePlayback.current=false;video.current?.play().catch(()=>{});}}} onTime={progress} onEnded={monitorEnded} onPlay={source=>{if(source===expectedMediaRef.current.src)setPlaying(true);}} onPause={source=>{if(source===expectedMediaRef.current.src)setPlaying(false);}} onError={handleMonitorError} caption={currentCue?.text} empty={<div className="preview-empty"><span className="empty-film"><FilmStrip size={35}/></span><strong>Начните с видео</strong><p>Импортируйте ссылку YouTube<br/>или загрузите свой файл</p><button onClick={()=>fileInput.current?.click()} disabled={!online} className="empty-upload"><Plus size={18}/>Выбрать файл</button></div>}/>}
          {preciseState?.key===preciseKey&&!cameraEdit&&!layerEdit&&<div className={`frame-status ${preciseState.status}`} role="status">{preciseState.status==='loading'?<><SpinnerGap className="spin" size={15}/> Готовим точный SDR кадр…</>:<>Не удалось получить точный кадр. Черновой просмотр остаётся доступен.<button className="text-button" onClick={retryPreciseFrame}>Повторить</button></>}</div>}
          <div className="exact-playback"><button className="secondary" disabled={!project.clips.length||exactSubmitting||!!(exactRequested?.key===exactKey&&['queued','running'].includes(exactJobStatus?.status||''))} onClick={renderExactPlayback}><FilmStrip size={16}/>{exactSubmitting||exactRequested?.key===exactKey&&['queued','running'].includes(exactJobStatus?.status||'')?'Готовим точное видео…':'Собрать полный просмотр как экспорт'}</button>{exactError&&<span role="alert">{exactError}</span>}<span>{mode==='hdr'?'HDR: браузер может не декодировать цвет.':'Полный проект · экспортный размер '+resolution+'p · '+(previewPreparationEnabled?'точный SDR-кадр появится на паузе.':'точный кадр будет доступен после подготовки предпросмотра.')}</span></div>

          <div className="transport"><button className="icon-button" onClick={() => updateStyle({ mute: !style.mute })} aria-label={style.mute ? 'Включить звук' : 'Выключить звук'}>{style.mute ? <SpeakerSlash size={20} /> : <SpeakerHigh size={20} />}</button><div className="play-controls"><button className="text-button seek-step" disabled={!activeAsset} onClick={()=>seek(Math.max(rangeStart,playhead-5))} aria-label="Назад на пять секунд">−5с</button><button className="text-button seek-step" disabled={!activeAsset} onClick={()=>seek(Math.max(rangeStart,playhead-1/30))} aria-label="На кадр назад">−1 кадр</button><button className="play-button" disabled={!activeAsset} onClick={togglePlay} aria-label={playing ? 'Пауза' : 'Воспроизвести'}>{playing ? <Pause size={19} weight="fill" /> : <Play size={19} weight="fill" />}</button><button className="text-button seek-step" disabled={!activeAsset} onClick={()=>seek(Math.min(rangeEnd,playhead+1/30))} aria-label="На кадр вперёд">+1 кадр</button><button className="text-button seek-step" disabled={!activeAsset} onClick={()=>seek(Math.min(rangeEnd,playhead+5))} aria-label="Вперёд на пять секунд">+5с</button><span>{time(Math.max(0, playhead - rangeStart))}<i>/</i>{time(rangeEnd - rangeStart)}</span></div>{selectedClip&&previewScope==='clip'&&<div className="mark-controls"><button className="text-button" title="Отметить начало выделения. Границы фрагмента пока не меняются." onClick={()=>markEditRange('start')}>Начало выделения (I)</button><button className="text-button" title="Отметить конец выделения. Затем можно оставить или удалить выделенный участок." onClick={()=>markEditRange('end')}>Конец выделения (O)</button></div>}{previewScope==='source'&&activeAsset&&<div className="mark-controls source-mark-controls"><button className="text-button" onClick={()=>markSourceRange('start')}>Начало выделения (I)</button><button className="text-button" onClick={()=>markSourceRange('end')}>Конец выделения (O)</button>{currentSourceMarks&&<span>{currentSourceMarks.start===undefined?'—':time(currentSourceMarks.start)} → {currentSourceMarks.end===undefined?'—':time(currentSourceMarks.end)}</span>}{currentSourceMarks?.start!==undefined&&currentSourceMarks.end!==undefined&&currentSourceMarks.end>currentSourceMarks.start&&<button className="text-button add-at-playhead" onClick={addMarkedSourceRange}>Добавить выбранный фрагмент</button>}{currentSourceMarks&&<button className="text-button" onClick={()=>setSourceMarks(null)}>Очистить</button>}</div>}<span className="source-time">Исходник {time(playhead)}</span></div>
          <input className="scrubber" type="range" aria-label="Позиция в исходном видео" min={rangeStart} max={Math.max(rangeStart+.001,rangeEnd)} step={1/30} value={Math.max(rangeStart,Math.min(playhead,rangeEnd))} disabled={!activeAsset} onChange={e => seek(Number(e.target.value))} />
          {previewScope==='clip'&&selectedClip&&currentEditRange&&<div className="range-selection" role="status"><span>Выделение: {currentEditRange.start?.toFixed(2)??'—'} → {currentEditRange.end?.toFixed(2)??'—'} сек. · границы ещё не изменены</span><button className="text-button" disabled={!hasEditRange} onClick={keepEditRange}>Оставить выделенное</button><button className="text-button danger" disabled={!hasEditRange} onClick={()=>deleteSelection('range')}>Удалить участок</button><button className="text-button" onClick={()=>{setEditRange(null);setSelectionTarget('clip');}}>Снять выделение</button></div>}
          {cameraEdit&&selectedClip&&<div className="camera-live-controls" aria-label="Перемещение кадра"><label>По горизонтали<input aria-label="Кадр по горизонтали" type="range" min={0} max={1} step={.005} value={currentCamera.x} onChange={e=>updateCamera({x:Number(e.target.value)})}/><output>{Math.round(currentCamera.x*100)}%</output></label><label>По вертикали<input aria-label="Кадр по вертикали" type="range" min={0} max={1} step={.005} value={currentCamera.y} onChange={e=>updateCamera({y:Number(e.target.value)})}/><output>{Math.round(currentCamera.y*100)}%</output></label><label>Приближение<input aria-label="Приближение кадра" type="range" min={1} max={3} step={.01} value={currentCamera.zoom} onChange={e=>updateCamera({zoom:Number(e.target.value)})}/><output>{currentCamera.zoom.toFixed(2)}×</output></label><span>Изменения видны сразу · точка на {localCameraTime.toFixed(2)} сек. · если нет запаса по краям, увеличьте приближение.</span></div>}
          </div><div className="editor-details" tabIndex={-1}>
          {activeAsset&&rangeEnd>rangeStart&&<Waveform assetId={activeAsset.id} start={rangeStart} end={rangeEnd} playhead={playhead} onSeek={seek} muted={style.mute} cameraKeys={previewScope==='clip'?cameraKeys:[]} selection={previewScope==='source'?currentSourceMarks:currentEditRange}/>}
          <div className="workflow-prompt"><div><strong>{workflowStep===1?'Шаг 1 · Выберите исходник':workflowStep===2?'Шаг 2 · Отметьте нужный фрагмент':workflowStep===3?'Шаг 3 · Настройте кадр и оформление':'Шаг 4 · Проверьте и сохраните'}</strong><span>{workflowStep===1?'Нажмите на видео слева, затем добавьте фрагмент кнопкой ниже.':workflowStep===2?'I / O отмечают участок без обрезки. Примените выделение или добавьте секунды под дорожкой.':workflowStep===3?'Перемещение кадра и элементов оформления включается отдельно.':'SDR выбран по умолчанию; HDR можно включить, если проект это поддерживает.'}</span></div>{workflowStep===1&&<button className="primary" disabled={!activeAsset} onClick={() => currentSourceMarks?.start!==undefined&&currentSourceMarks.end!==undefined?addMarkedSourceRange():addClip(playhead)}><Plus size={17}/>{currentSourceMarks?.start!==undefined&&currentSourceMarks.end!==undefined?'Добавить выбранный фрагмент':'Добавить 60 секунд с текущего места'}</button>}{workflowStep===2&&!selectedClip&&project.clips[0]&&<button className="primary" onClick={()=>setSelected(project.clips[0].id)}>Выбрать первый фрагмент</button>}{workflowStep===3&&<button className="primary" onClick={()=>setMobilePanel('settings')}>Открыть оформление</button>}{workflowStep===4&&<button className="primary" disabled={!project.clips.length} onClick={()=>setExportOpen(true)}>Настроить экспорт</button>}</div>
          <div className="timeline-heading"><h2><Scissors size={18} />Моменты</h2><div><span>{project.clips.length} фрагм.</span><button className="text-button" disabled={!activeAsset} onClick={() => currentSourceMarks?.start!==undefined&&currentSourceMarks.end!==undefined?addMarkedSourceRange():addClip(playhead)}><Plus size={16} />{currentSourceMarks?.start!==undefined&&currentSourceMarks.end!==undefined?'Добавить выбранный фрагмент':'Добавить фрагмент'}</button></div></div>
          <div className="timeline-ruler">{[0, .25, .5, .75, 1].map(f => <span key={f}>{time((duration || 60) * f)}</span>)}</div>
          <div className="timeline-track">{project.clips.length ? project.clips.map((clip, i) => {
            const a = assets.find(x => x.id === clip.asset_id);
            return <button key={clip.id} className={`timeline-clip ${selected === clip.id ? 'active' : ''}`} style={{ flexGrow: clip.end - clip.start }} onClick={() => { setSelected(clip.id);setPreviewScope('clip');setSelectionTarget('clip');setCameraKeyIndex(-1);setEditRange(null); setAssetId(clip.asset_id); }}><span className="clip-number">{i + 1}</span>{a && <img src={mediaURL(a.id, 'thumbnail')} alt="" />}<span className="timeline-clip-info"><strong>{a?clip.label:'Исходник удалён'}</strong><small>{time(clip.end - clip.start)}</small></span>{a&&<Waveform compact assetId={a.id} start={clip.start} end={clip.end} muted={style.mute} playhead={selected===clip.id&&previewScope==='clip'?playhead:undefined}/>}</button>;
          }) : <button className="timeline-empty" disabled={!activeAsset} onClick={() => addClip()}><Plus size={20} />{activeAsset ? 'Добавьте первый фрагмент' : 'Фрагменты появятся здесь'}</button>}</div>
          {selectedClip&&<div className="extend-fragment" aria-label="Удлинить фрагмент"><strong>Добавить из исходника</strong><div><span>Перед началом</span>{[1,5].map(n=><button key={n} disabled={selectedClip.start<=0||duration>=180} onClick={()=>extendClip('start',n)}>+{n} сек. до</button>)}</div><div><span>После конца</span>{[1,5].map(n=><button key={n} disabled={!activeAsset||selectedClip.end>=activeAsset.duration||duration>=180} onClick={()=>extendClip('end',n)}>+{n} сек. после</button>)}</div><button className="text-button" onClick={splitSelectedClip}>Разделить (C)</button></div>}
          {selectedClip && <div className="trim-panel"><TrimInput label="Начало, сек." value={selectedClip.start} onCommit={value => trim('start', value)} /><TrimInput label="Конец, сек." value={selectedClip.end} onCommit={value => trim('end', value)} /><div className="trim-actions"><button className="icon-button" aria-label="Переместить фрагмент раньше" disabled={project.clips[0].id === selected} onClick={() => moveClip(selected, -1)}><ArrowUp size={17} /></button><button className="icon-button" aria-label="Переместить фрагмент позже" disabled={project.clips.at(-1)?.id === selected} onClick={() => moveClip(selected, 1)}><ArrowDown size={17} /></button><button className="icon-button danger" aria-label="Удалить фрагмент" onClick={removeSelectedClip}><Trash size={18} /></button></div></div>}
          {previewScope==='clip'&&selectedClip&&<section className="camera-panel" aria-label="Кадрирование во времени"><div className="camera-heading"><div><h3><FrameCorners size={16}/>Кадрирование · {selectedClip.label}</h3><span>{cameraKeys.length}/60 точек · сек. от начала фрагмента</span></div><div><button className="secondary" disabled={cameraKeys.length>=60&&!cameraKeys.some(k=>Math.abs(k.time-localCameraTime)<.015)} onClick={addOrUpdateCameraKey}><Plus size={15}/>Точка на плейхеде</button></div></div>
            <div className="camera-rail" aria-label="Точки кадрирования" onPointerDown={e=>{const rect=e.currentTarget.getBoundingClientRect();const fraction=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width));seek(selectedClip.start+fraction*(selectedClip.end-selectedClip.start));}}> {cameraKeys.map((k,i)=><button key={`${i}-${k.time}`} title={`${k.time.toFixed(2)} сек. · ${k.transition==='linear'?'Плавный переход':'Резкая смена'} · ${k.zoom.toFixed(2)}×`} style={{left:`${100*k.time/Math.max(.01,selectedClip.end-selectedClip.start)}%`}} className={`camera-marker transition-${k.transition} ${selectionTarget==='key'&&i===cameraKeyIndex?'active':''}`} onPointerDown={e=>e.stopPropagation()} onClick={()=>selectCameraKey(i)} aria-pressed={selectionTarget==='key'&&i===cameraKeyIndex} aria-label={`Точка ${k.time.toFixed(2)} сек. · ${k.transition==='linear'?'плавная':'резкая'} · ${k.zoom.toFixed(2)}×`}/>)}</div>
            <div className="camera-legend"><span><i className="legend-cut"/>Резкая смена</span><span><i className="legend-linear"/>Плавный переход к точке</span><span>Выберите точку · Delete удаляет её</span></div>
            {selectionTarget==='key'&&cameraKeys[cameraKeyIndex]&&<div className="camera-time"><TrimInput label="Время точки, сек." value={cameraKeys[cameraKeyIndex].time} onCommit={retimeCameraKey}/><label>Переход<select aria-label="Переход выбранной точки" value={cameraKeys[cameraKeyIndex].transition} onChange={e=>{const keys=[...cameraKeys];keys[cameraKeyIndex]={...keys[cameraKeyIndex],transition:e.target.value as CameraKey['transition']};updateClip(selectedClip.id,{camera:keys});}}><option value="cut">Резкая смена</option><option value="linear">Плавный к этой точке</option></select></label><button className="icon-button danger" onClick={()=>removeCameraKey(cameraKeyIndex)} aria-label="Удалить точку"><Trash size={16}/></button></div>}
            <details className="camera-advanced"><summary>Положение кадра и масштаб</summary><div className="camera-controls"><label>X <input type="range" min={0} max={1} step={.005} value={currentCamera.x} onChange={e=>updateCamera({x:Number(e.target.value)})}/><input type="number" min={0} max={1} step={.01} value={Number(currentCamera.x.toFixed(2))} onChange={e=>updateCamera({x:Number(e.target.value)})}/></label><label>Y <input type="range" min={0} max={1} step={.005} value={currentCamera.y} onChange={e=>updateCamera({y:Number(e.target.value)})}/><input type="number" min={0} max={1} step={.01} value={Number(currentCamera.y.toFixed(2))} onChange={e=>updateCamera({y:Number(e.target.value)})}/></label><label>Масштаб <input type="range" min={1} max={3} step={.01} value={currentCamera.zoom} onChange={e=>updateCamera({zoom:Number(e.target.value)})}/><input type="number" min={1} max={3} step={.05} value={Number(currentCamera.zoom.toFixed(2))} onChange={e=>updateCamera({zoom:Number(e.target.value)})}/></label></div></details>
            <div className="camera-footer"><button className="text-button" disabled={!online||suggestSubmitting||!!suggestRequest&&!suggestJobStatus||!!suggestJobStatus&&['queued','running','cancelling'].includes(suggestJobStatus.status)} onClick={()=>suggestCamera('scene')}><Sparkle size={15}/>{suggestJobStatus&&['queued','running'].includes(suggestJobStatus.status)?'Анализируем…':'План по сценам'}</button><button className="secondary speaker-plan-button" disabled={!online||suggestSubmitting||style.fit!=='cover'||activeAsset?.has_audio===false||!!suggestJobStatus&&['queued','running','cancelling'].includes(suggestJobStatus.status)||!!suggestRequest&&!suggestJobStatus} title={style.fit!=='cover'?'Выберите режим «Заполнить»':activeAsset?.has_audio===false?'В исходнике нет звука':'Найти говорящего по звуку и движению рта'} onClick={()=>suggestCamera('speaker')}><SpeakerHigh size={15}/>Камера на говорящего</button>{suggestion&&suggestion.clipId===selectedClip.id&&suggestion.fingerprint===suggestionFingerprint&&<div className="suggestion-ready"><span>{suggestion.note||'План по сценам и лицам готов.'}</span>{suggestion.segments&&suggestion.segments.length>0&&<div className="speaker-turns" aria-label="Найденные планы говорящих">{suggestion.segments.map((s,i)=><button key={i} className="text-button" onClick={()=>seek(selectedClip.start+s.time)}>{s.time.toFixed(1)}с · Участник {s.participant}</button>)}</div>}<button className="secondary" onClick={()=>{updateClip(selectedClip.id,{camera:suggestion.camera.slice(0,60)});setCameraKeyIndex(0);setSuggestion(null);notify('План применён; точки можно редактировать');}}>Применить план</button></div>}</div>
            <p className="hint">{!previewPreparationEnabled?'Качественный просмотр не запущен. Нажмите кнопку над монитором; точный кадр на паузе также не создаётся до запроса.':previewQuality==='draft'?'При воспроизведении используется быстрый черновой прокси.':usingSourcePreview?'Монитор показывает 15-секундное окно из оригинала; следующее окно подготовится при переходе дальше.':'Готовим высококачественное окно из оригинала.'} Точный кадр на паузе строится в SDR; {mode==='hdr'?'HDR цвет в точном кадре не отображается.':'это не меняет цвет экспортного файла.'}{style.fit==='contain'?' В режиме «Целиком» управление X/Y отключено.':''}</p>
          </section>}
          <div className="shortcut-help"><span>Пробел — просмотр · ← / → — кадр · Shift + ← / → — 1 сек.</span><span>I / O — выделение · C — разделить · K — точка · V — кадр · Delete — удалить выбранное · ⌘Z / Ctrl+Z — отменить</span><button className="text-button" disabled={!history.current.past.length} onClick={()=>restoreEdit()}>Отменить</button><button className="text-button" disabled={!history.current.future.length} onClick={()=>restoreEdit(true)}>Повторить</button></div><div className="timeline-foot"><span><Stack size={15} />Выберите другое видео, чтобы добавить вставку</span><span>Лимит 03:00 · {time(duration)}</span></div>
          </div>
        </section>

        <aside className={`settings-panel ${mobilePanel === 'settings' ? 'mobile-open' : ''}`}><div className="panel-heading"><h2>Оформление</h2><GearSix size={18} /><button className="icon-button mobile-close" onClick={() => setMobilePanel(null)} aria-label="Закрыть"><X size={18} /></button></div>
          <div className="settings-tabs" role="tablist" aria-label="Оформление">{([{ id: 'frame', icon: <FrameCorners size={19} />, label: 'Кадр' }, { id: 'text', icon: <TextT size={19} />, label: 'Текст' }, { id: 'captions', icon: <Subtitles size={19} />, label: 'Субтитры' }, { id: 'ai', icon: <Sparkle size={19} />, label: 'ИИ' }] as const).map(tab => <button role="tab" aria-selected={panel === tab.id} className={panel === tab.id ? 'active' : ''} onClick={() => setPanel(tab.id)} key={tab.id}>{tab.icon}{tab.label}</button>)}</div>
          <div className="settings-content">
            {panel === 'frame' && <><div className="setting-group"><label>Шаблон</label><div className="template-options"><button className={style.layout === 'plain' ? 'active' : ''} onClick={() => updateStyle({ layout: 'plain', header_box:null, video_box:null })}><span className="template-mini plain" />Чистое видео</button><button className={style.layout === 'card' ? 'active' : ''} onClick={() => updateStyle({ layout: 'card', header_box:null, video_box:null, border: 24, corner_radius: 70, background: '#64b9d1', text_color: '#101116', title_position: 'top', font_size: 64 })}><span className="template-mini card" />С шапкой</button></div></div><div className="setting-group"><label>Формат ролика</label><div className="aspect-options">{(['9:16', '1:1', '16:9'] as const).map(r => <button className={style.aspect === r ? 'active' : ''} key={r} onClick={() => updateStyle({ aspect: r })}><span className={`ratio-shape ratio-${r.replace(':', '-')}`} />{r}<small>{r === '9:16' ? 'Reels / TikTok' : r === '1:1' ? 'Квадрат' : 'Широкий'}</small></button>)}</div></div>
            <div className="setting-group"><label>Как вписать видео</label><div className="segmented"><button className={style.fit === 'cover' ? 'active' : ''} onClick={() => updateStyle({ fit: 'cover' })}>Заполнить</button><button className={style.fit === 'contain' ? 'active' : ''} onClick={() => updateStyle({ fit: 'contain' })}>Целиком</button></div><p className="hint">{style.fit === 'cover' ? 'Края исходника обрезаются под формат. Точка кадрирования задаётся в панели «Кадрирование».' : 'Исходник виден целиком, с полями; перемещение кадра отключено.'}</p></div>
            <div className="setting-group"><label>Рамка</label><div className="segmented">{([0, 24, 48] as const).map(b => <button key={b} className={style.border === b ? 'active' : ''} onClick={() => updateStyle({ border: b })}>{b === 0 ? 'Без рамки' : b === 24 ? 'Тонкая' : 'Широкая'}</button>)}</div></div>
            <div className="setting-group"><label htmlFor="bg">Цвет полей и рамки</label><div className="color-options">{['#101116', '#ffffff', '#4165db', '#f0b65b'].map(c => <button key={c} style={{ background: c }} className={style.background === c ? 'selected' : ''} onClick={() => updateStyle({ background: c })} aria-label={`Цвет ${c}`} />)}<input id="bg" type="color" value={style.background} onChange={e => updateStyle({ background: e.target.value })} /></div></div>
            <div className="setting-group"><label htmlFor="corners">Скругление видео <span>{style.corner_radius}</span></label><input id="corners" type="range" min={0} max={160} step={2} value={style.corner_radius} onChange={e => updateStyle({ corner_radius: Number(e.target.value) })} /></div>{style.layout === 'card' && <div className="setting-group"><label htmlFor="header-height">Высота шапки <span>{style.header_height}</span></label><input id="header-height" type="range" min={160} max={560} step={10} value={style.header_height} onChange={e => updateStyle({ header_height: Number(e.target.value), header_box:null,video_box:null })} /></div>}
            {style.layout==='card'&&<div className="setting-group"><label>Фото для шапки</label><button className="secondary full-width" onClick={()=>photoInput.current?.click()}><CloudArrowUp size={18}/>{style.header_image?'Заменить фото':'Загрузить фото'}</button>{style.header_image&&<button className="text-button" onClick={()=>updateStyle({header_image:null})}>Убрать фото</button>}<p className="hint">JPEG, PNG или WebP до 10 МБ. Перетащите шапку на холсте, чтобы выбрать положение.</p></div>}
            <div className="source-details"><h3>Исходное видео</h3>{activeAsset ? <><div><span>Размер кадра</span><strong>{activeAsset.width} × {activeAsset.height}</strong></div><div><span>Цвет</span><strong>{activeAsset.hdr ? activeAsset.transfer === 'arib-std-b67' ? 'HDR · HLG' : 'HDR · PQ' : 'SDR'} · {activeAsset.bit_depth} бит</strong></div><div><span>Файл</span><strong>{size(activeAsset.size)}</strong></div>{(activeAsset.hdr||mode==='hdr')&&<p>Монитор отображается в SDR. {mode==='hdr'?'Точный кадр на паузе тоже в SDR; цвет HDR-экспорта проверяйте на совместимом экране.':'HDR исходник в SDR-проекте преобразуется в SDR.'}</p>}</> : <p>Характеристики появятся после импорта.</p>}</div></>}
            {panel === 'text' && <><div className="setting-group"><label htmlFor="title">{style.layout === 'card' ? 'Заголовок в шапке' : 'Текст на видео'}</label><textarea id="title" rows={5} maxLength={160} placeholder="Добавьте заголовок или свой комментарий" value={style.title} onChange={e => updateStyle({ title: e.target.value })} /><div className="character-count">{style.title.length} / 160</div></div>{style.layout === 'plain' && <div className="setting-group"><label>Положение</label><div className="segmented">{([{ value: 'top', label: 'Сверху' }, { value: 'center', label: 'По центру' }, { value: 'bottom', label: 'Снизу' }] as const).map(o => <button className={style.title_position === o.value ? 'active' : ''} key={o.value} onClick={() => updateStyle({ title_position: o.value, title_box:null })}>{o.label}</button>)}</div></div>}<div className="setting-group"><label htmlFor="typeface">Шрифт</label><select id="typeface" className="full-width" value={style.font} onChange={e=>updateStyle({font:e.target.value as Style['font']})}>{[{id:'manrope',name:'Manrope · современный'},{id:'montserrat',name:'Montserrat · геометричный'},{id:'russoone',name:'Russo One · акцентный'},{id:'lora',name:'Lora · с засечками'},{id:'robotomono',name:'Roboto Mono · моноширинный'},{id:'cormorantgaramond',name:'Cormorant Garamond · элегантный'},{id:'cormorantitalic',name:'Cormorant Italic · изящный курсив'},{id:'prata',name:'Prata · контрастный'},{id:'tenorsans',name:'Tenor Sans · утончённый'},{id:'poiretone',name:'Poiret One · ар-деко'},{id:'greatvibes',name:'Great Vibes · каллиграфический'},{id:'badscript',name:'Bad Script · рукописный'},{id:'unbounded',name:'Unbounded · необычный геометричный'}].map(f=><option value={f.id} key={f.id}>{f.name}</option>)}</select><div className="font-sample" style={{fontFamily:`clip-${style.font}`,fontWeight:700}}>Красивые истории</div></div><div className="setting-group"><label htmlFor="font-size">Размер текста <span>{style.font_size}</span></label><input id="font-size" type="range" min={24} max={72} value={style.font_size} onChange={e => updateStyle({ font_size: Number(e.target.value) })} /></div>{style.layout === 'card' && <div className="setting-group"><label htmlFor="eyebrow">Подпись над заголовком</label><input id="eyebrow" className="full-width" maxLength={60} placeholder="Например: Мой комментарий" value={style.eyebrow} onChange={e => updateStyle({ eyebrow: e.target.value })} /></div>}<div className="setting-group"><label htmlFor="account">Ник / название аккаунта</label><input id="account" className="full-width" maxLength={50} placeholder="@your_account" value={style.account} onChange={e => updateStyle({ account: e.target.value })} /></div><div className="setting-group"><label htmlFor="text-color">Цвет текста</label><input id="text-color" type="color" value={style.text_color} onChange={e => updateStyle({ text_color: e.target.value })} /></div><p className="info-note">Текст и оформление в этой версии экспортируются в SDR. Для HDR оставьте чистое видео.</p></>}
            {panel === 'captions' && <><div className="toggle-setting"><label htmlFor="subs">Субтитры на видео</label><input id="subs" type="checkbox" checked={style.subtitles} onChange={e => updateStyle({ subtitles: e.target.checked })} /></div><button className="secondary full-width" disabled={!activeAsset} onClick={() => srtInput.current?.click()}><CloudArrowUp size={18} />Загрузить SRT / VTT</button><p className="hint">При импорте YouTube пробуем получить русские или английские субтитры.</p><div className="setting-group search-group"><label htmlFor="topic">Найти тему по субтитрам</label><div className="url-field"><MagnifyingGlass size={17} /><input id="topic" placeholder="Например, камера или HDR" value={query} onChange={e => setQuery(e.target.value)} /></div></div><div className="transcript-list">{subtitleMatches.slice(0, 80).map((cue, i) => <div className="transcript-cue" key={`${cue.start}-${i}`}><button onClick={() => seek(cue.start)}><span>{time(cue.start)}</span><p>{cue.text}</p></button><button className="icon-button" onClick={() => addClip(Math.max(0, cue.start - 2), cue.start + 58)} aria-label="Добавить минуту с этого момента"><Plus size={17} /></button></div>)}</div>{!cues.length && <div className="captions-empty"><Subtitles size={30} /><p>Субтитров пока нет</p><span>Загрузите SRT или VTT, чтобы искать моменты по словам.</span></div>}{cues.length > 0 && !subtitleMatches.length && <p className="hint">По этому запросу ничего не найдено.</p>}<p className="hint">Поиск по словам. Для поиска по смыслу и действиям откройте вкладку «ИИ».</p></>}
            {panel === 'ai' && <AIpanel asset={activeAsset} revision={jobs.filter(j => j.kind === 'analyze').map(j => j.id + j.status).join(',')} busy={activeJobs.some(j => j.kind === 'analyze')} onPreview={p => { if (activeAsset) setAssetId(activeAsset.id); if (!selected && analysisPreview?.start === p.start && analysisPreview?.end === p.end) { seek(p.start); if(previewQuality==='max'&&!sourcePreviewLoaded())continuePlayback.current=true;else video.current?.play().catch(report); } else { continuePlayback.current = true; setSelected(''); setAnalysisPreview({ start: p.start, end: p.end }); } }} onAdd={addProposal} onTitle={title => updateStyle({ title })} onError={report} onQueued={() => { notify('Анализ добавлен в очередь'); refresh().catch(report); }} />}
          </div>
          <div className="settings-bottom"><TiktokLogo size={18} /><InstagramLogo size={19} /><span>Один монтаж — две площадки</span></div>
        </aside>
      </main>
    </>}

    {view === 'projects' && <main className="page-view"><div className="page-heading"><div><h1>Ваши проекты</h1><p>Вернитесь к монтажу с сохранёнными фрагментами и оформлением.</p></div><button className="primary" onClick={newProject}><Plus size={19} />Новый ролик</button></div><div className="project-grid">{saved.map(p => { const a = assets.find(x => x.id === p.clips[0]?.asset_id); return <button className="saved-project" key={p.id} onClick={() => { const { id, ...body } = p; setProjectId(id); setProject({ ...body, style: { ...fresh().style, ...body.style } }); setSelected(body.clips[0]?.id || '');setPreviewScope(body.clips.length?'clip':'source'); setView('edit'); }}><div>{a ? <img src={mediaURL(a.id, 'thumbnail')} alt="" /> : <FilmStrip size={45} />}</div><strong>{p.name}</strong><span>{p.clips.length} фрагментов · {time(p.clips.reduce((n, c) => n + c.end - c.start, 0))} · {p.style.aspect}</span></button>; })}</div>{!saved.length && <div className="page-empty"><FolderOpen size={48} /><h2>Первый проект уже ждёт</h2><p>Загрузите видео, добавьте фрагмент — монтаж сохранится автоматически.</p><button className="secondary" onClick={() => setView('edit')}>Открыть редактор</button></div>}</main>}

    {view === 'exports' && <main className="page-view"><div className="page-heading"><div><h1>Готовые ролики</h1><p>{runtimeHealth?.runtime==='desktop'?`Рендер выполняется на этом Mac${runtimeHealth.renderer==='videotoolbox'?' · VideoToolbox':''}. Оставьте приложение открытым до готовности ролика.`:'Рендер выполняется на сервере. Можно закрыть эту вкладку.'}</p></div><button className="secondary" onClick={() => setView('edit')}><Scissors size={18} />Продолжить монтаж</button></div><div className="export-list">{exports.map(j => {const active=['queued','running','cancelling'].includes(j.status);return <div className={`export-card status-${j.status}`} key={j.id}><div className="export-icon">{j.status === 'ready' ? <CheckCircle size={28} /> : ['error','cancelled','dismissed'].includes(j.status) ? <X size={27} /> : <SpinnerGap className="spin" size={27} />}</div><div className="export-content"><strong>{j.result?.name || 'Экспорт ролика'}</strong><span>{j.status === 'ready' ? `${j.result?.mode === 'hdr' ? 'HDR · HEVC 10 бит' : 'SDR · H.264'} · ${time(j.result?.metadata?.duration || 0)} · ${size(j.result?.metadata?.size || 0)}` : j.status === 'queued' ? 'В очереди' : j.status === 'cancelled' ? 'Отменено' : j.status === 'dismissed' ? 'Закрыто' : j.message}</span>{j.status === 'running' && <div className="progress-bar"><span style={{ width: `${j.progress * 100}%` }} /></div>}</div>{j.status === 'ready' && <a className="primary" href={j.result?.url} download><DownloadSimple size={19} />Скачать MP4</a>}{active&&<button className="secondary export-cancel" disabled={j.status==='cancelling'} onClick={()=>cancelJob(j)}>{j.status==='cancelling'?'Отменяется…':'Отменить экспорт'}</button>}{!active&&<button className="icon-button danger" aria-label="Удалить экспорт" onClick={()=>api(`/exports/${j.id}`,{method:'DELETE'}).then(refresh).catch(report)}><Trash size={18}/></button>}</div>})}</div>{!exports.length && <div className="page-empty"><CloudArrowUp size={48} /><h2>Здесь будут готовые ролики</h2><p>Добавьте фрагменты в редакторе и нажмите «Экспортировать».</p><button className="secondary" onClick={() => setView('edit')}>Открыть редактор</button></div>}<section className="publish-panel"><h2>Публикация в соцсети</h2><p>Скачайте MP4 и загрузите его через приложение или сайт площадки.</p><div className="publish-links"><a href="https://www.tiktok.com/tiktokstudio/upload" target="_blank" rel="noreferrer"><TiktokLogo size={22} />Открыть TikTok</a><a href="https://www.instagram.com/" target="_blank" rel="noreferrer"><InstagramLogo size={22} />Открыть Instagram</a></div><label htmlFor="caption">Описание публикации</label><textarea id="caption" rows={3} maxLength={2200} placeholder="Ваш комментарий к ролику и хештеги" value={project.caption} onChange={e => setProject(p => ({ ...p, caption: e.target.value }))} /><button className="text-button" disabled={!project.caption} onClick={() => navigator.clipboard.writeText(project.caption).then(() => notify('Описание скопировано')).catch(() => setError('Не удалось скопировать. Выделите описание и скопируйте его вручную.'))}>Скопировать описание</button><div className="integration-note"><strong>Прямая публикация ещё не подключена</strong><p>Instagram требует подключения аккаунта и разрешений Meta. TikTok Direct Post API ограничивает личные утилиты и перенос произвольного чужого контента. <a href="https://developers.tiktok.com/doc/content-sharing-guidelines" target="_blank" rel="noreferrer">Правила TikTok</a></p></div></section></main>}

    {jobsOpen&&<div className="modal-backdrop" onClick={()=>setJobsOpen(false)}><section className="jobs-modal" role="dialog" aria-modal="true" aria-label="Процессы и рендеры" onClick={e=>e.stopPropagation()}><div className="jobs-heading"><div><h2>Процессы и рендеры</h2><p>{runtimeHealth?.parallel_quick_jobs?'Быстрые задачи идут параллельно с тяжёлой обработкой. В каждой очереди выполняется одна задача.':'Задачи выполняются последовательно. Параллельная быстрая очередь доступна в приложении на Mac.'}</p></div><button className="icon-button" onClick={()=>setJobsOpen(false)} aria-label="Закрыть процессы"><X size={21}/></button></div><div className="jobs-actions"><span>{activeJobs.length?`${activeJobs.length} активных процесса`:"Активных процессов нет"}</span><button className="secondary cancel-all" disabled={!activeJobs.length} onClick={cancelAllJobs}>Отменить все активные</button></div><div className="jobs-list">{[...jobs].sort((a,b)=>Number(activeJobs.some(j=>j.id===b.id))-Number(activeJobs.some(j=>j.id===a.id))).map(j=><article className={`job-card job-${j.status}`} key={j.id}><div className="job-card-head"><strong>{jobTitle(j.kind)}<small className="job-lane">{runtimeHealth?.parallel_quick_jobs?(j.lane==='quick'?'Быстрая очередь':'Основная очередь'):'Общая очередь'}</small></strong><span className={`job-state state-${j.status}`}>{jobStatus(j)}</span></div>{j.context?.title&&<div className="job-context">{j.context.title}</div>}{(j.context?.clip_label||j.context?.range||j.context?.clip_range)&&<div className="job-context">{j.context.clip_label||'Исходный участок'}{(j.context.range||j.context.clip_range)?` · ${time((j.context.range||j.context.clip_range)!.start)}–${time((j.context.range||j.context.clip_range)!.end)}`:''}</div>}<p className={j.status==='error'?'job-message job-error-message':'job-message'}>{j.message||jobStatus(j)}</p>{['queued','running','cancelling'].includes(j.status)&&<><div className="progress-bar"><span style={{width:`${Math.max(0,Math.min(1,j.progress))*100}%`}}/></div><button className="text-button cancel-one" disabled={j.status==='cancelling'} onClick={()=>cancelJob(j)}>{j.status==='cancelling'?'Отменяется…':'Отменить процесс'}</button></>}{j.status==='error'&&<button className="text-button" onClick={()=>api(`/jobs/${j.id}/dismiss`,json({})).then(refresh).catch(report)}>Закрыть сообщение об ошибке</button>}</article>)}{!jobs.length&&<div className="jobs-empty"><Stack size={28}/><strong>Задач пока нет</strong><span>Загрузка, анализ и рендеры появятся здесь.</span></div>}</div></section></div>}
    {confirmDelete&&<div className="modal-backdrop"><div className="export-modal" role="dialog" aria-modal="true" aria-label="Удаление исходника"><h2>Удалить исходник?</h2><p>{confirmDelete.title}. Оригинал, предпросмотр и предложения ИИ будут удалены. Готовые экспорты сохранятся. В других сохранённых монтажах это видео станет недоступно.</p><div className="modal-footer"><button className="secondary" onClick={()=>setConfirmDelete(null)}>Отмена</button><button className="primary" onClick={deleteAsset}>Удалить видео</button></div></div></div>}
    {exportOpen && <div className="modal-backdrop" onClick={() => setExportOpen(false)}><section className="export-modal" role="dialog" aria-modal="true" aria-labelledby="export-title" onClick={e => e.stopPropagation()}><div className="modal-heading"><span className="modal-icon"><CloudArrowUp size={26} /></span><button className="icon-button" onClick={() => setExportOpen(false)} aria-label="Закрыть экспорт"><X size={21} /></button></div><h2 id="export-title">Экспортировать ролик</h2><p>{project.name} · {time(duration)} · {style.aspect}</p><div className="setting-group"><label>Цвет и совместимость</label><button className={`export-option ${mode === 'sdr' ? 'active' : ''}`} onClick={() => setMode('sdr')}><span><strong>SDR · H.264</strong><small>Для загрузки в TikTok и Instagram</small></span>{mode === 'sdr' && <CheckCircle size={22} />}</button><button className={`export-option ${mode === 'hdr' ? 'active' : ''}`} disabled={Boolean(hdrReason)} onClick={() => setMode('hdr')}><span><strong>HDR · HEVC 10 бит</strong><small>{hdrReason || 'Сохранить HLG или PQ исходника'}</small></span>{mode === 'hdr' && <CheckCircle size={22} />}</button></div><div className="setting-group"><label>Разрешение</label><div className="segmented"><button className={resolution === 720 ? 'active' : ''} onClick={() => setResolution(720)}>720p</button><button className={resolution === 1080 ? 'active' : ''} onClick={() => setResolution(1080)}>1080p</button></div></div>{mode === 'hdr' && <p className="info-note">HDR в файле не гарантирует HDR после обработки соцсетью. Dolby Vision и динамические метаданные не сохраняются.</p>}<div className="modal-footer"><span><Clock size={16} />{runtimeHealth?.runtime==='desktop'?`Обработка на этом Mac${runtimeHealth.renderer==='videotoolbox'?' · VideoToolbox':''}`:'Обработка на сервере'}</span><button className="primary" disabled={exporting || !project.clips.length} onClick={submitExport}>{exporting ? <SpinnerGap className="spin" size={19} /> : <DownloadSimple size={19} />}Начать экспорт</button></div></section></div>}
    {exactOpen&&<div className="modal-backdrop" onClick={()=>setExactOpen(false)}><section className="exact-modal" role="dialog" aria-modal="true" aria-label="Точный просмотр ролика" onClick={e=>e.stopPropagation()}><div className="exact-modal-heading"><div><h2>Просмотр ролика как на экспорте</h2><p>Полный проект · {resolution}p · {mode.toUpperCase()}</p></div><button className="icon-button" onClick={()=>setExactOpen(false)} aria-label="Закрыть просмотр"><X size={21}/></button></div>{exactResult?.key===exactKey?<video controls playsInline preload="metadata" src={exactResult.url}/>:<div className={`exact-pending ${exactError||exactRequested&&exactRequested.key!==exactKey?'has-error':''}`}>{exactError||exactRequested&&exactRequested.key!==exactKey?<X size={24}/>:<SpinnerGap className="spin" size={24}/>}<strong>{exactRequested&&exactRequested.key!==exactKey?'Настройки проекта изменились':exactError||exactJobStatus?.message||'Собираем полный ролик в разрешении экспорта…'}</strong><span>{exactRequested&&exactRequested.key!==exactKey?'Готовый просмотр больше не соответствует текущему монтажу. Соберите его заново.':exactError?'Исправьте причину ошибки и повторите сборку.':mode==='hdr'?'HDR результат доступен для загрузки в браузере, но плеер может не показать HDR цвет.':'Просмотр появится здесь после обработки.'}</span></div>}<div className="modal-footer"><button className="secondary" onClick={()=>setExactOpen(false)}>Закрыть</button>{(exactError||(exactRequested&&exactRequested.key!==exactKey))&&<button className="primary" onClick={renderExactPlayback}>Собрать актуальный просмотр</button>}</div></section></div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<Studio />);
