import { useEffect, useRef, useState, type CSSProperties, type PointerEvent, type RefObject } from 'react';
import type { Box, Style } from './main';
import type { CameraKey } from './main';

type Layer = 'video' | 'header' | 'title' | 'eyebrow' | 'account';
const names: Record<Layer,string> = {video:'Видео',header:'Шапка',title:'Заголовок',eyebrow:'Подпись',account:'Ник'};
const clamp=(n:number,min:number,max:number)=>Math.max(min,Math.min(max,n));
const bounded=(b:Box):Box=>{const w=clamp(b.w,.03,1),h=clamp(b.h,.02,1);return {x:clamp(b.x,0,1-w),y:clamp(b.y,0,1-h),w,h};};
function bounds(s:Style,name:Layer):Box {
  const custom=s[`${name}_box`]; if(custom) return custom;
  const width=s.aspect==='16:9'?1920:1080,height=s.aspect==='9:16'?1920:1080;
  const header=s.layout==='card'?Math.floor(Math.min(s.header_height,height*.45)/2)*2:0;
  if(name==='video') return {x:s.border/width,y:(s.border+header)/height,w:1-2*s.border/width,h:1-(2*s.border+header+(s.layout==='card'?100:0))/height};
  if(name==='header') return {x:0,y:0,w:1,h:header?(s.border+header)/height:.2};
  const h=bounds(s,'header');
  if(name==='title') return s.layout==='card'?bounded({x:h.x+h.w*.06,y:h.y+(s.eyebrow?90:24)/height,w:h.w*.88,h:Math.max(.02,h.h-(s.eyebrow?110:44)/height)}):{x:.06,y:{top:.12,center:.45,bottom:.68}[s.title_position],w:.88,h:.18};
  if(name==='eyebrow') return bounded({x:h.x+h.w*.06,y:h.y+25/height,w:h.w*.88,h:40/height});
  return {x:.06,y:1-(s.border+(s.layout==='card'?67:90))/height,w:.88,h:50/height};
}
const position=(b:Box):CSSProperties=>({left:`${b.x*100}%`,top:`${b.y*100}%`,width:`${b.w*100}%`,height:`${b.h*100}%`});
function typography(text:string,s:Style,name:Layer,b:Box) {
  const width=s.aspect==='16:9'?1920:1080,height=s.aspect==='9:16'?1920:1080;
  let size=name==='title'?s.font_size:name==='eyebrow'?32:36;
  const ctx=document.createElement('canvas').getContext('2d');
  const wrap=()=>{
    if(!ctx) return text;
    ctx.font=`700 ${size}px clip-${s.font}`;
    const lines:string[]=[];
    for(const row of text.split('\n')) {
      let current='';
      for(let word of row.split(/\s+/).filter(Boolean)) {
        if(current&&ctx.measureText(`${current} ${word}`).width>b.w*width){lines.push(current);current='';}
        while(word.length>1&&ctx.measureText(word).width>b.w*width){let cut=1;while(cut<word.length&&ctx.measureText(word.slice(0,cut+1)).width<=b.w*width)cut++;if(current){lines.push(current);current='';}lines.push(word.slice(0,cut));word=word.slice(cut);}
        current+=(current?' ':'')+word;
      }
      lines.push(current);
    }
    return lines.join('\n');
  };
  let wrapped=wrap();while(wrapped.split('\n').length*size*1.2>b.h*height&&size>16){size--;wrapped=wrap();}
  return {wrapped,size:size/width*100};
}

