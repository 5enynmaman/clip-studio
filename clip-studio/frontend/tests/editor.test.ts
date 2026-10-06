import test from 'node:test';
import assert from 'node:assert/strict';
import {cameraValueAt,resizeClip} from '../src/editor.ts';

const clip={id:'clip',asset_id:'a'.repeat(32),label:'Фрагмент',start:20,end:40,crop_x:.5,camera:[
  {time:0,x:.2,y:.5,zoom:1,transition:'cut' as const},
  {time:8,x:.8,y:.7,zoom:2,transition:'linear' as const},
  {time:13,x:.1,y:.3,zoom:1.3,transition:'cut' as const},
  {time:20,x:.7,y:.5,zoom:1,transition:'linear' as const},
]};

for(const [label,start,end] of [['добавление секунд до',15,40],['добавление секунд после',20,45],['обрезка',23,37],['левая половина',20,30],['правая половина',30,40]] as const){
  test(`Траектория в исходнике сохраняется: ${label}`,()=>{
    const resized=resizeClip(clip,start,end);
    for(let t=Math.max(start,clip.start);t<=Math.min(end,clip.end);t+=.17){
      const expected=cameraValueAt(clip,t-clip.start),actual=cameraValueAt(resized,t-resized.start);
      for(const property of ['x','y','zoom'] as const)assert.ok(Math.abs(actual[property]-expected[property])<1e-8,`${property} at source ${t}`);
    }
    assert.ok(resized.camera.every((key,i)=>key.time>=0&&key.time<=end-start&&(!i||key.time>resized.camera[i-1].time)));
  });
}

test('Добавленные секунды удерживают первый и последний кадр',()=>{
  const extended=resizeClip(clip,15,45);
  assert.equal(cameraValueAt(extended,0).x,.2);
  assert.equal(cameraValueAt(extended,29).x,.7);
});

test('Точки после удалённого участка сохраняют положение в исходнике',()=>{
  const left=resizeClip(clip,20,25),right=resizeClip(clip,32,40);
  assert.equal(left.end-left.start+right.end-right.start,13);
  assert.equal(cameraValueAt(right,1).x,cameraValueAt(clip,13).x);
});

test('Неявный начальный кадр сохраняется при расширении',()=>{
  const implicit={...clip,camera:clip.camera.slice(1)};
  const extended=resizeClip(implicit,15,40);
  for(const t of [0,2,7,8,11,13,18,20])assert.ok(Math.abs(cameraValueAt(extended,t+5).x-cameraValueAt(implicit,t).x)<1e-8);
});

test('Некорректная длительность отклоняется',()=>{
  assert.throws(()=>resizeClip(clip,30,30));
  assert.throws(()=>resizeClip(clip,NaN,40));
  assert.throws(()=>resizeClip(clip,-1,40));
});
