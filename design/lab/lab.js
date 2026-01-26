'use strict';
(() => {
  const q = (s) => document.querySelector(s);
  // All data here is illustrative. No requests, credentials, execution, or approval persistence.
  const roles = [
    ['market', '市场分析师', '分析区', 20, 34, '指标与快照已整理，价格趋势仍需结合成交量判断。'],
    ['news', '新闻分析师', '分析区', 39, 35, '主要新闻已归档，暂未发现改变研究假设的新事件。'],
    ['social', '社交情绪分析师', '分析区', 19, 47, '讨论强度回落，情绪证据仅作辅助。'],
    ['fundamentals', '基本面分析师', '分析区', 40, 47, '盈利质量和估值已对照，关注预期变化。'],
    ['bull', 'Bull 研究员', '辩论桌', 61, 45, '趋势与资金流仍提供支持，但需要核实持续性。'],
    ['bear', 'Bear 研究员', '辩论桌', 85, 44, '当前价格可能已计入利好，回撤空间不能忽略。'],
    ['manager', '研究经理', '辩论桌', 73, 38, '双方证据存在分歧，建议保留谨慎假设。'],
    ['trader', '交易员', '交易与评级台', 18, 79, '交易提案已整理，等待风险意见和规则裁决。'],
    ['portfolio', '投资组合经理', '交易与评级台', 35, 79, '综合评级：谨慎观望。评级不代表获准交易。'],
    ['aggressive', '激进风险分析师', '风控圆桌', 62, 76, '积极敞口可能提高收益，也会放大回撤。'],
    ['neutral', '中性风险分析师', '风控圆桌', 74, 66, '收益机会与波动风险需要共同评估。'],
    ['conservative', '保守风险分析师', '风控圆桌', 91, 76, '优先限制下行损失，保留安全余量。'],
  ].map(([id, name, zone, x, y, summary]) => ({ id, name, zone, x, y, summary }));
  const labels = { disabled: '不启用', waiting: '待命', thinking: '思考中', research: '查资料', done: '交出', stalled: '停住', error: '中断' };
  const descriptions = { disabled: '空椅子保留工位；注明不启用原因。', waiting: '等待前序报告，没有正在执行的任务。', thinking: '只显示当前角色与状态持续时间，不编造百分比进度。', research: '显示查资料状态与最近事件，不使用旋转加载动画。', done: '报告已交出；仅在交接事件发生时移动文件。', stalled: '8 分钟无新事件。用琥珀色与文字提示，不推断任务已失败。', error: '运行失败。保留最后事件，以文字和砖红色提示中断。' };
  const sequence = [
    { time: '14:29:10', role: 'market', state: 'research', text: '市场分析师正在查询价格快照。', elapsed: 18 },
    { time: '14:30:02', role: 'news', state: 'research', text: '新闻分析师正在查阅新闻证据。', elapsed: 22 },
    { time: '14:31:05', role: 'market', state: 'done', text: '分析报告已交给辩论桌。', elapsed: 28 },
    { time: '14:32:00', role: 'bull', state: 'thinking', text: 'Bull 开始第一轮论证。', elapsed: 41 },
    { time: '14:34:18', role: 'bear', state: 'thinking', text: 'Bear 开始回应看多论点。', elapsed: 36 },
    { time: '14:36:40', role: 'manager', state: 'thinking', text: '研究经理正在对照双方证据。', elapsed: 24 },
    { time: '14:38:12', role: 'trader', state: 'thinking', text: '交易员正在拟定交易提案。', elapsed: 32 },
    { time: '14:40:16', role: 'neutral', state: 'thinking', text: '三种风险立场分别评估交易提案。', elapsed: 51 },
    { time: '14:43:20', role: 'portfolio', state: 'done', text: '综合评级提交；规则裁决 NO_TRADE。', elapsed: 19 },
  ];
  let mode = 'replay', instrument = 'etf', eventIndex = 3, selected = 'bull', override = null;
  let walkTimer = null, finishTimer = null, fileTimer = null, movementEpoch = 0;
  const walkingRoles = new Set();
  let ambientEnabled = true;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  roles.forEach((r,i) => Object.assign(r, window.labPixelScene.seats[i]));
  const canAnimate = () => ambientEnabled && !reduced.matches && !document.hidden;
  function paintAmbience() {
    window.labPixelScene.sync({roles:roles.map(r=>({...r,state:roleState(r.id)})),selected,active:canAnimate()});
  }
  function stopAmbience() { document.documentElement.dataset.ambient = 'off'; }
  function startAmbience() {
    document.documentElement.dataset.ambient = canAnimate() ? 'on' : 'off';
    q('#ambient').textContent = reduced.matches ? '系统已减少动态效果' : ambientEnabled ? '暂停办公室动态' : '播放办公室动态';
    q('#ambient').setAttribute('aria-pressed', String(ambientEnabled));
    paintAmbience();
  }
  function renderVacancies() {}
  function disabledReason(id) {
    if (id === 'fundamentals' && instrument !== 'stock') return instrument === 'etf' ? 'ETF 没有公司财报；此工位不启用。' : '本示例不使用公司财报分析加密货币。';
    return '';
  }
  function roleState(id) {
    if (override && selected === id) return override;
    if (disabledReason(id)) return 'disabled';
    if (mode === 'rest') return 'waiting';
    const event = sequence[eventIndex];
    if (id === event.role) return event.state;
    if (eventIndex >= 2 && ['market', 'news', 'social', 'fundamentals'].includes(id)) return 'done';
    if (eventIndex >= 5 && ['bull', 'bear'].includes(id)) return 'done';
    if (eventIndex >= 6 && id === 'manager') return 'done';
    if (eventIndex >= 7 && id === 'trader') return 'done';
    if (eventIndex === 7 && ['aggressive', 'conservative'].includes(id)) return 'thinking';
    if (eventIndex === 8 && ['aggressive', 'neutral', 'conservative'].includes(id)) return 'done';
    return 'waiting';
  }
  function stopMotion() {
    stopAmbience();
    movementEpoch += 1;
    clearTimeout(walkTimer); clearTimeout(finishTimer); clearTimeout(fileTimer);
    walkTimer = finishTimer = fileTimer = null; walkingRoles.clear();
    window.labPixelScene.stop();
    q('#flying-file').hidden = true;
    q('#walker').hidden = true;
    q('#walker').dataset.walking = 'false';
    q('#walk-speech').hidden = true;
    q('#walk').textContent = '让所选 Agent 去交流 ↗';
    q('#walk').setAttribute('aria-pressed', 'false');
    renderVacancies();
  }
  function render() {
    const event = sequence[eventIndex];
    q('#now-title').textContent = mode === 'rest' ? '休息中 · 上次研究已归档' : `${roles.find(r => r.id === event.role).name} · ${labels[event.state]}`;
    q('#now-caption').textContent = mode === 'rest' ? '今日还可分析 4 次' : `${event.elapsed} 秒${eventIndex >= 3 && eventIndex <= 5 ? ' · 第 1 轮' : ''}`;
    q('#mode-badge').textContent = { replay: '回放 · 示例', running: '运行中 · 示例', rest: '休息中 · 示例' }[mode];
    q('#symbol').textContent = { stock: 'AAPL', crypto: 'BTC', etf: 'SPY' }[instrument];
    q('#office-title').textContent = `${q('#symbol').textContent} · 研究办公室`;
    q('#replay-controls').hidden = mode !== 'replay';
    q('#rest-note').hidden = mode !== 'rest';
    q('#event-number').textContent = `事件 ${String(eventIndex + 1).padStart(2, '0')} / ${String(sequence.length).padStart(2, '0')}`;
    q('#previous').disabled = eventIndex === 0; q('#next').disabled = eventIndex === sequence.length - 1;
    document.querySelectorAll('[data-instrument]').forEach(b => b.setAttribute('aria-pressed', b.dataset.instrument === instrument));
    q('#team-count').textContent = `${roles.filter(r => !disabledReason(r.id)).length} 个启用`;
    renderVacancies();
    q('#scene-caption').textContent = '按工位朝向入座 · 点击人物查看详情';
    q('#people').innerHTML = roles.map(r => `<button class="person" data-role="${r.id}" data-state="${roleState(r.id)}" data-pose="seated" aria-pressed="${selected === r.id}" aria-label="${r.name}，${labels[roleState(r.id)]}" style="left:${r.x}%;top:${r.y}%;z-index:${Math.round(r.y)}"><span class="desk-label">${r.name} · ${labels[roleState(r.id)]}${['thinking','research'].includes(roleState(r.id)) ? '<span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span>' : ''}</span></button>`).join('');
    q('#roster').innerHTML = [...new Set(roles.map(r => r.zone))].map(zone => `<div class="roster-zone"><div class="zone-name">${zone}</div>${roles.filter(r => r.zone === zone).map(r => `<button class="role-button" data-role="${r.id}" data-state="${roleState(r.id)}" aria-pressed="${r.id === selected}"><span>${r.name}${mode === 'rest' ? `<small class="role-summary">${disabledReason(r.id) || r.summary}</small>` : ''}</span><span class="role-status">${labels[roleState(r.id)]}</span></button>`).join('')}</div>`).join('');
    document.querySelectorAll('[data-role]').forEach(b => b.onclick = () => { selected = b.dataset.role; override = null; render(); });
    const r = roles.find(r => r.id === selected), s = roleState(selected);
    const characterIndex = roles.indexOf(r);
    q('#portrait').style.backgroundImage = "url('vendor/pixel-agents/characters/char_" + characterIndex % 6 + ".png')";
    q('#portrait').style.backgroundSize = '700% 300%';
    q('#portrait').style.backgroundPosition = '0 0';
    q('#portrait').style.filter = characterIndex >= 6 ? 'hue-rotate(30deg)' : '';
    q('#selected-name').textContent = r.name; q('#selected-zone').textContent = r.zone;
    q('#selected-state').textContent = labels[s];
    q('#selected-state').style.color = s === 'error' ? 'var(--lab-error)' : s === 'stalled' ? 'var(--lab-warning)' : '';
    q('#selected-meta').textContent = mode === 'rest' ? '上次运行 · 一句话摘要' : s === 'stalled' ? '8 分钟无新事件' : s === 'disabled' ? '当前标的不适用' : selected === event.role ? `状态已持续 ${event.elapsed} 秒` : '基于最近收到的事件';
    q('#selected-bubble').textContent = mode === 'rest' && s !== 'disabled' ? r.summary : s === 'done' ? r.summary : s === 'thinking' ? '正在整理证据与判断，尚未交出新报告。' : s === 'research' ? '正在查阅资料；已收到查询开始事件。' : s === 'waiting' ? '等待前序报告，尚未开始当前任务。' : s === 'disabled' ? (disabledReason(r.id) || '设计检视：此工位未启用。') : descriptions[s];
    q('#selected-reason').textContent = r.id === 'portfolio' ? '提供综合评级，不拥有最终交易批准权。' : override ? '设计检视状态 · 不改变示例运行事件' : '';
    const resolved = eventIndex === 8 || mode === 'rest';
    q('#gate-result').innerHTML = resolved ? '<div class="gate-grid"><div><span>策略裁决</span><strong>NO_TRADE</strong></div><div><span>硬风控</span><strong>PASS</strong></div></div><p class="caption">规则通过不代表策略要求交易。本次不交易。</p>' : '<p>等待完整研究结果</p><p class="caption">风控与策略裁决尚未到达。</p>';
    q('.approval').textContent = resolved ? '本次不交易 · 无需审批' : '等待规则结果';
    const history = mode === 'rest' ? sequence : sequence.slice(0, eventIndex + 1);
    q('#events').innerHTML = [...history].reverse().slice(0, 5).map(e => `<li><time>${e.time}</time>${e.text}</li>`).join('');
    q('#state-options').innerHTML = Object.entries(labels).map(([key, label]) => `<button data-demo="${key}" aria-pressed="${override === key}">${label}</button>`).join('');
    q('#state-description').textContent = override ? descriptions[override] : '选择一个桌位，再选择状态进行检视。';
    document.querySelectorAll('[data-demo]').forEach(b => b.onclick = () => { stopMotion(); override = b.dataset.demo; render(); if (override === 'done') handoff(); });
    startAmbience();
  }
  function handoff() {
    if (reduced.matches) return;
    const source = roles.find(r => r.id === selected);
    // The final rating leaves the office for the rule panel, not another desk.
    if (source.id === 'portfolio') return;
    const targetId = source.zone === '分析区' ? 'bull' : source.id === 'bull' ? 'bear' : source.id === 'bear' ? 'manager' : source.id === 'manager' ? 'trader' : source.id === 'trader' ? 'neutral' : 'portfolio';
    const target = roles.find(r => r.id === targetId), epoch = movementEpoch;
    const f = q('#flying-file'); f.style.left = `${source.x}%`; f.style.top = `${source.y}%`; f.hidden = false;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (epoch !== movementEpoch || reduced.matches) return;
      f.style.left = `${target.x}%`; f.style.top = `${target.y}%`;
    }));
    fileTimer = setTimeout(() => { f.hidden = true; }, 1350);
  }
  function advance(delta) {
    stopMotion(); override = null; eventIndex = Math.max(0, Math.min(sequence.length - 1, eventIndex + delta));
    selected = sequence[eventIndex].role; render(); if (sequence[eventIndex].state === 'done') handoff();
  }
  q('#previous').onclick = () => advance(-1); q('#next').onclick = () => advance(1);
  q('#mode').onchange = e => { stopMotion(); mode = e.target.value; override = null; render(); };
  document.querySelectorAll('[data-instrument]').forEach(b => b.onclick = () => { stopMotion(); instrument = b.dataset.instrument; override = null; render(); });
  q('#clear-state').onclick = () => { stopMotion(); override = null; render(); };
  q('#theme').onclick = () => { document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; };
  function startWalk() {
    const source=roles.find(r=>r.id===selected);
    if(disabledReason(source.id)){q('#scene-caption').textContent=`${source.name}当前未启用，工位保持空椅子`;return;}
    const targetByRole={market:'bull',news:'bear',social:'bull',fundamentals:'manager',bull:'bear',bear:'manager',manager:'trader',trader:'neutral',aggressive:'portfolio',neutral:'portfolio',conservative:'portfolio',portfolio:'manager'};
    const target=roles.find(r=>r.id===targetByRole[source.id]);
    if (!canAnimate()) {
      q('#scene-caption').textContent=`${source.name}向${target.name}发出交流事件 · 减少动态效果下人物位置不变`;
      q('#selected-bubble').textContent=`已记录：准备与${target.name}交流。`;
      return;
    }
    const result=window.labPixelScene.walk(source.id,target.id,{
      message:`想和你核对一下最新判断。`,
      queued:()=>{q('#scene-caption').textContent=`${source.name}正在等候过道空闲`;},
      start:()=>{q('#walk').textContent='交流行程进行中';q('#scene-caption').textContent=`${source.name}已离开工位，前往${target.name}处交流`;},
      done:()=>{walkingRoles.delete(source.id);q('#walk').textContent=walkingRoles.size?'交流队列进行中':'让所选 Agent 去交流 ↗';q('#walk').setAttribute('aria-pressed',String(walkingRoles.size>0));q('#scene-caption').textContent=`${source.name}交流完成 · 已返回原工位`;paintAmbience();}
    });
    if(result==='duplicate'){q('#scene-caption').textContent=`${source.name}已经在路上或队列中`;return;}
    if(result==='reduced')return;
    walkingRoles.add(source.id);
    q('#walk').textContent=result==='queued'?'已加入交流队列':'交流行程进行中';q('#walk').setAttribute('aria-pressed','true');
  }
  q('#walk').onclick = () => startWalk();
  q('#ambient').onclick = () => { ambientEnabled = !ambientEnabled; stopMotion(); render(); };
  reduced.addEventListener('change', () => { stopMotion(); render(); });
  document.addEventListener('visibilitychange', () => { stopMotion(); render(); });
  render();
})();
