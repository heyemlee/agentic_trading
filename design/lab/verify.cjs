/* Offline behavior checks. This is not browser layout or screenshot validation. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, 'lab.js'), 'utf8');

function fixture({ambient = false} = {}) {
  const elements = new Map(), handlers = {}, timeouts = new Map();
  let serial = 0;
  function element(key) {
    if (!elements.has(key)) elements.set(key, { textContent: '', innerHTML: '', hidden: false, style: {}, dataset: {}, attributes: {}, setAttribute(k, v) { this.attributes[k] = String(v); } });
    return elements.get(key);
  }
  const kinds = ['stock', 'crypto', 'etf'].map(kind => { const e = element(`kind-${kind}`); e.dataset.instrument = kind; return e; });
  function dynamic(selector, container, attr) {
    const result = [], expression = new RegExp(`data-${attr}="([^"]+)"`, 'g');
    for (const match of element(container).innerHTML.matchAll(expression)) {
      const e = element(`${selector}-${match[1]}`); e.dataset[attr] = match[1]; result.push(e);
    }
    return result;
  }
  const media = { matches: false, addEventListener(k, fn) { handlers.media = fn; } };
  const document = {
    hidden: false, documentElement: { dataset: {} },
    querySelector: element,
    querySelectorAll(selector) {
      if (selector === '[data-instrument]') return kinds;
      if (selector === '[data-role]') return dynamic(selector, '#people', 'role');
      if (selector === '[data-demo]') return dynamic(selector, '#state-options', 'demo');
      throw new Error(`Unknown selector ${selector}`);
    },
    addEventListener(name, handler) { handlers[name] = handler; },
  };
  const pixel = {seats:Array.from({length:12},()=>({x:20,y:30})),queue:[],sync(data){this.data=data;},stop(){this.walking=false;this.queue=[];},walk(role,target,callbacks={}){this.calls=(this.calls||[]).concat([[role,target]]);if(this.walking){this.queue.push({role,target,callbacks});callbacks.queued?.();return 'queued';}this.walking=true;this.callbacks=callbacks;callbacks.start?.();return 'started';},finish(){this.walking=false;this.callbacks?.done?.();}};
  const context = vm.createContext({ window:{labPixelScene:pixel}, document, matchMedia: () => media,
    setTimeout(fn) { const id = ++serial; timeouts.set(id, fn); return id; },
    clearTimeout(id) { timeouts.delete(id); }, requestAnimationFrame(fn) { fn(); },
    fetch() { throw new Error('Preview must not access a service'); },
  });
  vm.runInContext(source, context);
  if (!ambient) element('#ambient').onclick();
  return { pixel, element, media, handlers, timeouts, document, kind: k => element(`kind-${k}`).onclick(), click: s => element(s).onclick(), mode: value => element('#mode').onchange({ target: { value } }) };
}

test('ETF disabled role becomes active for stock, without changing other roles', () => {
  const f = fixture();
  assert.match(f.element('#people').innerHTML, /data-role="fundamentals" data-state="disabled"/);
  f.kind('stock');
  assert.doesNotMatch(f.element('#people').innerHTML, /data-state="disabled"/);
  assert.equal((f.element('#people').innerHTML.match(/class="person"/g) || []).length, 12);
});

test('replay is bounded and distinguishes NO_TRADE from a passed hard risk check', () => {
  const f = fixture();
  for (let i = 0; i < 15; i++) f.click('#next');
  assert.equal(f.element('#event-number').textContent, '事件 09 / 09');
  assert.equal(f.element('#next').disabled, true);
  assert.match(f.element('#gate-result').innerHTML, /NO_TRADE/);
  assert.match(f.element('#gate-result').innerHTML, /PASS/);
  assert.match(f.element('.approval').textContent, /无需审批/);
  for (let i = 0; i < 15; i++) f.click('#previous');
  assert.equal(f.element('#event-number').textContent, '事件 01 / 09');
  assert.equal(f.element('#previous').disabled, true);
  assert.doesNotMatch(f.element('#gate-result').innerHTML, /NO_TRADE/);
});

test('all seven inspection states render, while rest shows the last summary', () => {
  const f = fixture();
  for (const [state, expected] of Object.entries({ disabled: '不启用', waiting: '待命', thinking: '思考中', research: '查资料', done: '交出', stalled: '停住', error: '中断' })) {
    f.click(`[data-demo]-${state}`);
    assert.equal(f.element('#selected-state').textContent, expected);
  }
  f.mode('rest');
  assert.match(f.element('#selected-meta').textContent, /上次运行/);
  assert.match(f.element('#selected-bubble').textContent, /资金流/);
  assert.equal(f.element('#rest-note').hidden, false);
  assert.equal(f.element('#replay-controls').hidden, true);
});


test('every role can request a routed walk and a busy aisle queues the next agent',()=>{
 const f=fixture({ambient:true});
 const roles=['market','news','social','bull','bear','manager','trader','portfolio','aggressive','neutral','conservative'];
 for(const role of roles){f.click(`[data-role]-${role}`);f.click('#walk');}
 assert.equal(f.pixel.calls.length,roles.length);
 assert.equal(f.pixel.calls[0][0],'market');assert.equal(f.pixel.calls[0][1],'bull');
 assert.ok(f.pixel.queue.length>0);assert.match(f.element('#scene-caption').textContent,/等候过道/);
 f.pixel.finish(); assert.match(f.element('#scene-caption').textContent,/已返回/);
});
test('fundamentals can walk when the selected instrument enables that desk',()=>{
 const f=fixture({ambient:true});f.kind('stock');f.click('[data-role]-fundamentals');f.click('#walk');
 assert.deepEqual(f.pixel.calls.at(-1),['fundamentals','manager']);
});
test('walking, pause, visibility and reduced motion stay in sync',()=>{
 const f=fixture({ambient:true}); f.click('#walk'); assert.equal(f.pixel.walking,true);
 f.pixel.finish(); assert.match(f.element('#scene-caption').textContent,/已返回/);
 f.click('#walk'); f.click('#ambient'); assert.equal(f.pixel.walking,false); assert.equal(f.pixel.data.active,false);
 f.click('#walk'); assert.equal(f.pixel.walking,false);
 f.click('#ambient'); f.document.hidden=true; f.handlers.visibilitychange(); assert.equal(f.pixel.data.active,false);
 f.document.hidden=false;f.handlers.visibilitychange();assert.equal(f.pixel.data.active,true);
 f.media.matches=true;f.handlers.media();assert.equal(f.pixel.data.active,false);
 f.click('#walk');assert.equal(f.pixel.walking,false);
});
test('bundled character sheets fit every direction and animation frame',()=>{
 for(let i=0;i<6;i++) {const b=fs.readFileSync(path.join(__dirname,'vendor/pixel-agents/characters/char_'+i+'.png'));assert.equal(b.readUInt32BE(16),112);assert.equal(b.readUInt32BE(20),96);}
 assert.ok(fs.existsSync(path.join(__dirname,'vendor/pixel-agents/LICENSE')));
});
