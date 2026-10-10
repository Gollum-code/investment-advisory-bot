"""导出分析报告：服务端渲染 SVG K 线 + 自包含 HTML 报告页。

项目零第三方依赖，不能用 matplotlib/reportlab 生成 PNG/PDF。这里用纯 Python
生成 SVG 蜡烛图 + 内联样式，输出一个自包含 HTML 文件——浏览器打开后直接
"打印"就能存成 PDF，也便于存档/分享。

用法：
    GET /api/report?symbol=BTC-USDT&mode=intraday  -> 返回整页 HTML
"""

from __future__ import annotations

import html
import time

from .market import Candle


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt(v, d=2) -> str:
    return "--" if v is None else f"{v:,.{d}f}"


def _row(c):
    """把 Candle 或其 dict 统一成 (ts, open, high, low, close) 元组。"""
    if isinstance(c, dict):
        return (c["ts"], c["open"], c["high"], c["low"], c["close"])
    return (c.ts, c.open, c.high, c.low, c.close)


def svg_candles(candles, *, width: int = 900, height: int = 320,
                plan: dict | None = None) -> str:
    """渲染一张 SVG 蜡烛图（K线 + 关键位：止损/止盈/入场）。

    candles 可以是 Candle 对象列表，也可以是 Candle.to_dict() 的列表。
    """
    rows = [r for r in (_row(c) for c in candles) if r[4] > 0]  # close > 0
    if not rows:
        return '<div class="empty">无 K 线数据</div>'
    lo = min(r[3] for r in rows)   # low
    hi = max(r[2] for r in rows)   # high
    pad = (hi - lo) * 0.08 or hi * 0.02 or 1
    lo, hi = lo - pad, hi + pad

    n = len(rows)
    cw = width / max(1, n)
    body_w = max(1.0, cw * 0.6)

    def y(price):
        return height - (price - lo) / (hi - lo) * height

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" class="chart" xmlns="http://www.w3.org/2000/svg">']
    for i, (ts, o, h, low, c) in enumerate(rows):
        x = i * cw + cw / 2
        up = c >= o
        col = "#22c55e" if up else "#ef4444"
        yc, yo = y(c), y(o)
        hh = abs(yc - yo) or 1.0
        parts.append(f'<rect x="{x - body_w/2:.2f}" y="{min(yc,yo):.2f}" '
                     f'width="{body_w:.2f}" height="{hh:.2f}" fill="{col}"/>')
        parts.append(f'<line x1="{x:.2f}" y1="{y(h):.2f}" '
                     f'x2="{x:.2f}" y2="{y(low):.2f}" stroke="{col}" stroke-width="1"/>')
    # 关键位（入场/止损/止盈）横向线
    def hline(price, color, label, dash=True):
        if not price:
            return
        yy = y(float(price))
        d = ' stroke-dasharray="4 3"' if dash else ''
        parts.append(f'<line x1="0" y1="{yy:.2f}" x2="{width}" y2="{yy:.2f}" '
                     f'stroke="{color}" stroke-width="1.2"{d} opacity="0.85"/>')
        parts.append(f'<text x="4" y="{yy - 3:.2f}" fill="{color}" font-size="10">'
                     f'{_esc(label)} {_esc(_fmt(price))}</text>')

    if plan:
        d = plan.get("direction")
        hline(plan.get("entry_low"), "#3b82f6", "入场")
        hline(plan.get("entry_high"), "#3b82f6", "入场", dash=True)
        hline(plan.get("stop"), "#ef4444", "止损")
        for t in (plan.get("targets") or [])[:3]:
            hline(t.get("price"), "#22c55e", t.get("label") or "TP")
    parts.append('</svg>')
    return "\n".join(parts)


_REPORT_CSS = """
body{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  margin:0;padding:28px;background:#0b1020;color:#e8edf7}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8f9cbb;font-size:12px;margin-bottom:18px}
.card{background:#151d33;border:1px solid #243055;border-radius:12px;padding:16px;
  margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 10px;color:#cfe0ff}
.big{font-size:22px;font-weight:700}
.up{color:#22c55e}.down{color:#ef4444}.dim{color:#8f9cbb}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:5px 4px;border-top:1px solid #1c2747}
td.k{color:#8f9cbb;width:88px}
.chart{display:block}
.dir.long{color:#22c55e}.dir.short{color:#ef4444}
.factors{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:6px 18px}
.f{display:flex;justify-content:space-between;border-bottom:1px dotted #243055;padding:3px 0}
.empty{color:#8f9cbb;padding:20px;text-align:center}
footer{color:#8f9cbb;font-size:11px;text-align:center;margin-top:18px}
@media print{body{background:#fff;color:#111}.card{border-color:#ccc}
  .sub,.dim,footer{color:#666}}
"""


