import type { CameraKey, Clip } from './main';

export function cameraValueAt(clip: Clip | undefined, localTime: number): CameraKey {
  const implicit: CameraKey = {time:0,x:clip?.crop_x??.5,y:.5,zoom:1,transition:'cut'};
  const keys = [...(clip?.camera || [])].sort((a,b)=>a.time-b.time);
  const all = keys[0]?.time===0 ? keys : [implicit,...keys];
  let previous = all[0];
  for (const next of all.slice(1)) {
    if (localTime < next.time) {
      if (next.transition==='linear') {
        const f=Math.max(0,Math.min(1,(localTime-previous.time)/(next.time-previous.time)));
        return {time:localTime,x:previous.x+(next.x-previous.x)*f,y:previous.y+(next.y-previous.y)*f,zoom:previous.zoom+(next.zoom-previous.zoom)*f,transition:next.transition};
      }
      return {...previous,transition:next.transition};
    }
    previous=next;
  }
  return previous;
}

// Camera times belong to the fragment. Preserve the trajectory in source time
// when adding handles, trimming, splitting or removing a selected interval.
export function resizeClip(clip: Clip, start: number, end: number): Clip {
  if (!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start) throw new Error('Проверьте границы фрагмента');
  if (!clip.camera?.length) return {...clip,start,end};
  const delta=start-clip.start,span=end-start;
  const keys=clip.camera[0].time>0?[{time:0,x:clip.crop_x,y:.5,zoom:1,transition:'cut' as const},...clip.camera]:clip.camera;
  const camera:CameraKey[]=[{...cameraValueAt(clip,delta),time:0,transition:'cut'}];
  for(const key of keys) if(key.time>delta&&key.time<delta+span) camera.push({...key,time:key.time-delta});
  if(delta+span<clip.end-clip.start) camera.push({...cameraValueAt(clip,delta+span),time:span});
  else {
    const endpoint=keys.find(k=>Math.abs(k.time-delta-span)<.0001);
    if(endpoint)camera.push({...endpoint,time:span});
  }
  if(camera.length>60)throw new Error('Новые границы требуют больше 60 точек. Удалите лишние точки кадрирования.');
  return {...clip,start,end,camera};
}
