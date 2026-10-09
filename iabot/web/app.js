/* 火币合约行情分析 · 前端逻辑（原生 JS，零依赖） */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    meta: null,
    watchlist: [],
    symbol: 'BTC-USDT',
    mode: 'intraday',
    plan: null,
    snapshot: null,
    klines: [],
    tf: '15min',
    busy: false,
    es: null,
    log: [],
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
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok || !data || data.ok === false) {
      throw new Error((data && data.error) || ('HTTP ' + res.status));
    }
    return data;
  }

  function loading(el, text) {
    el.innerHTML = '<div class="loading">' + esc(text || '加载中…') + '</div>';
  }

  // ---------------------------------------------------------------- 初始化
  async function init() {
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

      applyScheduleUI((def.schedule) || {});
      loadHealth();
      loadHistory();
      loadSchedule();
      analyze(false);
    } catch (err) {
      setMsg('初始化失败：' + err.message, 'err');
      toast('初始化失败', err.message, 'err');
    }
    bindEvents();
    connectSSE();
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
    $('equity').onchange = $('leverage').onchange = $('risk').onchange = () => analyze(true);
    $('btn-run').onclick = runNow;
    $('btn-save-sched').onclick = saveSchedule;
    $('btn-clear-hist').onclick = clearHistory;
    $('btn-scan').onclick = scan;
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
    if (state.busy) return;
    state.busy = true;
    const sym = ($('symbol').value || '').trim().toUpperCase();
    if (!/[A-Z0-9]/.test(sym)) { state.busy = false; return setMsg('请输入合约代码，例如 BTC-USDT', 'err'); }
    $('symbol').value = sym;
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
      state.plan = d.plan;
      state.snapshot = d.snapshot;
      state.klines = d.snapshot.klines || {};
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
    } catch (err) {
      setMsg('分析失败：' + err.message, 'err');
      $('advice').innerHTML = '<div class="empty">分析失败。</div>';
      $('factors').innerHTML = '<div class="empty">分析失败。</div>';
      toast('分析失败', err.message, 'err');
    } finally {
      state.busy = false;
      $('btn-go').disabled = $('btn-refresh').disabled = false;
    }
  }

  function toTf() {
    const ks = (state.snapshot && state.snapshot.klines) || {};
    return ks[state.tf] ? state.tf : (Object.keys(ks)[0] || '15min');
  }

  function buildSeg(klines) {
    const keys = Object.keys(klines);
    const seg = $('tf-seg');
    seg.innerHTML = keys.map((k) =>
      '<button class="seg-btn" data-tf="' + esc(k) + '">' + esc(k) + '</button>').join('');
    const pref = state.mode === 'swing' ? ['1day', '60min', '4hour'] : ['15min', '5min', '60min'];
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
    const cssH = 360;
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

    const w = Math.abs(Math.max(-100, Math.min(100, plan.score))) / 100 * 50;
    h += '<div class="gauge"><i style="left:' + (plan.score >= 0 ? 50 : 50 - w) + '%;width:' + w + '%"></i>' +
      '<div class="mid"></div></div>' +
      '<div class="gauge-lbl"><span>-100 空</span><span>±30 阈值</span><span>+100 多</span></div>';

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
      h += '<div class="kvgrid">' +
        kv('下单张数', sizing.contracts + ' 张（' + fmtNum(sizing.position_coin, 6) + ' 币）', 'wide') +
        kv('名义价值', fmtNum(sizing.notional_usdt, 2) + ' U', '') +
        kv('占用保证金', fmtNum(sizing.margin_usdt, 2) + ' U（' +
          fmtNum(sizing.margin_pct_of_equity, 1) + '%）', '') +
        kv('止损亏损', fmtNum(sizing.loss_if_stopped_usdt, 2) + ' U（' +
          fmtNum(sizing.loss_pct_of_equity, 2) + '%）', '') +
        kv('杠杆 / 手续费', sizing.leverage + 'x / ' + fmtNum(sizing.fee_estimate_usdt, 3) + ' U', '') +
        '</div>';
      if (sizing.note) h += '<div class="sizing-note">' + esc(sizing.note) + '</div>';
    } else if (sizing.note) {
      h += '<div class="adv-note">仓位：' + esc(sizing.note) + '</div>';
    }

    if (plan.invalidation) h += '<div class="adv-note">失效条件：' + esc(plan.invalidation) + '</div>';
    (plan.warnings || []).forEach((t) => { h += '<div class="warnbox">⚠ ' + esc(t) + '</div>'; });
    (plan.notes || []).forEach((t) => { h += '<div class="adv-note">' + esc(t) + '</div>'; });
    if (!plan.warnings.length && plan.direction !== 'wait') {
      h += '<div class="warnbox ok">✔ 未发现风险提示（仍然只是分析，不构成投资建议）</div>';
    }
    el.innerHTML = h;
    $('card-advice').className = 'card dir-' + dir;
  }

  function kv(label, value, kind) {
    return '<div class="kv ' + (kind || '') + '"><span>' + esc(label) + '</span><b>' + value + '</b></div>';
  }

  // ---------------------------------------------------------------- 因子
  function renderFactors(plan) {
    const el = $('factors');
    const fs = (plan && plan.factors) || [];
    if (!fs.length) { el.innerHTML = '<div class="empty">暂无因子数据。</div>'; return; }
    $('factor-hint').textContent = '加权 ' + fs.length + ' 项，贡献 = 得分 × 权重';
    el.innerHTML = fs.map((f) => {
      const c = cls(f.score);
      const w = Math.abs(f.score) * 50;
      return '<div class="factor"><div class="f-top">' +
        '<span class="f-label">' + esc(f.label) + '</span>' +
        '<span class="hint">权重 ' + fmtNum(f.weight, 2) + '</span>' +
        '<span class="f-val ' + c + '">' + signed(f.score, 2) + '</span></div>' +
        '<div class="f-detail">' + esc(f.detail || '') + '（贡献 ' + signed(f.contribution, 3) + '）</div>' +
        '<div class="f-bar"><i class="' + (f.score >= 0 ? 'up' : 'dn') + '" style="' +
        (f.score >= 0 ? 'left:50%' : 'left:' + (50 - w) + '%') + ';width:' + w + '%"></i></div>' +
        '</div>';
    }).join('');
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
  async function scan() {
    const btn = $('btn-scan');
    btn.disabled = true;
    loading($('scan'), '扫描中，逐个合约拉取行情（约 3-10 秒/个）…');
    try {
      const q = new URLSearchParams({
        symbols: state.watchlist.join(','),
        mode: $('mode').value,
        equity: $('equity').value || '450',
        leverage: $('leverage').value || '10',
        risk_pct: $('risk').value || '2',
      });
      const d = await api('/api/scan?' + q.toString());
      const plans = d.plans || [];
      if (!plans.length) { $('scan').innerHTML = '<div class="empty">没有结果。</div>'; return; }
      $('scan').innerHTML = plans.map((p) => {
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
      if ((d.errors || []).length) toast('部分合约失败', d.errors.join('；'), 'err', 6000);
      logLine('ok', '扫描完成：' + plans.length + ' 个合约');
    } catch (err) {
      $('scan').innerHTML = '<div class="empty">扫描失败：' + esc(err.message) + '</div>';
      toast('扫描失败', err.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 定时任务
  function applyScheduleUI(sc) {
    $('sc-enabled').checked = !!sc.enabled;
    $('sc-interval').value = sc.interval_sec || 300;
    $('sc-symbols').value = (sc.symbols || ['BTC-USDT']).join(', ');
    $('sc-onlysig').checked = !!sc.only_on_signal;
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
    $('sched-hint').textContent = '已跑 ' + (sc.run_count || 0) + ' 轮 · 合约 ' +
      ((sc.symbols || []).join(', ') || '--') +
      (sc.last_error ? ' · 上次错误：' + sc.last_error : '');
  }

  async function saveSchedule() {
    const syms = $('sc-symbols').value.split(/[,，\s]+/).filter(Boolean)
      .map((s) => s.toUpperCase());
    if (!syms.length) return toast('保存失败', '至少要填一个合约', 'err');
    $('btn-save-sched').disabled = true;
    try {
      const d = await api('/api/schedule', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: $('sc-enabled').checked,
          interval_sec: Number($('sc-interval').value) || 300,
          symbols: syms,
          mode: $('mode').value,
          only_on_signal: $('sc-onlysig').checked,
        }),
      });
      renderSchedStatus(d.schedule);
      toast('已保存', ($('sc-enabled').checked ? '循环执行已开启' : '循环执行已关闭') +
        '，每 ' + d.schedule.interval_sec + ' 秒一轮', 'ok');
      logLine('ok', '任务配置已更新：' + syms.join(', ') + ' / ' + d.schedule.interval_sec + 's');
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

  async function loadHistory() {
    try {
      const d = await api('/api/history?limit=60');
      const rows = d.rows || [];
      $('hist-hint').textContent = rows.length + ' 条';
      if (!rows.length) { $('history').innerHTML = '<div class="empty">还没有落库的信号。</div>'; return; }
      $('history').innerHTML = rows.map((p) =>
        '<div class="h-item" data-sym="' + esc(p.symbol) + '">' +
        '<span class="sym">' + esc(p.symbol) + '</span>' +
        '<span class="dir ' + p.direction + '">' + dirLabel(p.direction) + '</span>' +
        '<span class="sc ' + cls(p.score) + '">' + signed(p.score) + '</span>' +
        '<span class="tm">' + esc(modeLabel(p.mode)) + ' · ' + fmtTime(p.generated_at, true) + '</span>' +
        '</div>').join('');
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
      toast('已清空', '', 'ok');
    } catch (err) { toast('清空失败', err.message, 'err'); }
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
    const es = new EventSource('/api/stream');
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
        logLine('ok', '一轮结束：' + d.count + ' 个合约，耗时 ' + d.elapsed + 's');
        loadSchedule();
      } else if (d.type === 'skip') {
        logLine('warn', '跳过：' + (d.reason || ''));
      } else if (d.type === 'error') {
        logLine('err', (d.symbol ? d.symbol + ' ' : '') + (d.error || '未知错误'));
      }
    };
  }

  window.addEventListener('resize', () => { if (state.snapshot) renderChart(); });
  document.addEventListener('DOMContentLoaded', init);
})();