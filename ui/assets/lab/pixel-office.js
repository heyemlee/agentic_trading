/* Pixel Agents sprite conventions and frame loop adapted under MIT.
 * Copyright (c) 2026 Pablo De Lucca. See vendor/pixel-agents/LICENSE. */
'use strict';
window.createLabPixelScene = function (document) {
  const canvas = document.querySelector('#pixel-office'), ctx = canvas.getContext('2d');
  const W = 320, H = 296, base = 'assets/lab/pixel-agents/';
  const seats = [
    [52,100,'up'],[108,100,'up'],[52,149,'up'],[108,149,'up'],
    [216,126,'right'],[280,126,'left'],[248,93,'down'],
    [52,247,'up'],[108,247,'up'],[202,244,'right'],[234,211,'down'],[266,244,'left'],
  ];
  const images = {}, jobs = [];
  function load(key, path) { jobs.push(new Promise((resolve,reject) => { const img = new Image(); img.onload = () => { images[key] = img; resolve(); }; img.onerror = reject; img.src = base + path; })); }
  for (let i=0;i<6;i++) load('char'+i, `characters/char_${i}.png`);
  ['DESK/DESK_FRONT','PC/PC_FRONT_ON_1','PC/PC_FRONT_ON_2','CUSHIONED_CHAIR/CUSHIONED_CHAIR_FRONT','BOOKSHELF/BOOKSHELF','DOUBLE_BOOKSHELF/DOUBLE_BOOKSHELF','LARGE_PLANT/LARGE_PLANT','PLANT/PLANT','CACTUS/CACTUS','COFFEE/COFFEE','BIN/BIN','CLOCK/CLOCK','SMALL_PAINTING/SMALL_PAINTING','WHITEBOARD/WHITEBOARD'].forEach(p => load(p.split('/')[1], 'furniture/'+p+'.png'));
  let roles=[], active=true, selected='', clock=0, last=0, ready=false, journey=null;
  const walkQueue=[];
  const palette = { floor:'#e9e5da', seam:'#ddd8cb', wall:'#f7f4ea', trim:'#c2b49a', rug:'#aeb8a0', edge:'#949f88', oak:'#cdaa79', oakLight:'#e6c79b', ink:'#596653' };
  function rect(x,y,w,h,color) { ctx.fillStyle=color;ctx.fillRect(x,y,w,h); }
  function image(key,x,y) { ctx.drawImage(images[key],Math.round(x),Math.round(y)); }
  function partition(x,y,w,h) {
    rect(x+3,y+5,w,h,'#c9c8ba');
    rect(x,y,w,h,palette.edge);rect(x,y,w,2,'#d9dfcc');
    if(w>h)rect(x+2,y+3,w-4,h-4,palette.rug);
    else rect(x+2,y+3,w-4,h-6,palette.rug);
  }
  function table(x,y,w,h) { rect(x+5,y+h,3,9,palette.ink);rect(x+w-8,y+h,3,9,palette.ink);rect(x,y+3,w,h,palette.oak);rect(x,y,w,h-3,palette.oakLight);rect(x+3,y+3,w-6,1,'#f5ddbb'); }
  function paper(x,y,color) { rect(x,y,10,12,'#fffdf4');for(let i=0;i<3;i++)rect(x+2+i*2,y+8-i*2,1,2+i*2,color); }
  function accessPath(i) {
    const [x,y]=seats[i];
    if(i<=3)return [[138,y],[138,155],[170,155]];
    if(i===4)return [[216,145],[181,145],[170,145]];
    if(i===5)return [[280,145],[300,145],[300,190],[170,190]];
    if(i===6)return [[248,60],[170,60]];
    if(i===7||i===8)return [[138,y],[138,270],[170,270]];
    if(i===9)return [[202,260],[181,260],[170,260]];
    if(i===10)return [[234,185],[170,185]];
    return [[266,260],[300,260],[300,270],[170,270]];
  }
  function routeFor(from,to) {
    const home=seats[from].slice(0,2), leave=accessPath(from), arrive=[...accessPath(to)].reverse();
    const target=arrive[arrive.length-1], outbound=[...leave,...arrive];
    const back=[...outbound.slice(0,-1)].reverse();
    return [...outbound,[target[0],target[1],2.2],...back,home];
  }
  function bubble(text,x,y,waiting=false) {
    const layer=document.querySelector('#ambient-bubbles');
    layer.replaceChildren();
    if(!text)return;
    const node=document.createElement('span');
    node.className=`ambient-bubble walk-bubble${waiting?' is-waiting':''}`;
    node.style.left=x/W*100+'%';node.style.top=(y-31)/H*100+'%';node.textContent=text;
    layer.appendChild(node);
  }
  function person(i,x,y,dir,walking=false) {
    const state=roles[i]?.state;
    if(state==='disabled')return;
    const moving=active&&!['stalled','error'].includes(state);
    const t=clock+i*.37;
    const frame=walking ? [0,1,2,1][Math.floor(t/.15)%4] : moving ? (state==='research'||i>3?5:3)+Math.floor(t/.3)%2 : 1;
    const row=dir==='up'?1:dir==='down'?0:2;
    ctx.save();ctx.translate(Math.round(x),Math.round(y));
    if(dir==='left')ctx.scale(-1,1);
    if(i>=6)ctx.filter=`hue-rotate(${[25,-20,55,-30,30,60][i-6]}deg)`;
    ctx.drawImage(images['char'+i%6],frame*16,row*32,16,32,-8,-32,16,32);
    ctx.restore();
  }
  function render() {
    if(!ready)return;
    ctx.imageSmoothingEnabled=false;
    rect(0,0,W,H,palette.floor);
    for(let y=32;y<H;y+=16)for(let x=8;x<W;x+=32){rect(x,y,31,1,palette.seam);rect(x+(y%32?16:0),y,1,16,palette.seam);}
    rect(0,0,W,35,palette.wall);rect(0,33,W,4,palette.trim);rect(0,0,6,H,palette.wall);rect(314,0,6,H,palette.wall);
    // One continuous floor, with low upholstered dividers and an open shared aisle.
    partition(147,49,6,91);partition(24,173,112,7);
    partition(191,163,103,7);partition(147,206,6,48);
    image('DOUBLE_BOOKSHELF',26,0);image('WHITEBOARD',199,1);image('CLOCK',287,7);
    image('SMALL_PAINTING',160,3);image('LARGE_PLANT',7,31);
    image('PLANT',294,38);image('PLANT',124,147);image('CACTUS',287,181);
    image('BIN',8,211);image('BIN',294,256);image('COFFEE',129,76);image('COFFEE',259,214);
    seats.forEach(([x,y])=>image('CUSHIONED_CHAIR_FRONT',x-8,y-12));
    // Back-facing workers sit in front of their monitors; no bitmap masks.
    // Continuous shared benches instead of six isolated square desks.
    [74,123,221].forEach(y=>table(28,y,104,17));
    [0,1,2,3,7,8].forEach(i=>{const [x,y]=seats[i];image('PC_FRONT_ON_'+(active&&Math.floor(clock/2)%2?2:1),x-8,y-37);});
    partition(28,105,104,4);
    table(228,89,40,39);table(214,207,40,39);
    paper(232,95,'#7b9c79');paper(253,109,'#bd8264');paper(218,213,'#bc7b61');paper(239,227,'#7b9c79');
    rect(245,102,5,6,palette.ink);rect(244,98,7,5,'#829969');rect(231,220,5,6,palette.ink);rect(230,216,7,5,'#829969');
    seats.map((s,i)=>({s,i})).sort((a,b)=>a.s[1]-b.s[1]).forEach(({s,i})=>{if(journey&&i===journey.role)return;person(i,...s);});
    if(journey)person(journey.role,journey.x,journey.y,journey.dir,!journey.hold);
    const idx=roles.findIndex(r=>r.id===selected);
    if(idx>=0&&!(journey&&idx===journey.role)){const [x,y]=seats[idx];rect(x-9,y+2,18,1,'#55795b');}
  }
  function tick(dt) {
    if(!active)return;
    clock+=dt;
    if(!journey)return;
    if(journey.hold>0){
      journey.hold-=dt;
      bubble(`${roles[journey.role].name}：${journey.message}`,journey.x,journey.y);
      if(journey.hold<=0)bubble('');
      return;
    }
    const target=journey.path[0];
    if(!target){const done=journey.done;journey=null;bubble('');done?.();startNextJourney();return;}
    const dx=target[0]-journey.x,dy=target[1]-journey.y,d=Math.hypot(dx,dy),step=dt*38;
    journey.dir=Math.abs(dx)>Math.abs(dy)?(dx>0?'right':'left'):(dy>0?'down':'up');
    if(d<=step){journey.x=target[0];journey.y=target[1];journey.path.shift();journey.hold=target[2]||0;}
    else{journey.x+=dx/d*step;journey.y+=dy/d*step;}
    const hit=document.querySelector(`#people [data-role="${roles[journey.role].id}"]`);
    if(hit){hit.style.left=journey.x/W*100+'%';hit.style.top=journey.y/H*100+'%';}
  }
  function startNextJourney() {
    if(journey||!active||!walkQueue.length)return;
    const request=walkQueue.shift(), from=roles.findIndex(r=>r.id===request.roleId), to=roles.findIndex(r=>r.id===request.targetId);
    if(from<0||to<0||from===to||roles[from].state==='disabled'){request.done?.();startNextJourney();return;}
    const [x,y]=seats[from];
    journey={role:from,target:to,x,y,dir:seats[from][2],hold:0,path:routeFor(from,to),message:request.message||'我带着结论过来交流。',done:request.done};
    request.start?.();
  }
  window.labPixelScene={
    seats:seats.map(([x,y])=>({x:x/W*100,y:y/H*100})),
    sync(data){roles=data.roles;selected=data.selected;active=data.active;if(!active)this.stop();render();},
    stop(){journey=null;walkQueue.splice(0);bubble('');},
    walk(roleId,targetId,callbacks={}){
      if(!active)return 'reduced';
      if((journey&&roles[journey.role]?.id===roleId)||walkQueue.some(r=>r.roleId===roleId))return 'duplicate';
      walkQueue.push({roleId,targetId,...callbacks});
      const queued=Boolean(journey);if(queued)callbacks.queued?.();startNextJourney();return queued?'queued':'started';
    },
    status(){return {active:journey?roles[journey.role]?.id:null,queued:walkQueue.map(r=>r.roleId),holding:Boolean(journey?.hold)};},
  };
  // Adapted from upstream gameLoop.ts: capped delta, one rAF, no React rerenders.
  function frame(time){const dt=last?Math.min((time-last)/1000,.1):0;last=time;if(!document.hidden&&active){tick(dt);render();}requestAnimationFrame(frame);}
  Promise.all(jobs).then(()=>{ready=true;render();requestAnimationFrame(frame);}).catch(()=>{document.querySelector('#scene-caption').textContent='办公室素材加载失败，请刷新重试。';});
};
