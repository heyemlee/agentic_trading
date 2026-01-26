'use strict';
document.querySelector('#asset-grid').innerHTML = Array.from({length:6},(_,i)=>`<article class="asset-card"><img style="image-rendering:pixelated;aspect-ratio:112/96" src="vendor/pixel-agents/characters/char_${i}.png" alt="角色 ${i+1} 的三方向动作帧"><h2>角色模板 ${i+1}</h2><small>走路 · 打字 · 阅读</small></article>`).join('');
document.querySelector('#asset-theme').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';};