def render_report(plan: dict, snap_summary: dict, candles,
                  *, symbol: str, mode: str, win_stats: dict | None = None) -> str:
    """生成一整页自包含 HTML 报告。"""
    direction = plan.get("direction") or "wait"
    dir_cn = {"long": "做多", "short": "做空", "wait": "观望"}.get(direction, direction)
    score = plan.get("score")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    # 关键指标表
    m = plan.get("market") or {}
    rows = [
        ("评分", f"{score:+.1f}" if score is not None else "--"),
        ("方向", dir_cn),
        ("置信度", f"{plan.get('confidence','--')}"
                  + (f" ({plan.get('confidence_score')})" if plan.get('confidence_score') is not None else "")),
        ("现价", _fmt(plan.get("price"), digits(plan.get("price")))),
        ("入场区间", f"{_fmt(plan.get('entry_low'), digits(plan.get('price')))} – "
                     f"{_fmt(plan.get('entry_high'), digits(plan.get('price')))}"),
        ("止损", f"{_fmt(plan.get('stop'), digits(plan.get('price')))}"
                 f"（{_fmt(plan.get('stop_pct'))}%）" if plan.get("stop") else "--"),
        ("盈亏比", _fmt(plan.get("rr"))),
    ]
    if m:
        rows += [
            ("市场状态", f"{m.get('regime','--')}（ADX {_fmt(m.get('adx'),1)}）"),
            ("24h 涨跌", f"{_fmt(m.get('change_24h_pct'))}%"),
        ]
    s = plan.get("sizing") or {}
    if s.get("applicable"):
        rows += [
            ("建议仓位", f"{s.get('contracts')} 张 / 保证金 {_fmt(s.get('margin_usdt'))} U"),
            ("止损亏损", f"{_fmt(s.get('loss_if_stopped_usdt'))} U（{_fmt(s.get('loss_pct_of_equity'))}%）"),
        ]

    targets = "".join(
        f'<tr><td class="k">{_esc(t.get("label"))}</td>'
        f'<td>{_fmt(t.get("price"), digits(plan.get("price")))}</td>'
        f'<td>RR {_fmt(t.get("rr"))}</td></tr>' for t in (plan.get("targets") or []))

    factors = "".join(
        f'<div class="f"><span>{_esc(f.get("label"))}</span>'
        f'<span>{f.get("score",0):+.2f}×{f.get("weight",0):.2f}'
        f'={f.get("contribution",0):+.3f}</span></div>'
        for f in (plan.get("factors") or []))

    warnings = "".join(f'<li>{_esc(w)}</li>' for w in (plan.get("warnings") or []))

    win_block = ""
    if win_stats and win_stats.get("resolved"):
        wr = win_stats.get("win_rate")
        win_block = f"""
    <div class="card"><h2>策略胜率（已回填信号）</h2>
      <div>已判定 <span class="big">{win_stats.get('resolved')}</span> 笔 ·
        胜率 <span class="big {'up' if (wr or 0)>=0.5 else 'down'}">{(wr*100):.1f}%</span> ·
        平均 R <span class="big">{_fmt(win_stats.get('avg_r'))}</span></div>
    </div>"""

    return f"""<!DOCTYPE html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(symbol)} 分析报告 · {ts}</title><style>{_REPORT_CSS}</style></head><body>
<div class="wrap">
  <h1>{_esc(symbol)} · 投资建议报告 <span class="dir {direction}">{dir_cn}</span></h1>
  <div class="sub">生成时间 {ts} · 模式 {mode} · iabot（只读行情，不构成投资建议）</div>

  <div class="card"><h2>K 线与关键位</h2>{svg_candles(candles, plan=plan)}</div>

  <div class="card"><h2>交易建议</h2><table>
    {''.join(f'<tr><td class="k">{k}</td><td>{v}</td></tr>' for k, v in rows)}
  </table>{f'<div class="dim" style="margin-top:8px">{_esc(plan.get("entry_note") or "")}</div>' if plan.get("entry_note") else ''}</div>

  {f'<div class="card"><h2>止盈目标</h2><table>{targets}</table></div>' if targets else ''}
  {win_block}
  {f'<div class="card"><h2>因子明细</h2><div class="factors">{factors}</div></div>' if factors else ''}
  {f'<div class="card"><h2>风险提示</h2><ul class="dim">{warnings}</ul></div>' if warnings else ''}
  <footer>iabot · 只读公开行情接口，不下单、不动真钱 · 本报告不构成投资建议</footer>
</div></body></html>"""


def digits(price) -> int:
    import math
    v = abs(float(price or 0))
    if not v:
        return 2
    return max(2, min(8, 5 - math.floor(math.log10(v))))
