'use strict';
// Masks isolate original painted parts; small transforms create continuous desk ambience.
(() => {
  const shapes = {
    head: ['100 65 400 400', 'M108 405C94 350 115 243 143 174C168 100 244 63 294 75C329 60 386 80 414 112C470 166 472 240 484 322C497 367 478 411 453 425L343 436L335 455L271 455L267 434L174 431Z'],
    torso: ['610 160 355 285', 'M617 406C604 347 609 254 627 219Q650 187 692 204L729 163Q780 184 827 166L873 201Q923 187 945 221C966 280 962 357 942 409Q898 447 788 442Q690 441 617 406Z'],
    upper: ['1175 65 160 275', 'M1193 80Q1231 48 1263 90C1290 141 1301 195 1314 245Q1339 300 1307 335L1249 338Q1217 314 1226 278C1209 225 1193 172 1186 127Q1175 95 1193 80Z'],
    left: ['177 530 140 440', 'M215 554Q263 516 289 543Q318 576 306 635L290 744Q296 790 268 821L264 844Q284 899 302 934Q300 958 276 957L251 938L235 913Q247 951 234 953Q213 938 209 910L210 863L218 831Q181 804 180 771C177 716 190 613 215 554Z'],
  };
  function markup(id) {
    const piece = (name,x,y,w,h) => `<svg x="${x}" y="${y}" width="${w}" height="${h}" viewBox="${shapes[name][0]}"><image href="assets/neutral-parts-v4.png" width="1536" height="1024" clip-path="url(#${id}-${name})"/></svg>`;
    return `<svg class="neutral-rig" viewBox="0 0 200 200" role="img" aria-label="中性分析师正在轻微呼吸、转头和活动双手"><defs>${Object.entries(shapes).map(([name,[,path]])=>`<clipPath id="${id}-${name}"><path d="${path}"/></clipPath>`).join('')}</defs><g class="rig-breath"><g class="rig-sway">${piece('torso',53,99,95,76)}<g class="rig-left">${piece('left',44,110,23,67)}</g><g class="rig-right">${piece('upper',126,108,28,43)}</g><g class="rig-head">${piece('head',39,1,123,123)}</g></g></g></svg>`;
  }
  document.querySelector('#rig-seat').innerHTML = markup('seat');
  document.querySelector('#rig-closeup').innerHTML = markup('closeup');
  document.querySelector('#rig-focus').onclick = () => document.querySelector('#rig-study').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'center'});
})();