export function Canvas({style:s,update,video,assetId,sourceUrl,sourceStart=0,assetWidth,assetHeight,crop,muted,onLoaded,onTime,onEnded,onPlay,onPause,onError,caption,empty,cameraKeys=[],cameraStart=0,cameraSampleTime=0,cameraPlaying=false,cameraEdit,editing=false,onCameraChange,preciseUrl,onPreciseError,monitorZoom,outputWidth,outputHeight}: {
  style:Style;update:(s:Partial<Style>)=>void;video:RefObject<HTMLVideoElement|null>;assetId?:string;sourceUrl?:string;sourceStart?:number;assetWidth?:number;assetHeight?:number;crop:number;muted:boolean;editing?:boolean;
  cameraKeys?:CameraKey[];cameraStart?:number;cameraSampleTime?:number;cameraPlaying?:boolean;cameraEdit?:boolean;onCameraChange?:(xy:Partial<{x:number;y:number}>)=>void;preciseUrl?:string;onPreciseError?:()=>void;monitorZoom?:'fit'|1|2;outputWidth?:number;outputHeight?:number;
  onLoaded:(currentTime:number,source:string)=>void;onTime:(currentTime:number,source:string)=>void;onEnded:(source:string)=>void;onPlay:(source:string)=>void;onPause:(source:string)=>void;onError:(source:string)=>void;caption?:string;empty:React.ReactNode;
}) {
  const canvas=useRef<HTMLDivElement>(null);
  const [active,setActive]=useState<Layer>('video');
  const [,fontLoaded]=useState(0);
  const [mediaState,setMediaState]=useState<{key:string;status:'ready'|'error'}|null>(null);
  const mediaKey=`${assetId}:${sourceUrl||'proxy'}`;
  const drag=useRef<{name:Layer;box:Box;x:number;y:number;resize:boolean;source?:boolean;camera?:CameraKey}|null>(null);
  const cameraState=useRef({keys:cameraKeys,baseX:crop,start:cameraStart,sampleTime:cameraSampleTime});
  cameraState.current={keys:cameraKeys,baseX:crop,start:cameraStart,sampleTime:cameraSampleTime};
  function cameraAt(sourceTime:number):CameraKey{
    const state=cameraState.current,keys=[...state.keys].sort((a,b)=>a.time-b.time),implicit:CameraKey={time:0,x:state.baseX,y:.5,zoom:1,transition:'cut'},all=keys[0]?.time>0?[implicit,...keys]:keys.length?keys:[implicit];
    const t=Math.max(0,sourceTime-state.start);let previous=all[0],value=previous;
    for(const next of all.slice(1)){if(t<next.time){if(next.transition==='linear'){const f=Math.max(0,Math.min(1,(t-previous.time)/(next.time-previous.time)));value={time:t,x:previous.x+(next.x-previous.x)*f,y:previous.y+(next.y-previous.y)*f,zoom:previous.zoom+(next.zoom-previous.zoom)*f,transition:next.transition};}else value=previous;break;}previous=next;value=next;}
    return value;
  }
  useEffect(()=>{document.fonts.load(`700 48px clip-${s.font}`).then(()=>fontLoaded(n=>n+1));},[s.font]);
  useEffect(()=>{
    const element=video.current;if(!element)return;
    const apply=(sourceTime:number)=>{
      const value=cameraAt(sourceTime+sourceStart);
      element.style.objectPosition=`${value.x*100}% ${value.y*100}%`;element.style.transform=`scale(${value.zoom})`;element.style.transformOrigin=`${value.x*100}% ${value.y*100}%`;
    };
    if(!cameraPlaying){apply(cameraState.current.start+cameraState.current.sampleTime-sourceStart);return;}
    type VideoFrameElement=HTMLVideoElement&{requestVideoFrameCallback?:(callback:(now:number,metadata:unknown)=>void)=>number;cancelVideoFrameCallback?:(id:number)=>void};
    const frameVideo=element as VideoFrameElement;let rafId=0,videoFrameId:number|undefined,stopped=false;
    if(frameVideo.requestVideoFrameCallback){const tick=()=>{if(stopped)return;apply(element.currentTime);videoFrameId=frameVideo.requestVideoFrameCallback!(tick);};videoFrameId=frameVideo.requestVideoFrameCallback(tick);}
    else {const tick=()=>{if(stopped)return;apply(element.currentTime);rafId=requestAnimationFrame(tick);};rafId=requestAnimationFrame(tick);}
    return()=>{stopped=true;if(rafId)cancelAnimationFrame(rafId);if(videoFrameId!==undefined)frameVideo.cancelVideoFrameCallback?.(videoFrameId);};
  },[video,assetId,sourceUrl,sourceStart,cameraKeys,crop,cameraStart,cameraPlaying]);
  useEffect(()=>{if(!cameraPlaying)applyCameraSample();},[cameraSampleTime,cameraPlaying]);
  function applyCameraSample(){const element=video.current;if(!element)return;const value=cameraAt(cameraState.current.start+cameraState.current.sampleTime);element.style.objectPosition=`${value.x*100}% ${value.y*100}%`;element.style.transform=`scale(${value.zoom})`;element.style.transformOrigin=`${value.x*100}% ${value.y*100}%`;}
  const visible:Layer[]=['video',...(s.layout==='card'?['header' as Layer]:[]),...(s.title?['title' as Layer]:[]),...(s.eyebrow&&s.layout==='card'?['eyebrow' as Layer]:[]),...(s.account?['account' as Layer]:[])];
  function down(e:PointerEvent<HTMLDivElement>,name:Layer,resize=false) {
    if(e.button!==0||drag.current||(!editing&&!(cameraEdit&&name==='video'))) return;
    e.preventDefault();e.stopPropagation();e.currentTarget.focus({preventScroll:true});e.currentTarget.setPointerCapture(e.pointerId);setActive(name);
    drag.current={name,box:bounds(s,name),x:e.clientX,y:e.clientY,resize,source:name==='video'&&!!cameraEdit,camera:cameraAt(cameraState.current.start+cameraState.current.sampleTime)};
  }
  function move(e:PointerEvent<HTMLDivElement>) {
    const d=drag.current,rect=canvas.current?.getBoundingClientRect();if(!d||!rect)return;
    const dx=(e.clientX-d.x)/rect.width,dy=(e.clientY-d.y)/rect.height;
    if(d.source){const b=d.box,windowAspect=(s.aspect==='16:9'?16/9:s.aspect==='1:1'?1:9/16)*b.w/b.h,sourceAspect=(assetWidth||16)/(assetHeight||9),current=d.camera!,excessX=Math.max(1,sourceAspect/windowAspect)*current.zoom-1,excessY=Math.max(1,windowAspect/sourceAspect)*current.zoom-1;const change:{x?:number;y?:number}={};if(excessX>1e-4)change.x=clamp(current.x-dx/(b.w*excessX),0,1);if(excessY>1e-4)change.y=clamp(current.y-dy/(b.h*excessY),0,1);if(Object.keys(change).length)onCameraChange?.(change);return;}
    const b=d.resize?{...d.box,w:clamp(d.box.w+dx,.03,1-d.box.x),h:clamp(d.box.h+dy,.02,1-d.box.y)}:bounded({...d.box,x:d.box.x+dx,y:d.box.y+dy});
    update({[`${d.name}_box`]:b});
  }
  function key(e:React.KeyboardEvent<HTMLDivElement>,name:Layer) {
    if(cameraEdit&&name==='video'&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){
      e.preventDefault();e.stopPropagation();const current=cameraAt(cameraState.current.start+cameraState.current.sampleTime),step=e.shiftKey?.05:.005;
      onCameraChange?.({x:clamp(current.x+(e.key==='ArrowRight'?step:e.key==='ArrowLeft'?-step:0),0,1),y:clamp(current.y+(e.key==='ArrowDown'?step:e.key==='ArrowUp'?-step:0),0,1)});return;
    }
    if(!editing||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;
    e.preventDefault();e.stopPropagation();const b=bounds(s,name),step=e.shiftKey?.02:.002;
    update({[`${name}_box`]:bounded({...b,x:b.x+(e.key==='ArrowRight'?step:e.key==='ArrowLeft'?-step:0),y:b.y+(e.key==='ArrowDown'?step:e.key==='ArrowUp'?-step:0)})});
  }
  const layer=(name:Layer,children:React.ReactNode,extra:CSSProperties={})=><div key={name} className={`canvas-layer layer-${name} ${editing?'draggable':''} ${editing&&active===name?'selected-layer':''} ${name==='video'&&cameraEdit?'camera-source-drag selected-layer':''}`} style={{...position(bounds(s,name)),...extra,pointerEvents:cameraEdit&&name!=='video'?'none':undefined}} tabIndex={editing||cameraEdit&&name==='video'?0:-1} role="group" aria-label={`Слой ${names[name]}`} onKeyDown={e=>key(e,name)} onPointerDown={e=>down(e,name)} onPointerMove={move} onPointerUp={()=>drag.current=null} onPointerCancel={()=>drag.current=null} onLostPointerCapture={()=>drag.current=null}>{children}{(editing&&active===name||cameraEdit&&name==='video')&&<><span className="layer-label">{cameraEdit?'Перемещение кадра':names[name]}</span>{!(name==='video'&&cameraEdit)&&<div className="resize-handle" role="button" aria-label={`Изменить размер: ${names[name]}`} onPointerDown={e=>down(e,name,true)} />}</>}</div>;
  const textLayer=(name:'title'|'eyebrow'|'account',text:string)=>{
    const t=typography(text,s,name,bounds(s,name));
    return layer(name,<span style={{fontSize:`${t.size}cqw`}}>{t.wrapped}</span>,{fontFamily:`clip-${s.font}`,color:s.text_color,textAlign:name==='account'||(s.layout==='plain'&&name==='title')?'center':'left',background:s.layout==='plain'&&name!=='eyebrow'?'#101116aa':undefined});
  };
  const scale=monitorZoom==='fit'?1:Number(monitorZoom||1);
  const canvasSize:CSSProperties=monitorZoom==='fit'?{}:{width:`${(outputWidth||1080)*scale}px`,height:`${(outputHeight||1920)*scale}px`,maxWidth:'none',maxHeight:'none',flex:'none'};
  return <><div className="layer-toolbar">{editing&&visible.map(name=><button key={name} className={active===name?'active':''} onClick={()=>setActive(name)}>{names[name]}</button>)}{editing&&<button onClick={()=>update({video_box:null,header_box:null,title_box:null,eyebrow_box:null,account_box:null})}>Сбросить оформление</button>}</div><div className={`preview-stage monitor-zoom-${monitorZoom||'fit'}`}><div className="monitor-viewport"><div ref={canvas} className={`preview-canvas aspect-${s.aspect.replace(':','-')}`} style={{background:s.background,...canvasSize}}>
  {assetId?<>{layer('video',<><video key={mediaKey} ref={video} src={sourceUrl||`/api/assets/${assetId}/proxy`} preload="metadata" playsInline muted={muted} style={{objectFit:s.fit,objectPosition:'50% 50%',transform:'scale(1)',transformOrigin:'50% 50%'}} onEmptied={()=>setMediaState(null)} onLoadedData={()=>setMediaState({key:mediaKey,status:'ready'})} onSeeked={e=>{if(e.currentTarget.readyState>=2)setMediaState({key:mediaKey,status:'ready'});}} onLoadedMetadata={e=>onLoaded(e.currentTarget.currentTime,e.currentTarget.currentSrc||e.currentTarget.src)} onTimeUpdate={e=>onTime(e.currentTarget.currentTime,e.currentTarget.currentSrc||e.currentTarget.src)} onEnded={e=>onEnded(e.currentTarget.currentSrc||e.currentTarget.src)} onPlay={e=>onPlay(e.currentTarget.currentSrc||e.currentTarget.src)} onPause={e=>onPause(e.currentTarget.currentSrc||e.currentTarget.src)} onError={e=>{setMediaState({key:mediaKey,status:'error'});onError(e.currentTarget.currentSrc||e.currentTarget.src);}} />{mediaState?.key!==mediaKey||mediaState.status!=='ready'?<div className="video-loading" role="status">{mediaState?.key===mediaKey&&mediaState.status==='error'?'Кадр недоступен':'Загружаем кадр…'}</div>:null}</>,{borderRadius:`${s.corner_radius/(s.aspect==='16:9'?1920:1080)*100}cqw`})}{s.layout==='card'&&layer('header',s.header_image?<img src={`/api/graphics/${s.header_image}`} alt="Фото шапки" draggable={false}/>:null,{background:s.background})}{s.eyebrow&&s.layout==='card'&&textLayer('eyebrow',s.eyebrow)}{s.title&&textLayer('title',s.title)}{s.account&&textLayer('account',s.account)}{s.subtitles&&caption&&<div className="video-caption">{caption}</div>}{preciseUrl&&<img className="precise-frame" src={preciseUrl} alt="Точный кадр из экспортного конвейера" onError={onPreciseError}/>}</>:empty}
  </div></div><div className="safe-area-note">{cameraEdit?'Перетащите исходник внутри окна, чтобы изменить точку кадрирования':editing?'Перетащите слой · угол меняет размер · стрелки сдвигают точнее':'Предпросмотр оформления'}</div></div></>;
}
