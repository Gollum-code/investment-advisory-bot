/* 火币合约行情分析 · 前端逻辑（原生 JS，零依赖） */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    meta: null,
    watchlist: [],
    symbol: 'BTC-USDT',
    // 默认 swing：与后端 DEFAULT_MODE 一致。intraday 完整历史实测 -0.094R。
    mode: 'swing',
    plan: null,
    snapshot: null,
    klines: [],
    tf: '15min',
busy: false,
    analyzeSeq: 0,      // 分析请求序号：新请求总是胜出，旧响应直接丢弃
    es: null,
    recent: [],
    uniAuto: true,
    uniRows: [],
    uniBusy: false,
    threshold: 30,      // 评分阈值，仪表盘刻度要用（以前写死 ±30）
    winSeq: 0,        // 胜率面板请求序号
    histSeq: 0,       // 历史列表请求序号
  };

  // ---------------------------------------------------------------- 工具
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function digitsFor(p) {
    const v = Math.abs(Number(p) || 0);
    if (!v || v <= 0) return 2;
    return Math.max(2, Math.min(8, 5 - Math.floor(Math.log10(v))));
  }

  function fmtPrice(p, digits) {
    if (p == null || !isFinite(p)) return '--';
    const d = digits == null ? digitsFor(p) : digits;
    const s = Number(p).toFixed(d);
    const [i, f] = s.split('.');
    const sign = i.startsWith('-') ? '-' : '';
    const body = i.replace('-', '').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return sign + body + (f ? '.' + f : '');
  }

  function fmtNum(v, d) {
    if (v == null || !isFinite(v)) return '--';
    return Number(v).toFixed(d == null ? 2 : d);
  }

  function fmtPct(v, d) {
    if (v == null || !isFinite(v)) return '--';
    const n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(d == null ? 2 : d) + '%';
  }

  function signed(v, d) {
    if (v == null || !isFinite(v)) return '--';
    const n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(d == null ? 1 : d);
  }

  function cls(v) {
    if (!v || Math.abs(v) < 1e-9) return 'flat';
    return v > 0 ? 'up' : 'dn';
  }

  function fmtTime(sec, withDate) {
    if (!sec) return '--';
    const d = new Date(sec * 1000);
    const p = (n) => String(n).padStart(2, '0');
    const hm = p(d.getHours()) + ':' + p(d.getMinutes());
    return withDate ? (p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + hm) : hm;
  }

  function modeLabel(k) {
    const m = (state.meta && state.meta.modes || []).find((x) => x.key === k);
    return m ? m.label : k;
  }

  function dirLabel(d) {
    return { long: '做多', short: '做空', wait: '观望' }[d] || d;
  }

  function outcomeLabel(o) {
    return {
      tp1: { label: '命中 TP1', good: true },
      tp2: { label: '命中 TP2', good: true },
      tp3: { label: '命中 TP3', good: true },
      stopped: { label: '触发止损', good: false },
      timeout: { label: '到期未了结', good: false },
      no_fill: { label: '挂单未成交', good: false },
    }[o] || { label: o, good: false };
  }

  function toast(title, body, kind, ms) {
    const wrap = $('toast-wrap');
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.innerHTML = '<div class="tt">' + esc(title) + '</div>' +
      (body ? '<div>' + esc(body) + '</div>' : '');
    wrap.appendChild(el);
    setTimeout(() => el.remove(), ms || 4200);
  }

  function setMsg(text, kind) {
    const el = $('msg');
    el.textContent = text || '';
    el.className = 'msg' + (kind ? ' ' + kind : '');
  }

  function setPill(id, text, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.className = 'pill' + (kind ? ' ' + kind : '');
  }

  async function api(path, opts) {
    opts = opts || {};
    const token = localStorage.getItem('iabot_token');
    const hdrs = Object.assign({}, opts.headers || {});
    if (token) hdrs['Authorization'] = 'Bearer ' + token;
    if (opts.body && !hdrs['Content-Type']) hdrs['Content-Type'] = 'application/json';
    const res = await fetch(path, Object.assign({}, opts, { headers: hdrs }));
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401 && data && data.error && /token|令牌/.test(data.error)) {
      const again = await askToken();
      if (again) return api(path, opts);  // 填入 token 后重试
    }
    if (!res.ok || !data || data.ok === false) {
      throw new Error((data && data.error) || ('HTTP ' + res.status));
    }
    return data;
  }

  // 服务器要求访问令牌时让用户填一次，记到 localStorage，之后自动带
  async function askToken() {
    const cur = localStorage.getItem('iabot_token') || '';
    const tok = window.prompt('本服务设置了访问令牌，请输入（仅保存在本机浏览器）：', cur);
    if (tok === null) return false;
    localStorage.setItem('iabot_token', tok.trim());
    return true;
  }

  function loading(el, text) {
    el.innerHTML = '<div class="loading">' + esc(text || '加载中…') + '</div>';
  }

  // ------------------------------------------------------------ 最近搜索
  // 记在 localStorage 里，刷新页面后仍在；最多保留 RECENT_MAX 条。
  const RECENT_KEY = 'iabot.recent';
  const RECENT_MAX = 8;

  function loadRecent() {
    try {
      const raw = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
      state.recent = Array.isArray(raw)
        ? raw.filter((s) => typeof s === 'string' && s).slice(0, RECENT_MAX) : [];
    } catch (e) {
      state.recent = [];                 // 隐身模式 / 存储被禁用时静默降级
    }
  }

  function saveRecent() {
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(state.recent)); } catch (e) { /* 忽略 */ }
  }

  function pushRecent(sym) {
    if (!sym) return;
    state.recent = [sym].concat(state.recent.filter((s) => s !== sym)).slice(0, RECENT_MAX);
    saveRecent();
    renderRecent();
  }

  function renderRecent() {
    const row = $('recent-row');
    const box = $('recent');
    if (!row || !box) return;
    if (!state.recent.length) { row.hidden = true; box.innerHTML = ''; return; }
    row.hidden = false;
    box.innerHTML = state.recent.map((s) =>
      '<span class="chip' + (s === state.symbol ? ' active' : '') + '" data-recent="' + esc(s) +
      '" title="' + esc(s) + '">' + esc(s.replace(/-USDT$/, '')) + '</span>').join('');
  }

  function clearRecent() {
    state.recent = [];
    saveRecent();
    renderRecent();
  }

  // ---------------------------------------------------------------- 初始化
  async function init() {
    // 先绑事件再拉 meta。/api/meta 要探行情域名，慢的话能卡好几秒
    // （配了 token 时还会弹 window.prompt）。绑在 await 之后的话，
    // 这段时间里整个面板点了没反应——而权重/方案/任务面板根本不依赖 meta。
    bindEvents();
    connectSSE();
    try {
      const meta = await api('/api/meta');
      state.meta = meta;
      state.watchlist = (meta.watchlist || []).slice();
      if (!state.watchlist.length) state.watchlist = ['BTC-USDT', 'ETH-USDT', 'SOL-USDT'];

      const dl = $('sym-list');
      dl.innerHTML = (meta.symbols || [])
        .map((s) => '<option value="' + esc(s.code) + '">' + esc(s.base + ' ' + s.name) + '</option>')
        .join('');

      const def = meta.defaults || {};
      const acc = def.account || {};
      if (acc.equity_usdt) $('equity').value = acc.equity_usdt;
      if (acc.preferred_leverage) $('leverage').value = acc.preferred_leverage;
      if (acc.risk_pct_per_trade) $('risk').value = acc.risk_pct_per_trade;

      const cert = meta.cert || {};
      setPill('pill-cert', '证书 ' + (cert.ca_count || 0) + ' 个' + (cert.verifying ? '' : '（未校验）'),
        cert.ca_count ? 'ok' : 'bad');

      loadRecent();
      renderRecent();
      applyScheduleUI((def.schedule) || {});
      loadHealth();
      loadHistory();
      loadSchedule();
      loadWinStats();
      loadWeights();
      loadProfiles();
      loadPositions();
      // 雷达是全市场一次性请求，等首个分析跑完再拉，免得开局就撞限流
      // （原来这里只是 setTimeout 1500ms，而 analyze 根本没 await，
      //   首屏分析通常要好几秒，等于压根没让路）
      await analyze(false);
      if (state.uniAuto) loadUniverse(false);
    } catch (err) {
      setMsg('初始化失败：' + err.message, 'err');
      toast('初始化失败', err.message, 'err');
    }
  }

  async function loadHealth() {
    try {
      const d = await api('/api/health');
      const cur = (d.bases || []).find((b) => b.current);
      setPill('pill-base', cur ? ('行情域名 ' + cur.base.replace(/^https?:\/\//, '')) : '行情域名 --',
        cur ? 'ok' : 'bad');
    } catch (e) {
      setPill('pill-base', '行情域名 不可用', 'bad');
    }
  }

  function bindEvents() {
    $('btn-go').onclick = () => analyze(false);
    $('btn-refresh').onclick = () => analyze(true);
    $('mode').onchange = () => { state.mode = $('mode').value; analyze(true); };
    $('symbol').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); analyze(false); }
    });
    $('quick').addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      $('symbol').value = chip.dataset.sym;
      analyze(false);
    });
    $('recent').addEventListener('click', (e) => {
      const chip = e.target.closest('[data-recent]');
      if (!chip) return;
      $('symbol').value = chip.dataset.recent;
      analyze(false);
    });
    $('recent-clear').onclick = clearRecent;
    $('equity').onchange = $('leverage').onchange = $('risk').onchange = () => analyze(true);
    $('btn-run').onclick = runNow;
    $('btn-test-notify').onclick = testNotify;
    $('win-days').onchange = () => loadWinStats();
    $('btn-weights-save').onclick = () => saveWeights(false);
    $('btn-weights-reset').onclick = () => saveWeights(true);
    $('btn-prof-save').onclick = saveProfile;
    $('btn-prof-cmp').onclick = compareProfiles;
    $('btn-bt-reload').onclick = loadBacktests;
    $('btn-pos-open').onclick = openPosition;
    $('btn-report').onclick = openReport;
    $('pos-entry').oninput = () => {
      // 之前是 onclick + "仅当输入框为空"：先填了价再想填现价就点不动，
      // 而且合约不一致时会填错价（fillMarkPrice 现在自带校验）。
      if (!$('pos-entry').value) fillMarkPrice();
    };
    $('btn-save-sched').onclick = saveSchedule;
    $('btn-clear-hist').onclick = clearHistory;
    $('btn-scan').onclick = scan;
    $('btn-scan-all').onclick = () => scan(true);
    $('btn-uni-reload').onclick = () => loadUniverse(true);
    $('btn-uni-scan').onclick = () => scan(true);
    $('uni-autoload').onchange = (e) => { state.uniAuto = e.target.checked; if (state.uniAuto) loadUniverse(false); };
    $('sc-uni').onchange = () => {
      const on = $('sc-uni').checked;
      $('sc-uni-batch').disabled = !on;
      $('sc-symbols').disabled = on;
    };
    $('tf-seg').addEventListener('click', (e) => {
      const b = e.target.closest('.seg-btn');
      if (!b) return;
      state.tf = b.dataset.tf;
      markSeg();
      renderChart();
    });
  }

  // ---------------------------------------------------------------- 分析
  async function analyze(force) {
    const sym = ($('symbol').value || '').trim().toUpperCase();
    if (!/[A-Za-z0-9\u4e00-\u9fff]/.test(sym)) { return setMsg('请输入合约代码，例如 BTC-USDT 或 牛来-USDT', 'err'); }
    $('symbol').value = sym;
    // 用请求序号而不是 busy 直接丢弃：以前第二次点击会直接 return，
    // 但输入框已经被改成新币，于是出现「框里是 ETH、面板显示 BTC」，
    // 而且 ETH 永远不会被分析。序号保证新请求总是胜出、旧响应不再覆盖。
    const seq = ++state.analyzeSeq;
    state.symbol = sym;
    state.mode = $('mode').value;
    $('btn-go').disabled = $('btn-refresh').disabled = true;
    setMsg('正在拉取 ' + sym + ' 行情并计算…');
    loading($('advice'), '计算中…');
    loading($('factors'), '计算中…');
    try {
      const q = new URLSearchParams({
        symbol: sym, mode: state.mode,
        equity: $('equity').value || '450',
        leverage: $('leverage').value || '10',
        risk_pct: $('risk').value || '2',
      });
      if (force) q.set('force', '1');
      const d = await api('/api/analyze?' + q.toString());
      if (seq !== state.analyzeSeq) return;   // 已有更新的请求，丢弃这份过期结果
      state.plan = d.plan;
      state.snapshot = d.snapshot;
      buildSeg(d.snapshot.klines || {});
      renderQuote();
      renderChart();
      renderAdvice(d.plan);
      renderFactors(d.plan);
      renderLadder(d.plan);
      setMsg('已更新 · ' + fmtTime(d.plan.generated_at, true) +
        (d.snapshot.errors && d.snapshot.errors.length ? ' · 部分接口异常：' + d.snapshot.errors.join('；') : ''),
        d.snapshot.errors && d.snapshot.errors.length ? '' : 'ok');
      logLine('ok', sym + ' ' + dirLabel(d.plan.direction) + ' 评分 ' + signed(d.plan.score));
      pushRecent(sym);
    } catch (err) {
      if (seq !== state.analyzeSeq) return;
      setMsg('分析失败：' + err.message, 'err');
      $('advice').innerHTML = '<div class="empty">分析失败。</div>';
      $('factors').innerHTML = '<div class="empty">分析失败。</div>';
      toast('分析失败', err.message, 'err');
    } finally {
      if (seq === state.analyzeSeq) $('btn-go').disabled = $('btn-refresh').disabled = false;
    }
  }

  function buildSeg(klines) {
    const keys = Object.keys(klines);
    const seg = $('tf-seg');
    seg.innerHTML = keys.map((k) =>
      '<button class="seg-btn" data-tf="' + esc(k) + '">' + esc(k) + '</button>').join('');
    // 超卖反弹和波段都是日线判据，默认摊日线那几档
    const pref = (state.mode === 'swing' || state.mode === 'reversion')
      ? ['1day', '60min', '4hour'] : ['15min', '5min', '60min'];
    state.tf = pref.find((p) => keys.indexOf(p) >= 0) || keys[0] || '15min';
    markSeg();
  }

  function markSeg() {
    Array.from($('tf-seg').children).forEach((b) =>
      b.classList.toggle('active', b.dataset.tf === state.tf));
  }

  // ---------------------------------------------------------------- 行情头
  function renderQuote() {
    const s = state.snapshot || {};
    const p = state.plan || {};
    const d = digitsFor(s.price);
    const chg = s.change_24h_pct;
    const parts = [
      '<span class="q-price">' + fmtPrice(s.price, d) + '</span>',
      '<span class="' + cls(chg) + '">' + fmtPct(chg) + ' 24h</span>',
      '高 ' + fmtPrice(s.high, d) + ' / 低 ' + fmtPrice(s.low, d),
      '基差 ' + fmtPct(s.basis_pct, 3),
      '资金费率 ' + (s.funding_rate == null ? '--' : (s.funding_rate * 100).toFixed(4) + '%'),
      '持仓量 ' + fmtNum(s.open_interest, 0) + ' 张',
      (p.market && p.market.regime ? '状态 ' + p.market.regime : ''),
      (p.market && p.market.adx != null ? 'ADX ' + p.market.adx : ''),
      (p.market && p.market.atr_pct != null ? 'ATR ' + p.market.atr_pct + '%' : ''),
    ].filter(Boolean);
    $('quote').innerHTML = parts.join(' · ');
  }

  // ---------------------------------------------------------------- K 线
  function renderChart() {
    const cv = $('chart');
    const rows = (state.snapshot && state.snapshot.klines && state.snapshot.klines[state.tf]) || [];
    const ctx = cv.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const cssW = cv.clientWidth || 800;
    const cssH = cv.clientHeight || 360;
    cv.width = Math.round(cssW * dpr);
    cv.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    if (!rows.length) {
      $('chart-legend').textContent = '暂无 K 线数据';
      return;
    }
    const data = rows.slice(-160);
    const padL = 6, padR = 68, padT = 12, padB = 20, volH = 54;
    const plotW = Math.max(40, cssW - padL - padR);
    const plotH = Math.max(40, cssH - padT - padB - volH);
    const plan = state.plan || {};
    const planPrices = [];
    if (plan.entry_low) planPrices.push(plan.entry_low);
    if (plan.entry_high) planPrices.push(plan.entry_high);
    if (plan.stop) planPrices.push(plan.stop);
    (plan.targets || []).forEach((t) => planPrices.push(t.price));

    let lo = Math.min.apply(null, data.map((c) => c.low));
    let hi = Math.max.apply(null, data.map((c) => c.high));
    const span0 = hi - lo || hi * 0.01 || 1;
    planPrices.forEach((p) => {
      if (p >= lo - span0 * 0.6 && p <= hi + span0 * 0.6) { lo = Math.min(lo, p); hi = Math.max(hi, p); }
    });
    const pad = (hi - lo) * 0.06 || 0.001;
    lo -= pad; hi += pad;
    const y = (p) => padT + (hi - p) / (hi - lo) * plotH;
    const bw = plotW / data.length;
    const cw = Math.max(1, bw * 0.68);

    // 网格 + 价格轴
    ctx.font = '11px "Segoe UI",system-ui,sans-serif';
    ctx.textBaseline = 'middle';
    const dg = digitsFor(hi);
    for (let i = 0; i <= 4; i++) {
      const p = lo + (hi - lo) * (i / 4);
      const yy = Math.round(y(p)) + 0.5;
      ctx.strokeStyle = '#1a2340';
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
      ctx.fillStyle = '#8f9cbb';
      ctx.fillText(fmtPrice(p, dg), padL + plotW + 6, yy);
    }

    // 成交量
    const maxVol = Math.max.apply(null, data.map((c) => c.volume)) || 1;
    const volTop = padT + plotH + 8;
    data.forEach((c, i) => {
      const h = (c.volume / maxVol) * (volH - 6);
      ctx.fillStyle = c.close >= c.open ? 'rgba(34,197,94,.45)' : 'rgba(239,68,68,.45)';
      ctx.fillRect(padL + i * bw + (bw - cw) / 2, volTop + (volH - 6) - h, cw, h);
    });

    // K 线
    data.forEach((c, i) => {
      const cx = padL + i * bw + bw / 2;
      const up = c.close >= c.open;
      const color = up ? '#22c55e' : '#ef4444';
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(cx) + 0.5, y(c.high));
      ctx.lineTo(Math.round(cx) + 0.5, y(c.low));
      ctx.stroke();
      const yo = y(c.open), yc = y(c.close);
      const top = Math.min(yo, yc);
      const h = Math.max(1, Math.abs(yc - yo));
      ctx.fillRect(cx - cw / 2, top, cw, h);
    });

    // MA20
    const closes = data.map((c) => c.close);
    ctx.strokeStyle = 'rgba(59,130,246,.95)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < data.length; i++) {
      if (i < 19) continue;
      let sum = 0;
      for (let j = i - 19; j <= i; j++) sum += closes[j];
      const cx = padL + i * bw + bw / 2;
      const yy = y(sum / 20);
      if (!started) { ctx.moveTo(cx, yy); started = true; } else ctx.lineTo(cx, yy);
    }
    ctx.stroke();

    // 计划价位
    function hline(p, color, label, dash) {
      if (p == null || !isFinite(p)) return;
      const yy = Math.round(y(p)) + 0.5;
      if (yy < padT - 4 || yy > padT + plotH + 4) return;
      ctx.save();
      ctx.setLineDash(dash || []);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
      ctx.restore();
      if (label) {
        ctx.fillStyle = color;
        ctx.textAlign = 'right';
        ctx.fillText(label, padL + plotW - 4, yy - 7);
        ctx.textAlign = 'left';
      }
    }
    if (plan.entry_low && plan.entry_high) {
      const y1 = y(Math.max(plan.entry_low, plan.entry_high));
      const y2 = y(Math.min(plan.entry_low, plan.entry_high));
      ctx.fillStyle = 'rgba(59,130,246,.16)';
      ctx.fillRect(padL, y1, plotW, Math.max(2, y2 - y1));
      ctx.fillStyle = 'rgba(59,130,246,.9)';
      ctx.fillText('入场区', padL + 4, (y1 + y2) / 2);
    }
    hline(plan.stop, 'rgba(239,68,68,.9)', '止损', [5, 4]);
    (plan.targets || []).forEach((t) => hline(t.price, 'rgba(34,197,94,.85)', t.label, [5, 4]));
    const last = data[data.length - 1];
    hline(last.close, 'rgba(232,237,247,.5)', '', [2, 3]);

    // 时间轴
    ctx.fillStyle = '#8f9cbb';
    [0, Math.floor(data.length / 2), data.length - 1].forEach((i) => {
      const c = data[i];
      if (!c) return;
      ctx.fillText(fmtTime(c.ts, true), padL + i * bw + bw / 2 - 26, cssH - 8);
    });

    // 图例
    const chg = last.close >= last.open;
    let ma20 = null;
    if (closes.length >= 20) {
      let s = 0; for (let j = closes.length - 20; j < closes.length; j++) s += closes[j];
      ma20 = s / 20;
    }
    $('chart-legend').innerHTML = [
      '开 ' + fmtPrice(last.open, dg),
      '高 ' + fmtPrice(last.high, dg),
      '低 ' + fmtPrice(last.low, dg),
      '收 <b class="' + (chg ? 'up' : 'dn') + '">' + fmtPrice(last.close, dg) + '</b>',
      'MA20 <b>' + fmtPrice(ma20, dg) + '</b>',
      esc(state.tf) + ' × ' + data.length + ' 根',
    ].join(' · ');
  }

  // ---------------------------------------------------------------- 建议
  function renderAdvice(plan) {
    const el = $('advice');
    if (!plan || plan.price == null) {
      el.innerHTML = '<div class="empty">拿不到行情，无法给出建议。</div>';
      $('card-advice').className = 'card dir-wait';
      return;
    }
    const d = digitsFor(plan.price);
    const dir = plan.direction;
    let h = '';
    h += '<div class="adv-head ' + dir + '">' +
      '<span class="adv-dir">' + dirLabel(dir) + (dir === 'long' ? ' LONG' : dir === 'short' ? ' SHORT' : ' WAIT') + '</span>' +
      '<span class="adv-score">' + signed(plan.score) + '</span>' +
      '<span class="adv-conf">置信度 ' + esc(plan.confidence || '--') + '</span>' +
      '<span class="adv-meta">' + esc(plan.symbol) + ' · ' + esc(modeLabel(plan.mode)) +
      ' · ' + fmtTime(plan.generated_at, true) + '</span></div>';

    // 评分给了方向、却被风控闸门拦下时，把“为什么”直接写出来
    const gate = plan.gate || {};
    if (dir === "wait" && gate.blocked) {
      const cn = gate.would_be === "long" ? "做多" : "做空";
      const why = gate.reason === "no_target"
        ? "前方没有像样的空间（价格卡在关键位中间）"
        : "第一目标盈亏比只有 " + fmtNum(gate.rr, 2) + "，低于 " + fmtNum(gate.need, 1);
      h += '<div class="gate"><b>评分 ' + signed(gate.score) + " 其实指向" + cn +
        "</b>，但" + esc(why) + "，所以按规则转为观望。" +
        "评分只决定方向多强，盈亏比决定这个位置值不值得进。</div>";
    }

    const w = Math.abs(Math.max(-100, Math.min(100, plan.score))) / 100 * 50;
    h += '<div class="gauge"><i style="left:' + (plan.score >= 0 ? 50 : 50 - w) + '%;width:' + w + '%"></i>' +
      '<div class="mid"></div></div>' +
      '<div class="gauge-lbl"><span>-100 空</span><span>±' +
      (state.threshold != null ? state.threshold : 30) + ' 阈值</span><span>+100 多</span></div>';

    if (plan.entry_note) h += '<div class="adv-note">' + esc(plan.entry_note) + '</div>';

    const entry = (plan.entry_low != null && plan.entry_high != null)
      ? fmtPrice(plan.entry_low, d) + ' – ' + fmtPrice(plan.entry_high, d) : '--';
    h += '<div class="kvgrid">' +
      kv('现价', fmtPrice(plan.price, d), '') +
      kv('入场区间', entry, 'wide') +
      kv('止损', plan.stop != null ? fmtPrice(plan.stop, d) + '（' + fmtNum(plan.stop_pct, 2) + '%）' : '--', dir) +
      kv('盈亏比', plan.rr != null ? fmtNum(plan.rr, 2) : '--', plan.rr >= 1.5 ? 'long' : '') +
      '</div>';

    const sizing = plan.sizing || {};
    const tps = {};
    (sizing.targets || []).forEach((t) => { tps[t.label] = t; });
    if ((plan.targets || []).length) {
      h += '<div class="tp-list">';
      plan.targets.forEach((t) => {
        const s = tps[t.label] || {};
        h += '<div class="tp-row"><span class="lbl">' + esc(t.label) + '</span>' +
          '<span class="p">' + fmtPrice(t.price, d) + '</span>' +
          '<span class="rr">RR ' + fmtNum(t.rr, 2) + '</span>' +
          '<span class="src">' + esc(t.source || '') + '</span>' +
          (s.profit_usdt != null ? '<span class="gain">+' + fmtNum(s.profit_usdt, 2) + ' U (' +
            fmtNum(s.profit_pct_of_equity, 1) + '%)</span>' : '') +
          '</div>';
      });
      h += '</div>';
    }

    if (sizing.applicable) {
      const capTag = sizing.leverage_capped ? '<span class="tag warn">已降杠杆</span>' : '';
      h += '<div class="kvgrid">' +
        kv('下单张数', sizing.contracts + ' 张（' + fmtNum(sizing.position_coin, 6) + ' 币）', 'wide') +
        kv('名义价值', fmtNum(sizing.notional_usdt, 2) + ' U', '') +
        kv('占用保证金', fmtNum(sizing.margin_usdt, 2) + ' U（' +
          fmtNum(sizing.margin_pct_of_equity, 1) + '%）', '') +
        kv('止损亏损', fmtNum(sizing.loss_if_stopped_usdt, 2) + ' U（' +
          fmtNum(sizing.loss_pct_of_equity, 2) + '%）', '') +
        (sizing.liquidation_price != null
          ? kv('强平价', fmtPrice(sizing.liquidation_price, d) + ' ' + capTag,
               sizing.leverage_capped ? 'bad' : '')
          : '') +
        kv('杠杆 / 手续费', sizing.leverage + 'x / ' + fmtNum(sizing.fee_estimate_usdt, 3) + ' U', '') +
        '</div>';
      if (sizing.note) h += '<div class="sizing-note">' + esc(sizing.note) + '</div>';
    } else if (sizing.note) {
      h += '<div class="adv-note">仓位：' + esc(sizing.note) + '</div>';
    }

    // 一键以本计划开模拟仓：把入场/止损/止盈自动带进模拟盘
    if (plan.direction === 'long' || plan.direction === 'short') {
      h += '<div class="adv-cta">' +
        '<button class="ghost open-paper" data-act="open">以本计划开模拟仓</button>' +
        '<button class="ghost copy-paper" data-act="copy">复制计划</button>' +
        '<span class="hint">复制计划方便手动下单；仅记录模拟仓位，不下单</span></div>';
    }

    if (plan.invalidation) h += '<div class="adv-note">失效条件：' + esc(plan.invalidation) + '</div>';
    (plan.warnings || []).forEach((t) => { h += '<div class="warnbox">⚠ ' + esc(t) + '</div>'; });
    (plan.notes || []).forEach((t) => { h += '<div class="adv-note">' + esc(t) + '</div>'; });
    if (!plan.warnings.length && plan.direction !== 'wait') {
      h += '<div class="warnbox ok">✔ 未发现风险提示（仍然只是分析，不构成投资建议）</div>';
    }
    el.innerHTML = h;
    $('card-advice').className = 'card dir-' + dir;
    const cta = el.querySelector('.open-paper');
    if (cta) cta.onclick = () => openFromPlan(plan);
    const cp = el.querySelector('.copy-paper');
    if (cp) cp.onclick = () => copyPlan(plan);
  }

  async function copyPlan(plan) {
    const d = digitsFor(plan.price);
    const s = plan.sizing || {};
    const lines = [
      plan.symbol + ' ' + dirLabel(plan.direction) +
        '（评分 ' + signed(plan.score) + '，置信度' + plan.confidence + '）',
      '入场区间 ' + fmtPrice(plan.entry_low, d) + ' - ' + fmtPrice(plan.entry_high, d),
      '止损 ' + fmtPrice(plan.stop, d) + '（' + fmtNum(plan.stop_pct, 2) + '%）',
      '盈亏比 ' + fmtNum(plan.rr, 2),
    ];
    (plan.targets || []).forEach((t) => {
      lines.push(t.label + ' ' + fmtPrice(t.price, d) + '（RR ' + fmtNum(t.rr, 2) + '）');
    });
    if (s.applicable) {
      lines.push('仓位 ' + s.contracts + ' 张 / 名义 ' + fmtNum(s.notional_usdt, 0) +
        ' U / 保证金 ' + fmtNum(s.margin_usdt, 2) + ' U / 止损亏 ' +
        fmtNum(s.loss_if_stopped_usdt, 2) + ' U');
    }
    if (plan.entry_note) lines.push('说明 ' + plan.entry_note);
    const text = lines.join('\n');
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制', '交易计划已复制到剪贴板', 'ok');
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      toast('已复制', '交易计划已复制到剪贴板', 'ok');
    }
  }

  async function openFromPlan(plan) {
    const s = plan.sizing || {};
    $('pos-sym').value = plan.symbol;
    $('pos-dir').value = plan.direction;
    $('pos-entry').value = plan.price;
    if (s.position_coin) $('pos-size').value = s.position_coin;
    if (s.leverage) $('pos-lev').value = s.leverage;
    $('pos-stop').value = plan.stop != null ? plan.stop : '';
    const tp1 = (plan.targets || [])[0];
    $('pos-tp1').value = tp1 ? tp1.price : '';
    $('positions').scrollIntoView({ behavior: 'smooth', block: 'center' });
    await openPosition();
  }

  function kv(label, value, kind) {
    return '<div class="kv ' + (kind || '') + '"><span>' + esc(label) + '</span><b>' + value + '</b></div>';
  }

  // ---------------------------------------------------------------- 因子
  // 因子瀑布：把"评分 = 各项贡献之和"这件事直接画出来。
  // 之前只列了每项的得分/权重/贡献，用户还得自己累加猜总分；
  // 现在瀑布图按贡献从大到小堆叠，一眼看出是谁把分数推到阈值那边去的。
  function renderFactors(plan) {
    const el = $('factors');
    const fs = (plan && plan.factors) || [];
    if (!fs.length) { el.innerHTML = '<div class="empty">暂无因子数据。</div>'; return; }
    $('factor-hint').textContent = '加权 ' + fs.length + ' 项，贡献 = 得分 × 权重';
    const sorted = fs.slice().sort((a, b) =>
      Math.abs(b.contribution || 0) - Math.abs(a.contribution || 0));
    const total = sorted.reduce((s, f) => s + (f.contribution || 0), 0);
    const score = (plan && plan.score) || 0;
    const thr = plan && plan.market && plan.market.threshold;
    const peak = Math.max(...sorted.map((f) => Math.abs(f.contribution || 0)), 0.0001);
    // 0 线在瀑布中间：正向贡献往右、负向往左，同一根柱子上并排
    const bar = (f) => {
      const c = f.contribution || 0;
      const w = Math.max(1.5, (Math.abs(c) / peak) * 50);
      const left = c >= 0 ? 50 : 50 - w;
      return '<div class="f-bar"><i class="' + (c >= 0 ? 'up' : 'dn') +
        '" style="left:' + left.toFixed(1) + '%;width:' + w.toFixed(1) + '%"></i></div>';
    };
    let html = '<div class="f-waterfall">';
    html += '<div class="fw-row fw-head"><span class="fw-k">因子</span>' +
      '<span class="fw-v">贡献</span></div>';
    for (const f of sorted) {
      const c = cls(f.score);
      html += '<div class="fw-row"><span class="fw-k" title="' + esc(f.detail || '') +
        '">' + esc(f.label) + '</span>' +
        '<span class="fw-bar">' + bar(f) + '</span>' +
        '<span class="fw-v ' + c + '">' + signed(f.contribution, 3) + '</span></div>';
    }
    html += '<div class="fw-row fw-total"><span class="fw-k">合计</span>' +
      '<span class="fw-bar"></span><span class="fw-v ' + cls(total) + '">' +
      signed(total, 3) + '</span></div>';
    if (thr != null) {
      html += '<div class="fw-row fw-gate"><span class="fw-k">开仓阈值 ±' +
        fmtNum(thr, 0) + '</span><span class="fw-bar"></span>' +
        '<span class="fw-v ' + cls(score) + '">当前 ' + signed(score, 2) + '</span></div>';
    }
    html += '</div>';
    // 明细仍然保留：瀑布给"谁在推"，明细给"每项到底说了什么"
    html += '<div class="f-detail-list">' + sorted.map((f) =>
      '<div class="factor"><div class="f-top">' +
      '<span class="f-label">' + esc(f.label) + '</span>' +
      '<span class="hint">权重 ' + fmtNum(f.weight, 2) + '</span>' +
      '<span class="f-val ' + cls(f.score) + '">' + signed(f.score, 2) + '</span></div>' +
      '<div class="f-detail">' + esc(f.detail || '') + '</div></div>').join('') + '</div>';
    el.innerHTML = html;
  }

  // ---------------------------------------------------------------- 价位阶梯
  function renderLadder(plan) {
    const d = digitsFor(plan && plan.price);
    const levels = (plan && plan.levels) || {};
    const resist = (levels.resistance || []).slice(0, 5);
    const support = (levels.support || []).slice(0, 5);
    $('res-list').innerHTML = resist.length ? resist.map((l) => ladItem(l, 'res', d)).join('')
      : '<div class="empty">附近没有明显阻力位。</div>';
    $('sup-list').innerHTML = support.length ? support.map((l) => ladItem(l, 'sup', d)).join('')
      : '<div class="empty">附近没有明显支撑位。</div>';
  }

  function ladItem(l, kind, d) {
    return '<div class="lad-item ' + kind + '">' +
      '<span class="p">' + fmtPrice(l.price, d) + '</span>' +
      '<span class="src" title="' + esc(l.detail || '') + '">' + esc(l.source) + '</span>' +
      '<span class="wbar"><i style="width:' + Math.round((l.weight || 0) * 100) + '%"></i></span>' +
      '<span class="w">' + Math.round((l.weight || 0) * 100) + '%</span></div>';
  }

  // ---------------------------------------------------------------- 扫描
  // ---------------------------------------------------------------- 市场情绪
  function renderBreadth(b) {
    if (!b || !b.scanned) return '';
    const bull = Math.round((b.bull_ratio || 0) * 100);
    const bear = Math.round((b.bear_ratio || 0) * 100);
    const sent = { 偏多: 'good', 偏空: 'bad', 中性: '' }[b.sentiment] || '';
    return '<div class="breadth">' +
      '<span class="bd-sent ' + sent + '">' + esc(b.sentiment || '--') + '</span>' +
      '<div class="bd-bar">' +
      '<i class="long" style="width:' + bull + '%"></i>' +
      '<i class="short" style="width:' + bear + '%"></i>' +
      '</div>' +
      '<span class="bd-nums">' + (b.longs || 0) + ' 多 / ' + (b.shorts || 0) +
      ' 空 / ' + (b.waits || 0) + ' 观望' +
      ' · 平均 |评分| ' + fmtNum(b.avg_abs_score, 1) + '</span>' +
      '</div>';
  }

  function uniQuery() {
    const q = new URLSearchParams({
      top: $('uni-top').value || '40',
      min_turnover: $('uni-turnover').value || '100000',
      sort: $('uni-sort').value || 'turnover',
    });
    if ($('uni-allassets').checked) q.set('all_assets', '1');
    return q;
  }

  async function loadUniverse(force) {
    const box = $('universe');
    if (state.uniBusy) return;
    state.uniBusy = true;
    if (force || !state.uniRows.length) loading(box, '拉取全市场行情中（一次请求拉回所有合约）…');
    try {
      const d = await api('/api/universe?' + uniQuery().toString());
      state.uniRows = d.candidates || [];
      renderUniverse(d);
    } catch (err) {
      box.innerHTML = '<div class="empty">雷达加载失败：' + esc(err.message) + '</div>';
      $('uni-hint').textContent = '全市场行情获取失败';
    } finally {
      state.uniBusy = false;
    }
  }

  const UNI_CLASS = {
    crypto: '加密', stock: '股票', tradfi: 'TradFi',
    commodity: '商品', index: '指数',
  };

  function renderUniverse(d) {
    const st = d.stats || {};
    $('uni-hint').textContent = '交易所共 ' + (st.total || 0) + ' 个 USDT 永续（加密 ' +
      (st.crypto || 0) + ' / 股票类 ' + ((st.stock || 0) + (st.tradfi || 0)) +
      '），成交额 ≥ ' + fmtCompact(st.threshold || 0) + ' U 的有 ' +
      (st.picked || 0) + ' 个';

    const rows = state.uniRows || [];
    if (!rows.length) {
      $('universe').innerHTML =
        '<div class="empty">没有合约通过筛选——把「24h 成交额下限」调低试试。</div>';
      return;
    }
    const body = rows.map((r) => {
      const chg = r.change_pct;
      const dd = digitsFor(r.price);
      const dir = r.direction || '';
      return '<tr data-sym="' + esc(r.symbol) + '">' +
        '<td class="us">' + esc(r.symbol) + '</td>' +
        '<td class="uni-tag ' + esc(r.asset_class) + '">' +
          esc(UNI_CLASS[r.asset_class] || r.asset_class) + '</td>' +
        '<td class="uni-px">' + fmtPrice(r.price, dd) + '</td>' +
        '<td class="' + cls(chg || 0) + '">' +
          (chg == null ? '--' : signed(chg) + '%') + '</td>' +
        '<td class="uni-to">' + fmtCompact(r.turnover) + '</td>' +
        '<td class="uni-sc ' + cls(r.score || 0) + '">' +
          (r.score == null ? '--' : signed(r.score)) +
          (dir && dir !== 'wait' ? ' ' + dirLabel(dir) : '') + '</td>' +
        '</tr>';
    }).join('');

    $('universe').innerHTML =
      '<table class="uni-tbl"><thead><tr><th>合约</th><th>类别</th><th>现价</th>' +
      '<th>24h</th><th>24h 成交额</th><th>信号</th></tr></thead><tbody>' +
      body + '</tbody></table>';
    $('universe').onclick = (e) => {
      const tr = e.target.closest('tr[data-sym]');
      if (!tr) return;
      $('symbol').value = tr.dataset.sym;
      analyze(false);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    };
  }

  function fmtCompact(v) {
    if (v == null || !isFinite(v)) return '--';
    const n = Number(v);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + '万';
    return fmtNum(n, 0);
  }

  async function scan(full) {
    // 两个扫描按钮 + 深挖按钮一起禁：它们会互相触发（深挖就是全市场扫描），
    // 只禁一个的话能从另一个再点进来，重复打同一批接口。
    const btns = [$('btn-scan'), $('btn-scan-all'), $('btn-uni-scan')];
    btns.forEach((b) => { b.disabled = true; });
    loading($('scan'), full
      ? '全市场扫描中：先按流动性筛候选，再并发深挖（越慢说明候选越多）…'
      : '扫描中，逐个合约拉取行情（约 3-10 秒/个，并发执行）…');
    try {
      const q = new URLSearchParams({
        symbols: full ? 'all' : state.watchlist.join(','),
        mode: $('mode').value,
        equity: $('equity').value || '450',
        leverage: $('leverage').value || '10',
        risk_pct: $('risk').value || '2',
        workers: $('uni-workers').value || '4',
      });
      if (full) for (const [k, v] of uniQuery()) q.set(k, v);
      const d = await api('/api/scan?' + q.toString());
      const plans = d.plans || [];
      if (!plans.length) {
        $('scan').innerHTML = '<div class="empty">没有结果。' +
          (d.error ? esc(d.error) : '') + '</div>';
        if (d.error) toast('全市场扫描', d.error, 'err', 8000);
        return;
      }
      const bd = d.breadth || {};
      $('scan').innerHTML = renderBreadth(bd) + plans.map((p) => {
        const dd = digitsFor(p.price);
        const s = p.sizing || {};
        return '<div class="scan-row ' + p.direction + '" data-sym="' + esc(p.symbol) + '">' +
          '<div class="top"><span class="sym">' + esc(p.symbol) + '</span>' +
          '<span class="dir ' + p.direction + '">' + dirLabel(p.direction) + '</span>' +
          '<span class="sc ' + cls(p.score) + '">' + signed(p.score) + '</span></div>' +
          '<div class="meta"><span>现价 <b>' + fmtPrice(p.price, dd) + '</b></span>' +
          '<span>RR <b>' + (p.rr != null ? fmtNum(p.rr, 2) : '--') + '</b></span>' +
          '<span>止损 <b>' + (p.stop_pct != null ? fmtNum(p.stop_pct, 2) + '%' : '--') + '</b></span>' +
          (s.applicable ? '<span>' + s.contracts + ' 张 / ' + fmtNum(s.notional_usdt, 0) + ' U</span>' : '') +
          '</div></div>';
      }).join('');
      $('scan').onclick = (e) => {
        const row = e.target.closest('.scan-row');
        if (!row) return;
        $('symbol').value = row.dataset.sym;
        analyze(false);
        window.scrollTo({ top: 0, behavior: 'smooth' });
      };
      const label = full ? '全市场扫描' : '扫描';
      logLine('ok', label + '完成：' + plans.length + ' 个合约 / 耗时 ' +
        (d.elapsed != null ? d.elapsed + 's' : '未知'));
      if (full) logLine('ok', '扫描范围：' + (d.scanned || plans.length) + ' 个候选');
      if ((d.errors || []).length) toast('部分合约失败', d.errors.slice(0, 5).join('；'), 'err', 6000);
      // 扫描完刷新雷达，顺带把新评分显示到雷达表上
      if (full && state.uniAuto) loadUniverse(false);
    } catch (err) {
      $('scan').innerHTML = '<div class="empty">扫描失败：' + esc(err.message) + '</div>';
      toast('扫描失败', err.message, 'err');
    } finally {
      btns.forEach((b) => { b.disabled = false; });
    }
  }

  // ---------------------------------------------------------------- 定时任务
  function applyScheduleUI(sc) {
    $('sc-enabled').checked = !!sc.enabled;
    $('sc-interval').value = sc.interval_sec || 300;
    $('sc-symbols').value = (sc.symbols || ['BTC-USDT']).join(', ');
    $('sc-onlysig').checked = !!sc.only_on_signal;
    const u = sc.universe || {};
    $('sc-uni').checked = !!u.enabled;
    $('sc-uni-batch').value = u.batch_size || 20;
    $('sc-uni-batch').disabled = !u.enabled;
    $('sc-symbols').disabled = !!u.enabled;
    // 全市场过滤条件与雷达面板共用同一份输入，省得两处各调一遍
    const f = u.filters || {};
    if (f.min_turnover_usdt != null) $('uni-turnover').value = f.min_turnover_usdt;
    if (f.max_symbols != null) $('uni-top').value = f.max_symbols;
    if (f.asset_class) $('uni-allassets').checked = f.asset_class === 'all';
    if (f.sort) $('uni-sort').value = f.sort;
  }

  async function loadSchedule() {
    try {
      const d = await api('/api/schedule');
      const sc = d.schedule || {};
      applyScheduleUI(sc);
      renderSchedStatus(sc);
      (sc.log || []).slice(-25).forEach((e) => {
        logLine(e.kind === 'error' ? 'err' : (e.kind === 'signal' ? 'ok' : ''), e.text || '');
      });
    } catch (e) { /* 忽略 */ }
  }

  function renderSchedStatus(sc) {
    const on = sc.enabled && sc.running;
    const next = sc.next_run ? '，下次 ' + fmtTime(sc.next_run) : '';
    setPill('pill-sched', on ? '任务运行中 ' + sc.interval_sec + 's' + next : '任务已停止',
      on ? 'ok' : 'warn');
    const u = sc.universe || {};
    const scope = u.enabled
      ? '全市场 ' + (u.total || '?') + ' 个 / 每轮 ' + u.batch_size +
        ' 个（游标 ' + (u.cursor || 0) + '）'
      : '指定合约 ' + ((sc.symbols || []).join(', ') || '--');
    $('sched-hint').textContent = '已跑 ' + (sc.run_count || 0) + ' 轮 · ' + scope +
      (sc.last_error ? ' · 上次错误：' + sc.last_error : '');
  }

  async function saveSchedule() {
    const fullMarket = $('sc-uni').checked;
    const syms = $('sc-symbols').value.split(/[,，\s]+/).filter(Boolean)
      .map((s) => s.toUpperCase());
    if (!fullMarket && !syms.length) return toast('保存失败', '至少要填一个合约', 'err');
    $('btn-save-sched').disabled = true;
    try {
      const body = {
        enabled: $('sc-enabled').checked,
        interval_sec: Number($('sc-interval').value) || 300,
        symbols: syms.length ? syms : ['BTC-USDT'],
        mode: $('mode').value,
        only_on_signal: $('sc-onlysig').checked,
        universe: {
          enabled: fullMarket,
          batch_size: Number($('sc-uni-batch').value) || 20,
          workers: Number($('uni-workers').value) || 4,
          filters: {
            min_turnover_usdt: Number($('uni-turnover').value) || 0,
            max_symbols: Number($('uni-top').value) || 40,
            asset_class: $('uni-allassets').checked ? 'all' : 'crypto',
            sort: $('uni-sort').value || 'turnover',
          },
        },
      };
      const d = await api('/api/schedule', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      renderSchedStatus(d.schedule);
      const scope = fullMarket ? '全市场轮询' : syms.join(', ');
      toast('已保存', ($('sc-enabled').checked ? '循环执行已开启' : '循环执行已关闭') +
        '，每 ' + d.schedule.interval_sec + ' 秒一轮 · ' + scope, 'ok');
      logLine('ok', '任务配置已更新：' + scope + ' / ' + d.schedule.interval_sec + 's');
    } catch (err) {
      toast('保存失败', err.message, 'err');
    } finally {
      $('btn-save-sched').disabled = false;
    }
  }

  async function runNow() {
    $('btn-run').disabled = true;
    logLine('run', '手动执行一轮…');
    try {
      const d = await api('/api/schedule/run', { method: 'POST' });
      (d.plans || []).forEach((p) =>
        logLine('ok', p.symbol + ' ' + dirLabel(p.direction) + ' ' + signed(p.score)));
      toast('已执行', '共分析 ' + d.count + ' 个合约', 'ok');
      loadHistory();
    } catch (err) {
      logLine('err', '执行失败：' + err.message);
      toast('执行失败', err.message, 'err');
    } finally {
      $('btn-run').disabled = false;
    }
  }

  async function testNotify() {
    const btn = $('btn-test-notify');
    btn.disabled = true;
    try {
      const d = await api('/api/notify/test', { method: 'POST' });
      const st = d.status || {};
      if (!st.enabled) toast('未开启', 'notify.enabled 还是 false', 'warn');
      else if (!st.configured) toast('未配置', '请在 config.json 填 webhook / telegram / bark', 'warn');
      else if (d.sent > 0) toast('已发送', '已推送 ' + d.sent + ' 个渠道', 'ok');
      else toast('发送失败', st.last_error || '未知错误', 'err', 6000);
    } catch (err) {
      toast('测试失败', err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  async function loadHistory() {
    // 同样需要序号：SSE 的 plan/outcome 事件和"清空"都会并发触发，
    // 旧响应回来了就把新列表盖回去。
    const seq = ++state.histSeq;
    try {
      const d = await api('/api/history?limit=60');
      if (seq !== state.histSeq) return;
      const rows = d.rows || [];
      $('hist-hint').textContent = rows.length + ' 条';
      if (!rows.length) { $('history').innerHTML = '<div class="empty">还没有落库的信号。</div>'; return; }
      $('history').innerHTML = rows.map((p) => {
        let oc = '';
        if (p.outcome) {
          const o = outcomeLabel(p.outcome);
          oc = '<span class="oc ' + (p.outcome === 'stopped' ? 'bad' : (p.outcome === 'timeout' || p.outcome === 'no_fill' ? 'warn' : 'good')) +
            '">' + esc(o.label) + '</span>';
        }
        return '<div class="h-item" data-sym="' + esc(p.symbol) + '">' +
        '<span class="sym">' + esc(p.symbol) + '</span>' +
        '<span class="dir ' + p.direction + '">' + dirLabel(p.direction) + '</span>' +
        '<span class="sc ' + cls(p.score) + '">' + signed(p.score) + '</span>' +
        oc +
        '<span class="tm">' + esc(modeLabel(p.mode)) + ' · ' + fmtTime(p.generated_at, true) + '</span>' +
        '</div>';
      }).join('');
      $('history').onclick = (e) => {
        const it = e.target.closest('.h-item');
        if (!it) return;
        $('symbol').value = it.dataset.sym;
        analyze(false);
      };
    } catch (e) { /* 忽略 */ }
  }

  async function clearHistory() {
    if (!confirm('确定清空历史信号记录？')) return;
    try {
      await api('/api/history/clear', { method: 'POST' });
      loadHistory();
      loadWinStats();
      toast('已清空', '', 'ok');
    } catch (err) { toast('清空失败', err.message, 'err'); }
  }

  // ---------------------------------------------------------------- 胜率统计
  async function loadWinStats() {
    const days = $('win-days').value;
    const q = new URLSearchParams();
    if (days) q.set('days', days);
    // 序号守卫：切换天数 + SSE 的 outcome 事件都会并发触发本函数，
    // 先发后回的旧响应会覆盖新选择（选了"全部"却显示"近 30 天"）。
    const seq = ++state.winSeq;
    try {
      const d = await api('/api/win-stats?' + q.toString());
      if (seq !== state.winSeq) return;
      renderWinStats(d.win || {}, d.outcomes || {});
    } catch (e) {
      if (seq !== state.winSeq) return;
      $('win').innerHTML = '<div class="empty">胜率统计加载失败：' + esc(e.message) + '</div>';
    }
  }

  function _pct(v) { return v == null ? '--' : (v * 100).toFixed(1) + '%'; }

  function _winBars(g) {
    const n = g.n || 0;
    if (!n) return '<div class="win-empty">无</div>';
    const w = g.wins || 0;
    return '<div class="win-bar" title="胜 ' + w + ' / 负 ' + (g.losses || 0) + '">' +
      '<i class="w" style="width:' + Math.round((w / n) * 100) + '%"></i>' +
      '<i class="l" style="width:' + Math.round(((g.losses || 0) / n) * 100) + '%"></i>' +
      '</div>';
  }

  function renderWinStats(win, outcomes) {
    const resolved = win.resolved || 0;
    const sum = $('win');
    if (!resolved) {
      const done = Object.keys(outcomes || {}).reduce((a, k) => a + outcomes[k], 0);
      sum.innerHTML = '<div class="empty">还没有已判定的信号结果' +
        (done ? '（已回填 ' + done + ' 条）' : '') +
        '。开启定时任务并等待行情推进后，这里会显示胜率。</div>';
      return;
    }
    const wr = win.win_rate != null ? (win.win_rate * 100).toFixed(1) + '%' : '--';
    const wrCls = win.win_rate == null ? '' : (win.win_rate >= 0.5 ? 'good' : 'bad');

    let html = '<div class="win-kpis">' +
      '<div class="kpi"><div class="v ' + wrCls + '">' + wr + '</div><div class="k">胜率</div></div>' +
      '<div class="kpi"><div class="v">' + (win.wins || 0) + '</div><div class="k">到达目标</div></div>' +
      '<div class="kpi"><div class="v bad">' + (win.losses || 0) + '</div><div class="k">触发止损</div></div>' +
      '<div class="kpi"><div class="v">' + (win.resolved || 0) + '</div><div class="k">已判定</div></div>' +
      '<div class="kpi"><div class="v">' + fmtNum(win.avg_r, 2) + '</div><div class="k">平均 R</div></div>' +
      '<div class="kpi"><div class="v">' + fmtNum(win.avg_mfe_pct, 2) + '%</div><div class="k">平均最大浮盈</div></div>' +
      '<div class="kpi"><div class="v">' + fmtNum(win.avg_mae_pct, 2) + '%</div><div class="k">平均最大浮亏</div></div>' +
      '</div>';

    function group(title, obj) {
      const keys = Object.keys(obj || {});
      if (!keys.length) return '';
      return '<div class="win-group"><div class="sub-title">' + title + '</div><table class="win-table">' +
        keys.map((k) => {
          const g = obj[k];
          return '<tr><td class="k">' + esc(k) + '</td><td>' + _winBars(g) + '</td>' +
            '<td class="n">' + (g.n || 0) + '</td>' +
            '<td class="n ' + (g.win_rate == null ? '' : (g.win_rate >= 0.5 ? 'good' : 'bad')) + '">' +
            _pct(g.win_rate) + '</td>' +
            '<td class="n">' + fmtNum(g.avg_r, 2) + '</td></tr>';
        }).join('') + '</table></div>';
    }

    html += '<div class="win-groups">' +
      group('按 |评分| 分档', win.by_score) +
      group('按模式', win.by_mode) +
      group('按方案', win.by_profile) +
      group('按置信度', win.by_confidence) +
      group('按币种', win.by_symbol) +
      '</div>';
    sum.innerHTML = html;
  }

  // ---------------------------------------------------------------- 因子权重调参
  async function loadWeights() {
    try {
      const d = await api('/api/weights');
      state.weights = d.weights || {};
      const names = {
        trend_daily: '日线趋势', trend_intraday: '1小时趋势', momentum: '动量',
        structure: '摆动结构', vwap: 'VWAP', funding: '资金费率',
        open_interest: '持仓量', elite: '大户持仓', orderbook: '盘口深度',
        volatility: '波动率',
      };
      const def = d.defaults || {};
      if (def['score_threshold'] !== undefined) $('score-threshold').value = def['score_threshold'];
      state.threshold = def['score_threshold'] ?? 30;
      // 阈值变了仪表盘刻度也要跟着变；重画一次建议卡（若已有计划）
      if (state.plan) renderAdvice(state.plan);
      $('res-enabled').checked = !!def['resonance_enabled'];
      $('weights').innerHTML = Object.keys(d.weights).map((k) => {
        const cur = d.weights[k];
        const isCustom = Math.abs(cur - def[k]) > 1e-9;
        return '<label class="w-row" title="' + esc(k) + '">' +
          '<span class="k">' + esc(names[k] || k) + '</span>' +
          '<input type="range" min="0" max="0.5" step="0.01" value="' + cur +
          '" data-name="' + esc(k) + '">' +
          '<span class="v">' + cur.toFixed(2) + '</span>' +
          (isCustom ? '<span class="tag">自定义</span>' : '') +
          '</label>';
      }).join('');
      $('weights').oninput = (e) => {
        const input = e.target.closest('input[type=range]');
        if (!input) return;
        const v = parseFloat(input.value);
        input.closest('.w-row').querySelector('.v').textContent = v.toFixed(2);
        state.weights[input.dataset.name] = v;
      };
      setWeightsMsg(d.customized ? '已加载自定义权重（与默认不同）' : '使用默认权重', d.customized ? '' : 'ok');
    } catch (e) {
      $('weights').innerHTML = '<div class="empty">权重加载失败：' + esc(e.message) + '</div>';
    }
  }

  function setWeightsMsg(text, kind) {
    const el = $('weights-msg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'msg' + (kind ? ' ' + kind : '');
  }

  async function saveWeights(reset) {
    $('btn-weights-save').disabled = true;
    $('btn-weights-reset').disabled = true;   // 同一个函数，防连点重复提交
    try {
      const data = reset ? {} : (state.weights || {});
      const thr = reset ? 30 : parseInt($('score-threshold').value || '30', 10);
      const res_on = reset ? false : $('res-enabled').checked;
      await api('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ analysis: {
          factor_weights: data, score_threshold: thr,
          resonance: { enabled: res_on, weight: 0.06 },
        } }),
      });
      state.threshold = thr;
      setWeightsMsg(reset ? '已恢复默认权重与阈值' : '权重与阈值已保存', 'ok');
      await loadWeights();
      await loadProfiles();
      toast('权重' + (reset ? '已重置' : '已保存'), '', 'ok');
    } catch (err) {
      setWeightsMsg('保存失败：' + err.message, 'err');
      toast('保存失败', err.message, 'err');
    } finally {
      $('btn-weights-save').disabled = false;
      $('btn-weights-reset').disabled = false;
    }
  }

  // ---------------------------------------------------------------- 导出报告
  async function openReport() {
    const btn = $('btn-report');
    btn.disabled = true;
    try {
      const q = new URLSearchParams({ symbol: $('symbol').value, mode: $('mode').value });
      const token = localStorage.getItem('iabot_token');
      const hdrs = {};
      if (token) hdrs['Authorization'] = 'Bearer ' + token;
      const res = await fetch('/api/report?' + q.toString(), { headers: hdrs });
      if (!res.ok) {
        // 未授权时走一次 api() 的弹窗 token 流程后重试
        if (res.status === 401) {
          const again = await askToken();
          if (again) return openReport();
        }
        throw new Error('HTTP ' + res.status);
      }
      const html = await res.text();
      const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank');
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (err) {
      toast('导出报告失败', err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 模拟盘持仓
  function fillMarkPrice() {
    // 只有当表单里的合约就是当前分析的合约时才能填——否则分析的是 BTC、
    // 表单填的是 ETH，填进去的就是 BTC 的价。
    const p = state.plan || state.snapshot;
    const sym = (state.symbol || '').toUpperCase();
    const want = ($('pos-sym').value || '').trim().toUpperCase();
    if (sym && want && sym !== want) return false;
    const price = (p && p.price != null ? p.price : (state.snapshot && state.snapshot.price));
    if (!price) return false;
    $('pos-entry').value = price;
    return true;
  }

  async function openPosition() {
    const body = {
      action: 'open',
      symbol: $('pos-sym').value,
      direction: $('pos-dir').value,
      entry_price: parseFloat($('pos-entry').value),
      size_coin: parseFloat($('pos-size').value),
      leverage: parseFloat($('pos-lev').value || '1'),
      stop: $('pos-stop').value ? parseFloat($('pos-stop').value) : null,
      tp1: $('pos-tp1').value ? parseFloat($('pos-tp1').value) : null,
    };
    if (!(body.entry_price > 0) || !(body.size_coin > 0)) {
      toast('请填写开仓价和数量', '', 'warn'); return;
    }
    // 防重复提交：其它写操作（保存方案/保存任务/导出）都会禁用按钮，
    // 只有开仓没有，双击会开出两笔重复持仓并虚增浮盈。
    const btn = $('btn-pos-open');
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      await api('/api/positions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      toast('已开仓', body.symbol + ' ' + body.direction, 'ok');
      loadPositions();
    } catch (err) { toast('开仓失败', err.message, 'err'); }
    finally { btn.disabled = false; }
  }

  async function loadPositions() {
    try {
      const d = await api('/api/positions');
      renderPositions(d.positions || []);
    } catch (e) {
      $('positions').innerHTML = '<div class="empty">持仓加载失败：' + esc(e.message) + '</div>';
    }
    loadPnl();
  }

  async function loadPnl() {
    try {
      const d = await api('/api/pnl');
      renderPnl(d.pnl || {});
    } catch (e) { /* 利润簿加载失败不打断 */ }
  }

  function renderPnl(p) {
    const el = $('pnl-book');
    if (!p || !p.closed_count) {
      el.innerHTML = '<div class="sub-title">利润簿（已平仓）</div>' +
        '<div class="empty">还没有平仓记录。平掉一笔模拟仓后这里显示利润簿。</div>';
      return;
    }
    const cls = (v) => (v >= 0 ? 'up' : 'down');
    const pct = (v) => (v == null ? '--' : (v >= 0 ? '+' : '') + fmtNum(v, 1) + '%');
    const kpi = [
      ['总净盈亏', p.total_net_usdt, ' U', cls(p.total_net_usdt)],
      ['本月', p.month_net_usdt, ' U', cls(p.month_net_usdt)],
      ['本周', p.week_net_usdt, ' U', cls(p.week_net_usdt)],
      ['胜率', p.win_rate * 100, '%', cls(p.win_rate - 0.5)],
      ['已平仓', p.closed_count, ' 笔', ''],
      ['均盈 / 均亏', p.avg_win_usdt + ' / ' + p.avg_loss_usdt, ' U', ''],
    ];
    const kpis = kpi.map(([k, v, u, c]) =>
      '<div class="kpi"><div class="v ' + c + '">' + (typeof v === 'number' ? fmtNum(v, 1) : v) + u + '</div>' +
      '<div class="k">' + k + '</div></div>').join('');
    const curve = (p.equity_curve || []).map((pt) => pt.cum);
    let svg = '';
    if (curve.length > 1) {
      const W = 900, H = 120;
      const mn = Math.min(0, ...curve), mx = Math.max(...curve);
      const rng = (mx - mn) || 1;
      const pts = curve.map((c, i) => {
        const x = (i / (curve.length - 1)) * W;
        const y = H - ((c - mn) / rng) * (H - 8) - 4;
        return x.toFixed(1) + ',' + y.toFixed(1);
      }).join(' ');
      const last = curve[curve.length - 1];
      svg = '<svg viewBox="0 0 900 120" class="pnl-curve" xmlns="http://www.w3.org/2000/svg">' +
        '<line x1="0" y1="' + (H - ((0 - mn) / rng) * (H - 8) - 4) + '" x2="900" y2="' +
        (H - ((0 - mn) / rng) * (H - 8) - 4) + '" stroke="#243055" stroke-dasharray="4 4"/>' +
        '<polyline points="' + pts + '" fill="none" stroke="' +
        (last >= 0 ? '#22c55e' : '#ef4444') + '" stroke-width="2"/>' +
        '<text x="4" y="14" fill="#8f9cbb" font-size="11">累计净盈亏 ' +
        (last >= 0 ? '+' : '') + fmtNum(last, 1) + ' U</text></svg>';
    }
    el.innerHTML = '<div class="sub-title">利润簿（已平仓，扣往返手续费）</div>' +
      '<div class="win-kpis">' + kpis + '</div>' + svg;
  }

  function renderPositions(rows) {
    const el = $('positions');
    if (!rows.length) { el.innerHTML = '<div class="empty">还没有模拟持仓。</div>'; return; }
    el.innerHTML = rows.map((p) => {
      const pnl = p.pnl || {};
      const up = (pnl.unrealized_pnl_usdt || 0) >= 0;
      const cls = p.status === 'open' ? (up ? 'up' : 'down') : 'closed';
      const mark = p.mark_price != null ? fmtPrice(p.mark_price, digitsFor(p.entry_price)) : '--';
      return '<div class="pos ' + cls + '">' +
        '<div class="pos-head"><span class="sym">' + esc(p.symbol) + '</span>' +
        '<span class="dir ' + p.direction + '">' + dirLabel(p.direction) + '</span>' +
        (p.status === 'open' ? '' : '<span class="tag closed">' + esc(p.close_reason || '已平') + '</span>') +
        (p.leverage > 1 ? '<span class="lev">' + p.leverage + 'x</span>' : '') +
        '<span class="pnl ' + (up ? 'up' : 'down') + '">' +
        (p.status === 'open'
          ? (up ? '+' : '') + fmtNum(pnl.unrealized_pnl_usdt, 2) + ' U (' + fmtNum(pnl.pnl_of_margin_pct, 1) + '%)'
          : (p.close_price ? '@ ' + fmtPrice(p.close_price, digitsFor(p.entry_price)) : '')) +
        '</span></div>' +
        '<div class="pos-meta">' +
        '<span>开仓 <b>' + fmtPrice(p.entry_price, digitsFor(p.entry_price)) + '</b></span>' +
        '<span>现价 <b>' + mark + '</b></span>' +
        '<span>数量 <b>' + fmtNum(p.size_coin, 4) + '</b></span>' +
        '<span>保证金 <b>' + fmtNum(pnl.margin_usdt, 2) + ' U</b></span>' +
        (p.stop ? '<span>止损 <b>' + fmtPrice(p.stop, digitsFor(p.entry_price)) + '</b></span>' : '') +
        (p.tp1 ? '<span>止盈 <b>' + fmtPrice(p.tp1, digitsFor(p.entry_price)) + '</b></span>' : '') +
        (pnl.liquidation_price != null
          ? '<span class="' + (pnl.liquidation_risk ? 'bad' : '') + '">强平 <b>' +
            fmtPrice(pnl.liquidation_price, digitsFor(p.entry_price)) + '</b>' +
            (pnl.liquidation_risk ? ' ⚠止损无效' : '') + '</span>'
          : '') +
        '</div>' +
        (p.status === 'open'
          ? '<button class="ghost tiny pos-close" data-id="' + p.id +
            '" data-price="' + (p.mark_price != null ? p.mark_price : '') + '">平仓</button>'
          : '') +
        '</div>';
    }).join('');
    el.querySelectorAll('.pos-close').forEach((b) => {
      b.onclick = () => closePosition(b.dataset.id, b.dataset.price);
    });
  }

  async function closePosition(id, markPrice) {
    // 必须用「这一行自己的」现价。state.plan.price 是顶部分析框当前合约的价，
    // 跟被点的持仓毫无关系——看的是 BTC、点 ETH 的平仓，就会拿 BTC 的价去结算。
    const px = Number(markPrice);
    if (!isFinite(px) || px <= 0) return toast('平仓失败', '拿不到该持仓的现价，请刷新后重试', 'err');
    if (!confirm('按现价 ' + fmtPrice(px, digitsFor(px)) + ' 平仓该模拟持仓？')) return;
    try {
      await api('/api/positions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'close', id, close_price: px, reason: 'manual' }),
      });
      toast('已平仓', '', 'ok');
      loadPositions();
    } catch (err) { toast('平仓失败', err.message, 'err'); }
  }

  // ---------------------------------------------------------------- 参数方案
  async function loadProfiles() {
    try {
      const d = await api('/api/profiles');
      renderProfiles(d.profiles || [], d.active || '', d.current || {});
    } catch (e) {
      $('profiles').innerHTML = '<div class="empty">方案加载失败：' + esc(e.message) + '</div>';
    }
    loadBacktests();
  }

  // ---------------------------------------------------------------- 回测记录
  async function loadBacktests() {
    try {
      const d = await api('/api/backtests?limit=20');
      renderBacktests(d.rows || []);
    } catch (e) {
      $('backtests').innerHTML = '<div class="empty">记录加载失败：' + esc(e.message) + '</div>';
    }
  }

  function renderBacktests(rows) {
    const el = $('backtests');
    if (!rows.length) {
      el.innerHTML = '<div class="empty">还没有回测记录。回测时勾选"存档"就会记下来。</div>';
      return;
    }
    el.innerHTML = rows.map((r) => {
      const res = r.result || {};
      const exp = res.expectancy_r || 0;
      const wr = res.win_rate || 0;
      return '<div class="bt-row">' +
        '<div class="bt-l">' +
        '<span class="sym">' + esc(r.symbol) + '</span>' +
        '<span class="mode">' + esc(r.mode) + '</span>' +
        (r.profile ? '<span class="tag on">' + esc(r.profile) + '</span>' : '') +
        '<span class="tm">' + fmtTime(r.created_at, true) + '</span></div>' +
        '<div class="bt-r">' +
        '<span>胜率 <b class="' + (wr >= 0.5 ? 'good' : 'bad') + '">' +
        (wr * 100).toFixed(1) + '%</b></span>' +
        '<span>期望 <b class="' + (exp >= 0 ? 'good' : 'bad') + '">' +
        (exp >= 0 ? '+' : '') + exp.toFixed(3) + 'R</b></span>' +
        '<span>' + (res.trades || 0) + ' 笔</span>' +
        (res.final_equity_usdt != null
          ? '<span>期末 <b>' + fmtNum(res.final_equity_usdt, 0) + 'U</b></span>' : '') +
        '</div></div>';
    }).join('');
  }

  function renderProfiles(list, active, current) {
    const el = $('profiles');
    if (!list.length) {
      el.innerHTML = '<div class="empty">还没有方案。调好权重后点"保存当前为方案"。</div>';
      return;
    }
    el.innerHTML = list.map((p) => {
      return '<div class="prof' + (p.active ? ' active' : '') + '">' +
        '<div class="prof-head"><span class="nm">' + esc(p.name) + '</span>' +
        (p.active ? '<span class="tag on">生效中</span>' : '') +
        '<span class="meta">' + esc(p.mode) + ' · 阈值 ' + p.score_threshold +
        (p.custom ? ' · ' + p.custom + ' 项权重自定义' : '') + '</span></div>' +
        (p.note ? '<div class="note">' + esc(p.note) + '</div>' : '') +
        '<div class="prof-acts">' +
        '<button class="ghost tiny" data-act="apply" data-name="' + esc(p.name) + '">应用</button>' +
        '<button class="ghost tiny" data-act="bt" data-name="' + esc(p.name) + '">回测</button>' +
        '<button class="ghost tiny" data-act="del" data-name="' + esc(p.name) + '">删除</button>' +
        '</div></div>';
    }).join('');
    el.onclick = (e) => {
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      const act = b.dataset.act, name = b.dataset.name;
      if (act === 'apply') applyProfile(name);
      else if (act === 'del') deleteProfile(name);
      else if (act === 'bt') backtestProfile(name);
    };
  }

  async function saveProfile() {
    const name = ($('prof-name').value || '').trim();
    if (!name) { toast('请填方案名', '', 'warn'); return; }
    try {
      await api('/api/profiles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: 'save', name,
          factor_weights: state.weights || {},
          score_threshold: parseInt($('score-threshold').value || '30', 10),
          mode: $('mode').value,
        }),
      });
      $('prof-name').value = '';
      await loadProfiles();
      toast('方案已保存', name, 'ok');
    } catch (err) { toast('保存失败', err.message, 'err'); }
  }

  async function applyProfile(name) {
    try {
      await api('/api/profiles', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'apply', name }),
      });
      await loadProfiles();
      await loadWeights();
      toast('已应用方案', name, 'ok');
    } catch (err) { toast('应用失败', err.message, 'err'); }
  }

  async function deleteProfile(name) {
    if (!confirm('删除方案 ' + name + '？')) return;
    try {
      await api('/api/profiles', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'delete', name }),
      });
      await loadProfiles();
      toast('已删除', name, 'ok');
    } catch (err) { toast('删除失败', err.message, 'err'); }
  }

  async function backtestProfile(name) {
    try {
      const d = await api('/api/profiles/backtest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, symbol: $('symbol').value, bars: 2000, step: 10,
                               save: $('prof-archive').checked }),
      });
      const r = d.result || {};
      const wr = (r.win_rate * 100).toFixed(1);
      toast('回测 ' + name, '胜率 ' + wr + '% · 期望 ' + (r.expectancy_r >= 0 ? '+' : '') +
        r.expectancy_r + ' R/笔', r.expectancy_r >= 0 ? 'ok' : 'warn', 6000);
      showEquityCurve(r, name, $('symbol').value);
      if ($('prof-archive').checked) loadBacktests();
    } catch (err) { toast('回测失败', err.message, 'err'); }
  }

  // 权益曲线：回测结果里的 equity_curve 是"每笔之后累计的 1R 风险累计值"，
  // 按 risk_amount_usdt 折算成金额，就能看出这条策略的资金曲线长什么样。
  // 之前只给一个期望值，回撤多大、最长连亏多长都看不见。
  function showEquityCurve(res, label, symbol) {
    const el = $('bt-curve');
    if (!el) return;
    const curve = (res && res.equity_curve) || [];
    const perR = (res && res.risk_amount_usdt) || 0;
    if (curve.length < 2 || perR <= 0) {
      el.innerHTML = '<div class="empty">交易笔数太少，画不出资金曲线。</div>';
      return;
    }
    const money = curve.map((v) => v * perR);
    const W = 900, H = 120;
    const mn = Math.min(0, ...money), mx = Math.max(...money);
    const rng = (mx - mn) || 1;
    const pts = money.map((c, i) =>
      ((i / (money.length - 1)) * W).toFixed(1) + ',' +
      (H - ((c - mn) / rng) * (H - 8) - 4).toFixed(1)).join(' ');
    const last = money[money.length - 1];
    let peak = -Infinity, dd = 0;
    money.forEach((c) => { peak = Math.max(peak, c); dd = Math.max(dd, peak - c); });
    const zeroY = (H - ((0 - mn) / rng) * (H - 8) - 4).toFixed(1);
    el.innerHTML = '<div class="sub-title">资金曲线 · ' + esc(label || '') + ' · ' +
      esc(symbol || '') + '（' + money.length + ' 笔，按 1R = ' +
      fmtNum(perR, 1) + ' U 折算）</div>' +
      '<svg viewBox="0 0 900 ' + H + '" class="pnl-curve" xmlns="http://www.w3.org/2000/svg">' +
      '<line x1="0" y1="' + zeroY + '" x2="900" y2="' + zeroY +
      '" stroke="#243055" stroke-dasharray="4 4"/>' +
      '<polyline points="' + pts + '" fill="none" stroke="' +
      (last >= 0 ? '#22c55e' : '#ef4444') + '" stroke-width="2"/>' +
      '<text x="4" y="14" fill="#8f9cbb" font-size="11">累计 ' +
      fmtNum(last, 0) + ' U · 最大回撤 ' + fmtNum(dd, 0) + ' U</text></svg>';
  }

  async function compareProfiles() {
    const el = $('prof-cmp');
    const btn = $('btn-prof-cmp');
    btn.disabled = true;
    el.innerHTML = '<div class="loading">正在对每个方案跑同一段历史回测（每个方案各跑一遍，方案越多越慢）…</div>';
    try {
      const d = await api('/api/profiles/compare', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: $('symbol').value, bars: 2000, step: 10,
                               save: $('prof-archive').checked }),
      });
      const rows = d.rows || [];
      if (!rows.length) { el.innerHTML = '<div class="empty">没有可对比的方案。</div>'; return; }
      const best = rows[0];
      el.innerHTML = '<div class="sub-title">回测对比（' + rows.length + ' 个，按期望 R 排序，当前合约 ' +
        esc($('symbol').value) + '）</div>' +
        '<table class="cmp-table"><tr>' +
        '<th>方案</th><th>胜率</th><th>期望 R</th><th>笔数</th><th>平均持仓</th><th>成本 R</th><th></th></tr>' +
        rows.map((r, i) => {
          const res = r.result || {};
          const exp = res.expectancy_r || 0;
          const active = r.active ? '<span class="tag on">生效中</span>' : '';
          const top = i === 0 ? '<span class="tag on">最优</span>' : '';
          return '<tr class="' + (i === 0 ? 'best' : '') + '">' +
            '<td class="k">' + esc(r.name) + active + ' ' + top + '</td>' +
            '<td>' + fmtNum((res.win_rate || 0) * 100, 1) + '%</td>' +
            '<td class="' + (exp >= 0 ? 'good' : 'bad') + '">' + (exp >= 0 ? '+' : '') +
            fmtNum(exp, 3) + '</td>' +
            '<td>' + (res.trades || 0) + '</td>' +
            '<td>' + fmtNum(res.avg_holding_h, 1) + 'h</td>' +
            '<td>' + fmtNum(res.avg_cost_r, 3) + '</td>' +
            '<td><button class="ghost tiny" data-act="apply" data-name="' + esc(r.name) +
            '">应用</button></td></tr>';
        }).join('') + '</table>';
      el.querySelectorAll('button[data-act=apply]').forEach((b) => {
        b.onclick = () => {
          if (b.dataset.name === '当前参数') { toast('当前参数已在生效', '', 'warn'); return; }
          applyProfile(b.dataset.name);
        };
      });
    } catch (err) {
      el.innerHTML = '<div class="empty">对比失败：' + esc(err.message) + '</div>';
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 日志 / SSE
  function logLine(kind, text) {
    const el = $('sched-log');
    if (!el || !text) return;
    const div = document.createElement('div');
    div.className = 'log-line' + (kind ? ' ' + kind : '');
    div.innerHTML = '<span class="t">' + fmtTime(Date.now() / 1000) + '</span>' + esc(text);
    el.appendChild(div);
    while (el.children.length > 200) el.removeChild(el.firstChild);
    el.scrollTop = el.scrollHeight;
  }

  function connectSSE() {
    if (!window.EventSource) { setPill('pill-sse', '实时流 不支持', 'bad'); return; }
    // 先关掉旧的再建：init 会先 bindEvents 再连一次，不关就会留下两个
    // EventSource，服务端多挂一个订阅者。
    if (state.es) { try { state.es.close(); } catch (e) { /* 已断开 */ } state.es = null; }
    const tok = localStorage.getItem('iabot_token');
    const es = new EventSource('/api/stream' + (tok ? ('?token=' + encodeURIComponent(tok)) : ''));
    state.es = es;
    es.onopen = () => setPill('pill-sse', '实时流 已连接', 'ok');
    es.onerror = () => setPill('pill-sse', '实时流 重连中…', 'warn');
    es.onmessage = (ev) => {
      let d = null;
      try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d.type === 'hello') {
        setPill('pill-sse', '实时流 v' + (d.version || ''), 'ok');
      } else if (d.type === 'run_start') {
        logLine('run', '开始一轮：' + (d.symbols || []).join(', '));
        setPill('pill-sched', '任务运行中…', 'ok');
      } else if (d.type === 'plan') {
        const p = d.plan || {};
        logLine(p.direction === 'wait' ? '' : 'ok',
          p.symbol + ' ' + dirLabel(p.direction) + ' ' + signed(p.score) +
          (p.rr != null ? ' RR ' + fmtNum(p.rr, 2) : ''));
        loadHistory();
      } else if (d.type === 'run_done') {
        logLine('ok', '一轮结束：' + d.count + ' 个合约，耗时 ' + d.elapsed + 's' +
          (d.verified ? '，回填 ' + d.verified + ' 条' : ''));
        loadSchedule();
      } else if (d.type === 'outcome') {
        const oc = outcomeLabel(d.outcome);
        logLine(oc.good ? 'ok' : 'err', d.symbol + ' ' + oc.label +
          (d.mfe_pct != null ? ' 最大浮盈 ' + fmtNum(d.mfe_pct, 2) + '%' : ''));
        loadWinStats();
        loadHistory();
      } else if (d.type === 'skip') {
        logLine('warn', '跳过：' + (d.reason || ''));
      } else if (d.type === 'error') {
        logLine('err', (d.symbol ? d.symbol + ' ' : '') + (d.error || '未知错误'));
      }
    };
  }

  window.addEventListener('resize', () => { if (state.snapshot) renderChart(); });
  document.addEventListener('visibilitychange', () => {
    // 页面切走时断开 SSE，避免后台标签页长期占着一条长连接 + 服务端订阅队列
    if (document.hidden) {
      if (state.es) { try { state.es.close(); } catch (e) { /* 忽略 */ } state.es = null; }
    } else {
      connectSSE();
    }
  });

  document.addEventListener('DOMContentLoaded', init);
})();