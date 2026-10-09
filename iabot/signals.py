"""信号模型：多因子打分 -> 方向 -> 入场/止损/止盈 -> 仓位。

设计原则和"Agent 写规则、引擎跑规则"一致：把人工看盘的那套判断固化成本地
确定性计算，任何一次分析都可以复现，也不依赖大模型。

支持两种模式：
  intraday（日内）  —— 用 15 分钟 ATR 定止损（约 0.4~0.8%），目标位取自 5m/15m/1h
                        的结构位与盘口挂单，持仓周期数小时。
  swing（波段）     —— 用日线 ATR 定止损（约 2~3%），目标位取自日线结构、
                        斐波那契与成交密集区，持仓周期数天到数周。
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field

from . import indicators as ind
from .levels import (Level, fib_levels, merge_levels, moving_average_levels,
                     orderbook_walls, price_ladder, round_number_levels,
                     swing_points, top_volume_nodes)
from .market import Snapshot, bars_per_year

# tanh 归一化尺度：加权均值除以它之后过 tanh。
# 0.40 => 加权均值 ±0.40 约对应 ±76 分；±0.20 约对应 ±46 分。
SCORE_SCALE = 0.40

MODES = {
    "intraday": {
        "label": "日内",
        "atr_period": "15min",
        "stop_atr_mult": 2.0,      # 止损 = 2.0 × ATR(15m)
        "min_target_atr": 1.4,     # 目标位至少这么远才有意义
        "max_target_atr": 14.0,
        "min_dist_atr": 0.45,      # 入场基准离现价至少这么远
        "level_lookback": {"5min": 288, "15min": 200, "60min": 160},
        "use_daily_fib": False,
        "stop_pct_range": (0.30, 2.50),   # 止损幅度（占价格 %）的合理区间
        "entry_width_pct": 0.12,          # 入场带最小宽度
    },
    "swing": {
        "label": "波段",
        "atr_period": "1day",
        "stop_atr_mult": 0.8,
        "min_target_atr": 1.0,
        "max_target_atr": 9.0,
        "min_dist_atr": 0.35,
        "level_lookback": {"60min": 240, "1day": 90},
        "use_daily_fib": True,
        "stop_pct_range": (1.50, 6.00),
        "entry_width_pct": 0.50,
    },
}


@dataclass
class Factor:
    name: str
    label: str
    score: float          # -1(极空) .. +1(极多)
    weight: float
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "score": round(self.score, 3),
                "weight": round(self.weight, 3), "detail": self.detail,
                "contribution": round(self.score * self.weight, 4)}


@dataclass
class Plan:
    symbol: str
    mode: str = "intraday"
    direction: str = "wait"          # long | short | wait
    score: float = 0.0               # -100 .. +100
    confidence: str = "低"
    entry_low: float | None = None
    entry_high: float | None = None
    entry_note: str = ""
    stop: float | None = None
    stop_pct: float | None = None
    targets: list[dict] = field(default_factory=list)
    rr: float | None = None
    invalidation: str = ""
    sizing: dict = field(default_factory=dict)
    factors: list[Factor] = field(default_factory=list)
    market: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 被风控闸门拦下来时的解释：评分给了方向，但位置不划算
    gate: dict = field(default_factory=dict)
    price: float | None = None
    generated_at: float = 0.0

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "factors"}
        d["factors"] = [f.to_dict() for f in self.factors]
        return d

    def headline(self) -> str:
        if self.direction == "wait":
            return f"{self.symbol} 观望（{self.score:+.0f}）"
        cn = "做多" if self.direction == "long" else "做空"
        return f"{self.symbol} {cn}（{self.score:+.0f}，置信度{self.confidence}）"


# --------------------------------------------------------------------------
# 因子
# --------------------------------------------------------------------------

def _f_trend(snap: Snapshot) -> Factor:
    cs = snap.closes("1day")
    if len(cs) < 60:
        return Factor("trend_daily", "日线趋势", 0.0, 0.20, "日线数据不足")
    price = snap.price or cs[-1]
    s20, s50, s200 = ind.last(ind.sma(cs, 20)), ind.last(ind.sma(cs, 50)), ind.last(ind.sma(cs, 200))
    e12, e26 = ind.last(ind.ema(cs, 12)), ind.last(ind.ema(cs, 26))
    pts = 0.0
    parts = []
    for name, v, w in (("SMA20", s20, 1.0), ("SMA50", s50, 1.3), ("SMA200", s200, 1.6)):
        if v:
            above = price > v
            pts += w if above else -w
            parts.append(f"{'上' if above else '下'}穿{name}({(price / v - 1) * 100:+.1f}%)")
    if e12 and e26:
        pts += 0.8 if e12 > e26 else -0.8
        parts.append(f"EMA12{'＞' if e12 > e26 else '＜'}EMA26")
    return Factor("trend_daily", "日线趋势", max(-1.0, min(1.0, pts / 4.7 * 1.35)), 0.20,
                  "；".join(parts))


def _f_intraday(snap: Snapshot) -> Factor:
    cs = snap.closes("60min")
    if len(cs) < 30:
        return Factor("trend_intraday", "1小时趋势", 0.0, 0.12, "1小时数据不足")
    price = snap.price or cs[-1]
    s20, s50 = ind.last(ind.sma(cs, 20)), ind.last(ind.sma(cs, 50))
    _, _, hist = ind.macd(cs)
    h = ind.last(hist)
    pts = 0.0
    parts = []
    if s20:
        above = price > s20
        pts += 1.0 if above else -1.0
        parts.append(f"现价在 SMA20 {'上' if above else '下'}方 {(price / s20 - 1) * 100:+.2f}%")
    if s50:
        above = price > s50
        pts += 1.2 if above else -1.2
        parts.append(f"SMA50 {'上' if above else '下'}方 {(price / s50 - 1) * 100:+.2f}%")
    if h is not None:
        pts += 0.9 if h > 0 else -0.9
        parts.append(f"MACD柱{'正' if h > 0 else '负'}({h:+,.2f})")
    return Factor("trend_intraday", "1小时趋势", max(-1.0, min(1.0, pts / 3.1)), 0.12,
                  "；".join(parts))


def _f_momentum(snap: Snapshot) -> Factor:
    cs15, cs60 = snap.closes("15min"), snap.closes("60min")
    r15 = ind.last(ind.rsi(cs15, 14)) if len(cs15) > 20 else None
    r60 = ind.last(ind.rsi(cs60, 14)) if len(cs60) > 20 else None
    if r15 is None and r60 is None:
        return Factor("momentum", "动量(RSI)", 0.0, 0.15, "RSI 数据不足")
    pts, parts = 0.0, []
    for label, r in (("15m", r15), ("1h", r60)):
        if r is None:
            continue
        if r >= 75:
            pts -= 0.7; parts.append(f"{label} RSI {r:.0f} 超买")
        elif r >= 60:
            pts += 0.5; parts.append(f"{label} RSI {r:.0f} 偏强")
        elif r <= 25:
            pts += 0.7; parts.append(f"{label} RSI {r:.0f} 超卖")
        elif r <= 40:
            pts -= 0.5; parts.append(f"{label} RSI {r:.0f} 偏弱")
        else:
            parts.append(f"{label} RSI {r:.0f} 中性")
    return Factor("momentum", "动量(RSI)", max(-1.0, min(1.0, pts / 1.4)), 0.15, "；".join(parts))


def _f_structure(snap: Snapshot) -> Factor:
    cs = snap.klines.get("60min", [])
    if len(cs) < 40:
        return Factor("structure", "摆动结构", 0.0, 0.14, "数据不足")
    highs, lows = swing_points(cs, width=3)
    pts, parts = 0.0, []
    if len(highs) >= 2:
        up = highs[-1][1] > highs[-2][1]
        pts += 0.7 if up else -0.7
        parts.append(f"高点{'抬升' if up else '降低'} {highs[-2][1]:,.2f}→{highs[-1][1]:,.2f}")
    if len(lows) >= 2:
        up = lows[-1][1] > lows[-2][1]
        pts += 0.7 if up else -0.7
        parts.append(f"低点{'抬升' if up else '降低'} {lows[-2][1]:,.2f}→{lows[-1][1]:,.2f}")
    a = ind.last(ind.adx([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], 14))
    if a is not None:
        parts.append(f"ADX {a:.0f} {'趋势市' if a >= 25 else ('震荡市' if a < 20 else '过渡')}")
    return Factor("structure", "摆动结构", max(-1.0, min(1.0, pts / 1.4)), 0.14, "；".join(parts))


def _f_vwap(snap: Snapshot) -> Factor:
    cs = snap.klines.get("5min", [])
    if len(cs) < 30:
        return Factor("vwap", "日内VWAP", 0.0, 0.08, "数据不足")
    day_start = (cs[-1].ts // 86400) * 86400
    start = next((i for i, c in enumerate(cs) if c.ts >= day_start), 0)
    v = ind.last(ind.vwap([c.high for c in cs], [c.low for c in cs],
                          [c.close for c in cs], [c.volume for c in cs], start=start))
    price = snap.price
    if v is None or not price:
        return Factor("vwap", "日内VWAP", 0.0, 0.08, "无法计算")
    dev = (price / v - 1.0) * 100
    return Factor("vwap", "日内VWAP", max(-1.0, min(1.0, dev / 0.8)), 0.08,
                  f"现价 vs VWAP {v:,.2f}（{dev:+.2f}%）")


def _f_funding(snap: Snapshot) -> Factor:
    r = snap.funding_rate
    if r is None:
        return Factor("funding", "资金费率", 0.0, 0.10, "无数据")
    ann = r * 3 * 365 * 100
    mean30 = None
    if snap.funding_history:
        vals = [float(x.get("funding_rate") or 0) for x in snap.funding_history[-90:]]
        if vals:
            mean30 = sum(vals) / len(vals) * 3 * 365 * 100
    if r >= 0.0005:
        score = -0.9
    elif r >= 0.0002:
        score = -0.45
    elif r <= -0.0005:
        score = 0.9
    elif r <= -0.0002:
        score = 0.45
    else:
        score = 0.0
    detail = f"当前 {r * 100:+.4f}%/8h（年化 {ann:+.1f}%）"
    if mean30 is not None:
        detail += f"，30日均 {mean30:+.1f}%"
    detail += "，多头拥挤" if score < 0 else ("空头拥挤" if score > 0 else "，情绪中性")
    return Factor("funding", "资金费率", score, 0.10, detail)


def _f_oi(snap: Snapshot) -> Factor:
    hist = snap.oi_history
    if len(hist) < 8 or not snap.price:
        return Factor("open_interest", "持仓量", 0.0, 0.09, "数据不足")
    now = float(hist[-1].get("volume") or 0)
    ago = float(hist[-8].get("volume") or 0)
    if not now or not ago:
        return Factor("open_interest", "持仓量", 0.0, 0.09, "数据不足")
    oi_chg = (now / ago - 1.0) * 100
    cs = snap.closes("1day")
    px_chg = ((cs[-1] / cs[-8] - 1.0) * 100) if len(cs) > 8 else None
    if px_chg is None:
        return Factor("open_interest", "持仓量", 0.0, 0.09, f"7日持仓 {oi_chg:+.1f}%")
    if px_chg > 0.5 and oi_chg > 1.0:
        score, tag = 0.6, "价涨仓增（多头主动进场）"
    elif px_chg > 0.5 and oi_chg < -1.0:
        score, tag = -0.3, "价涨仓减（空头回补，易衰竭）"
    elif px_chg < -0.5 and oi_chg > 1.0:
        score, tag = -0.7, "价跌仓增（空头主动进场）"
    elif px_chg < -0.5 and oi_chg < -1.0:
        score, tag = 0.3, "价跌仓减（多头止损离场，接近洗盘尾声）"
    else:
        score, tag = 0.0, "量价背离不明显"
    return Factor("open_interest", "持仓量", score, 0.09,
                  f"7日持仓 {oi_chg:+.1f}%、价格 {px_chg:+.1f}% · {tag}")


def _f_elite(snap: Snapshot) -> Factor:
    rows = snap.elite_position
    if not rows:
        return Factor("elite", "大户持仓", 0.0, 0.12, "接口无数据")

    def net(r):
        return float(r.get("buy_ratio") or 0) - float(r.get("sell_ratio") or 0)

    latest = net(rows[-1])
    window = rows[-14:] if len(rows) >= 14 else rows
    avg = sum(net(r) for r in window) / len(window)
    trend = ""
    if len(rows) >= 4:
        prev = net(rows[-4])
        if latest - prev > 0.02:
            trend = "且近 3 日在转多"
        elif latest - prev < -0.02:
            trend = "且近 3 日在转空"
    return Factor("elite", "大户持仓", max(-1.0, min(1.0, latest / 0.12)), 0.12,
                  f"多空净比 {latest:+.3f}（14日均 {avg:+.3f}）{trend}")


def _f_orderbook(snap: Snapshot, price: float | None) -> Factor:
    if not snap.depth or not price:
        return Factor("orderbook", "盘口深度", 0.0, 0.07, "无盘口数据")
    cs = snap.contract_size
    bid = sum(q for p, q in (snap.depth.get("bids") or []) if p >= price * 0.99)
    ask = sum(q for p, q in (snap.depth.get("asks") or []) if p <= price * 1.01)
    if bid + ask <= 0:
        return Factor("orderbook", "盘口深度", 0.0, 0.07, "无盘口数据")
    ratio = bid / ask
    score = max(-1.0, min(1.0, math.log(ratio) / math.log(3))) if ratio > 0 else 0.0
    tag = "买盘占优" if ratio > 1.15 else ("卖盘占优" if ratio < 0.87 else "均衡")
    return Factor("orderbook", "盘口深度", score, 0.07,
                  f"±1% 买 {bid * cs:,.1f} / 卖 {ask * cs:,.1f} 币，比值 {ratio:.2f}（{tag}）")


def _f_volatility(snap: Snapshot) -> Factor:
    cs = snap.closes("1day")
    detail = []
    atr_v = None
    if snap.klines.get("1day"):
        k = snap.klines["1day"]
        atr_v = ind.last(ind.atr([c.high for c in k], [c.low for c in k], [c.close for c in k], 14))
    rv = ind.realized_vol_pct(cs, 30, bars_per_year("1day")) if len(cs) > 31 else None
    if rv is not None:
        detail.append(f"30日年化波动 {rv:.0f}%")
    if atr_v and snap.price:
        detail.append(f"日ATR {atr_v:,.2f}（{atr_v / snap.price * 100:.2f}%）")
    score = 0.0
    if rv is not None and rv > 70:
        score = -0.5
        detail.append("波动过高，建议降杠杆")
    return Factor("volatility", "波动率", score, 0.05, "；".join(detail) or "无数据")


# (因子函数, 是否需要现价)。只有盘口因子额外要一个 price 参数，
# 显式写在这里，比在调用处做 `fn is _f_orderbook` 的标识比较更不容易出错。
FACTOR_FNS = [
    (_f_trend, False), (_f_intraday, False), (_f_momentum, False),
    (_f_structure, False), (_f_vwap, False), (_f_funding, False),
    (_f_oi, False), (_f_elite, False), (_f_orderbook, True), (_f_volatility, False),
]


def compute_factors(snap: Snapshot) -> list[Factor]:
    out: list[Factor] = []
    for fn, needs_price in FACTOR_FNS:
        try:
            out.append(fn(snap, snap.price) if needs_price else fn(snap))
        except Exception as exc:
            out.append(Factor(fn.__name__.replace("_f_", ""), "计算异常", 0.0, 0.0,
                              f"{type(exc).__name__}: {exc}"))
    return out


# --------------------------------------------------------------------------
# 价位阶梯
# --------------------------------------------------------------------------

def collect_levels(snap: Snapshot, price: float, mode: str = "intraday") -> list[Level]:
    m = MODES.get(mode, MODES["intraday"])
    lb = m["level_lookback"]
    lv: list[Level] = []

    for period, n in lb.items():
        k = snap.klines.get(period)
        if not k:
            continue
        window = k[-n:]
        if len(window) >= 20:
            lv += top_volume_nodes(window, bins=36, top=4, price=price,
                                   max_dist_pct=10.0 if mode == "intraday" else 25.0)
            hs, ls = swing_points(window, 3)
            w = 0.5 if mode == "intraday" else 0.6
            for _, p in hs[-6:]:
                lv.append(Level(p, "resistance" if p > price else "support", w,
                                f"{period}摆动高点", ""))
            for _, p in ls[-6:]:
                lv.append(Level(p, "resistance" if p > price else "support", w,
                                f"{period}摆动低点", ""))

    if m.get("use_daily_fib") and snap.klines.get("1day"):
        k1 = snap.klines["1day"][-90:]
        hi, lo = max(c.high for c in k1), min(c.low for c in k1)
        if hi > lo:
            for r, p in fib_levels(hi, lo):
                lv.append(Level(p, "resistance" if p > price else "support",
                                0.5 if 0.3 <= r <= 0.7 else 0.35,
                                f"斐波{r * 100:.1f}%", f"{lo:,.2f}~{hi:,.2f}"))
    elif snap.klines.get("60min"):
        k60 = snap.klines["60min"][-72:]
        hi, lo = max(c.high for c in k60), min(c.low for c in k60)
        if hi > lo:
            for r, p in fib_levels(hi, lo, (0.382, 0.5, 0.618)):
                lv.append(Level(p, "resistance" if p > price else "support", 0.35,
                                f"1h斐波{r * 100:.1f}%", f"{lo:,.2f}~{hi:,.2f}"))

    # 均线 / VWAP
    mas: dict[str, float | None] = {}
    cs60 = snap.closes("60min")
    cs15 = snap.closes("15min")
    cs1d = snap.closes("1day")
    if len(cs15) >= 50:
        mas["MA20(15m)"] = ind.last(ind.sma(cs15, 20))
    if len(cs60) >= 50:
        mas["MA50(1h)"] = ind.last(ind.sma(cs60, 50))
    if len(cs1d) >= 20:
        mas["EMA12"] = ind.last(ind.ema(cs1d, 12))
        mas["EMA26"] = ind.last(ind.ema(cs1d, 26))
    if len(cs1d) >= 50:
        mas["SMA20"] = ind.last(ind.sma(cs1d, 20))
        mas["SMA50"] = ind.last(ind.sma(cs1d, 50))
    if len(cs1d) >= 200:
        mas["SMA200"] = ind.last(ind.sma(cs1d, 200))
    ks = snap.klines.get("5min") or []
    if len(ks) >= 30:
        day_start = (ks[-1].ts // 86400) * 86400
        st = next((i for i, c in enumerate(ks) if c.ts >= day_start), 0)
        mas["VWAP"] = ind.last(ind.vwap([c.high for c in ks], [c.low for c in ks],
                                        [c.close for c in ks], [c.volume for c in ks], start=st))
    lv += moving_average_levels(price, mas, span_pct=3.0 if mode == "intraday" else 8.0)

    lv += round_number_levels(price, span_pct=1.0 if mode == "intraday" else 2.5)
    lv += orderbook_walls(snap.depth, price, pct=1.5, contract_size=snap.contract_size)
    return merge_levels(price, lv, cluster_pct=0.10)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def build_plan(snap: Snapshot, cfg: dict, mode: str = "intraday") -> Plan:
    mode = mode if mode in MODES else "intraday"
    m = MODES[mode]
    account = cfg.get("account") or {}
    equity = float(account.get("equity_usdt") or 0)
    risk_pct = float(account.get("risk_pct_per_trade") or 2.0)
    leverage = int(account.get("preferred_leverage") or 5)
    leverage = max(1, min(leverage, int(account.get("max_leverage") or 10)))
    max_margin_pct = float(account.get("max_margin_pct") or 50.0)
    threshold = float((cfg.get("analysis") or {}).get("score_threshold") or 30)

    plan = Plan(symbol=snap.symbol, mode=mode, price=snap.price, generated_at=_time.time())
    if not snap.price:
        plan.notes.append("拿不到最新价，无法分析。")
        plan.warnings.extend(snap.errors)
        return plan

    price = snap.price
    factors = compute_factors(snap)
    plan.factors = factors
    raw = sum(f.score * f.weight for f in factors)
    total_w = sum(f.weight for f in factors if f.weight > 0) or 1.0
    # 加权平均天然向 0 收缩（因子之间经常互相抵消），线性映射会让评分永远落在
    # ±30 内、阈值形同虚设。tanh 压缩后中间区域被放大，极端区域饱和。
    plan.score = round(100.0 * math.tanh((raw / total_w) / SCORE_SCALE), 1)

    # ---- 波动率与市场状态 ----
    atr_k = snap.klines.get(m["atr_period"]) or []
    atr_v = None
    if len(atr_k) > 15:
        atr_v = ind.last(ind.atr([c.high for c in atr_k], [c.low for c in atr_k],
                                 [c.close for c in atr_k], 14))
    k60 = snap.klines.get("60min", [])
    adx_v = ind.last(ind.adx([c.high for c in k60], [c.low for c in k60],
                             [c.close for c in k60], 14)) if len(k60) > 30 else None
    rv = ind.realized_vol_pct(snap.closes("1day"), 30, 365) if len(snap.closes("1day")) > 31 else None
    plan.market = {
        "mode": mode, "mode_label": m["label"],
        "atr_period": m["atr_period"],
        "atr": round(atr_v, 8) if atr_v else None,
        "atr_pct": round(atr_v / price * 100, 3) if atr_v else None,
        "adx": round(adx_v, 1) if adx_v is not None else None,
        "regime": ("趋势" if (adx_v or 0) >= 25 else ("震荡" if (adx_v or 0) < 20 else "过渡")),
        "rv_30d_ann_pct": round(rv, 1) if rv is not None else None,
        "change_24h_pct": round(snap.change_24h_pct, 2) if snap.change_24h_pct is not None else None,
        "basis_pct": round(snap.basis_pct, 4) if snap.basis_pct is not None else None,
    }

    if not atr_v:
        plan.direction = "wait"
        plan.notes.append(f"缺少 {m['atr_period']} 数据，无法计算止损距离。")
        plan.warnings.extend(snap.errors)
        return plan

    # ---- 价位阶梯 ----
    all_levels = collect_levels(snap, price, mode)
    min_dist = atr_v * m["min_dist_atr"]
    max_dist = atr_v * m["max_target_atr"]
    up = price_ladder(price, all_levels, min_dist=min_dist, max_dist=max_dist, direction="up")
    dn = price_ladder(price, all_levels, min_dist=min_dist, max_dist=max_dist, direction="down")
    plan.levels = {"resistance": up, "support": dn}

    if plan.score >= threshold:
        plan.direction = "long"
    elif plan.score <= -threshold:
        plan.direction = "short"
    else:
        plan.direction = "wait"

    stop_mult = m["stop_atr_mult"]
    buf = atr_v * stop_mult

    if plan.direction == "wait":
        plan.confidence = "低"
        plan.entry_note = f"评分 {plan.score:+.0f} 未达到 ±{threshold:.0f} 阈值，方向不明。"
        if dn and up:
            plan.entry_note += f" 关注区间 {dn[0]['price']:,.2f} – {up[0]['price']:,.2f}。"
            plan.invalidation = (f"站稳 {up[0]['price']:,.2f} 转多；"
                                 f"跌破 {dn[0]['price']:,.2f} 转空。")
        plan.warnings.extend(snap.errors)
        _attach_sizing(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v)
        return plan

    # ---- 入场 / 止损 / 止盈 ----
    if plan.direction == "long":
        support = dn[0]["price"] if dn else None
        # 支撑离现价合适就挂到支撑上，否则用现价下方一小段回撤位
        if support and (price - support) <= atr_v * 1.2:
            base = support + atr_v * 0.10
        else:
            base = price - atr_v * 0.30
        plan.entry_low = round(base, 8)
        plan.entry_high = round(base + atr_v * 0.35, 8)
        plan.stop = round(base - buf, 8)
        risk = base - plan.stop
        cands = []
        for t in up:
            rr = (t["price"] - base) / risk if risk > 0 else 0
            if rr >= 0.8:
                cands.append((rr, t))
        # 按"先到的先算"排序：RR 越小离得越近
        cands.sort(key=lambda x: x[0])
        for i, (rr, t) in enumerate(cands[:3], 1):
            plan.targets.append({"price": t["price"], "label": f"TP{i}",
                                 "rr": round(rr, 2), "source": t.get("source", "")})
        plan.invalidation = f"日线收盘跌破 {plan.stop:,.2f} 则多头逻辑作废"
        plan.entry_note = ("现价已进入入场区，可挂单或直接进。" if price <= plan.entry_high
                           else f"等回踩到 {plan.entry_low:,.2f}–{plan.entry_high:,.2f} 再进，现价追多风险高。")
    else:
        resist = up[0]["price"] if up else None
        if resist and (resist - price) <= atr_v * 1.2:
            base = resist - atr_v * 0.10
        else:
            base = price + atr_v * 0.30
        plan.entry_high = round(base, 8)
        plan.entry_low = round(base - atr_v * 0.35, 8)
        plan.stop = round(base + buf, 8)
        risk = plan.stop - base
        cands = []
        for t in dn:
            rr = (base - t["price"]) / risk if risk > 0 else 0
            if rr >= 0.8:
                cands.append((rr, t))
        cands.sort(key=lambda x: x[0])
        for i, (rr, t) in enumerate(cands[:3], 1):
            plan.targets.append({"price": t["price"], "label": f"TP{i}",
                                 "rr": round(rr, 2), "source": t.get("source", "")})
        plan.invalidation = f"日线收盘站上 {plan.stop:,.2f} 则空头逻辑作废"
        plan.entry_note = ("现价已进入入场区，可挂单或直接进。" if price >= plan.entry_low
                           else f"等反抽到 {plan.entry_low:,.2f}–{plan.entry_high:,.2f} 再进，现价追空风险高。")

    # ---- 止损距离夹紧：太窄容易被噪声打掉，太宽则仓位小到没意义 ----
    lo_pct, hi_pct = m["stop_pct_range"]
    mid = (plan.entry_low + plan.entry_high) / 2
    stop_dist = abs(mid - plan.stop)
    clamped_lo, clamped_hi = mid * lo_pct / 100.0, mid * hi_pct / 100.0
    if stop_dist < clamped_lo:
        plan.warnings.append(
            f"原始止损仅 {stop_dist / mid * 100:.2f}%（低于 {lo_pct}%），"
            f"已被放宽到 {lo_pct}% 以避免被噪声扫掉。")
        plan.stop = round(mid - clamped_lo if plan.direction == "long" else mid + clamped_lo, 8)
        stop_dist = clamped_lo
    elif stop_dist > clamped_hi:
        plan.warnings.append(
            f"原始止损 {stop_dist / mid * 100:.2f}% 过宽（超过 {hi_pct}%），"
            f"已收紧到 {hi_pct}%，请以更近的结构位为准。")
        plan.stop = round(mid - clamped_hi if plan.direction == "long" else mid + clamped_hi, 8)
        stop_dist = clamped_hi

    # ---- 入场带最小宽度：避免出现 0.02% 这种挂不进去的区间 ----
    min_w = mid * m["entry_width_pct"] / 100.0
    width = abs(plan.entry_high - plan.entry_low)
    if width < min_w:
        half = min_w / 2
        plan.entry_low = round(mid - half, 8)
        plan.entry_high = round(mid + half, 8)

    # 止损被夹紧后 RR 会变，重新计算目标位
    if plan.direction == "long":
        risk = (plan.entry_low + plan.entry_high) / 2 - plan.stop
        for i, t in enumerate(plan.targets, 1):
            t["rr"] = round((t["price"] - (plan.entry_low + plan.entry_high) / 2) / risk, 2)
            t["label"] = f"TP{i}"
    else:
        risk = plan.stop - (plan.entry_low + plan.entry_high) / 2
        for i, t in enumerate(plan.targets, 1):
            t["rr"] = round(((plan.entry_low + plan.entry_high) / 2 - t["price"]) / risk, 2)
            t["label"] = f"TP{i}"

    plan.stop_pct = round(stop_dist / mid * 100, 3)
    plan.rr = plan.targets[0]["rr"] if plan.targets else None

    # 盈亏比不达标就不给方向——给一个注定亏钱的单子是害人
    if not plan.targets:
        plan.warnings.append(
            f"{m['label']}级别找不到 ≥0.8 盈亏比的目标位（价格正卡在关键位中间）。")
        plan.gate = {"blocked": True, "reason": "no_target", "score": plan.score,
                     "would_be": plan.direction, "rr": None, "need": 0.8}
        return _downgrade(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v,
                          "评分有方向，但前方没有像样的空间，暂不出手。")
    if plan.rr is not None and plan.rr < 1.0:
        plan.warnings.append(f"调整止损后第一目标盈亏比只有 {plan.rr:.2f}（<1.0），风险大于收益。")
        plan.gate = {"blocked": True, "reason": "rr", "score": plan.score,
                     "would_be": plan.direction, "rr": round(plan.rr, 2), "need": 1.0}
        return _downgrade(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v,
                          "盈亏比不足 1:1，不出手。")
    if plan.rr is not None and plan.rr < 1.2:
        plan.warnings.append(f"第一目标盈亏比仅 {plan.rr:.2f}，性价比一般，建议等更好的位置。")

    agree = sum(1 for f in factors
                if abs(f.score) > 0.1 and (f.score > 0) == (plan.direction == "long"))
    strong = sum(1 for f in factors if abs(f.score) > 0.4)
    plan.confidence = ("高" if (abs(plan.score) >= 60 and agree >= 6)
                       else ("中" if abs(plan.score) >= 45 else "低"))
    plan.notes.append(f"{len(factors)} 个因子中 {agree} 个与方向一致，其中 {strong} 个为强信号。")
    plan.notes.append(f"信号强度：{'很强' if abs(plan.score) >= 70 else ('较强' if abs(plan.score) >= 50 else '一般')}"
                      f"；止损用 {stop_mult}×ATR({m['atr_period']})。")
    if any(f.name == "volatility" and f.score < 0 for f in factors):
        plan.warnings.append("当前波动率偏高，建议把杠杆降到 5 倍以内。")

    _attach_sizing(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v)
    plan.warnings.extend(snap.errors)
    return plan


def _attach_sizing(plan: Plan, snap: Snapshot, equity: float, risk_pct: float,
                   leverage: int, max_margin_pct: float, atr_v: float | None) -> None:
    """按"单笔亏损不超过账户 risk_pct%"反推张数。"""
    if plan.direction == "wait" or not plan.stop or not plan.price or equity <= 0:
        plan.sizing = {"applicable": False,
                       "note": "没有明确方向，暂不给仓位。" if plan.direction == "wait"
                               else "缺少保证金参数。"}
        return
    mid = (plan.entry_low + plan.entry_high) / 2 if plan.entry_low and plan.entry_high else plan.price
    stop_dist = abs(mid - plan.stop)
    if stop_dist <= 0:
        plan.sizing = {"applicable": False, "note": "止损距离为 0。"}
        return

    cs = snap.contract_size or 0.001
    risk_amount = equity * risk_pct / 100.0
    contracts = max(1.0, math.floor(risk_amount / (stop_dist * cs)))
    notional = contracts * cs * mid
    margin = notional / leverage
    max_margin = equity * max_margin_pct / 100.0
    reduced = False
    if margin > max_margin:
        contracts = max(1.0, math.floor(max_margin * leverage / (cs * mid)))
        notional = contracts * cs * mid
        margin = notional / leverage
        reduced = True

    loss = contracts * cs * stop_dist
    tps = []
    for t in plan.targets:
        gain = contracts * cs * abs(t["price"] - mid)
        tps.append({"label": t["label"], "price": t["price"],
                    "profit_usdt": round(gain, 2),
                    "profit_pct_of_equity": round(gain / equity * 100, 2),
                    "rr": t["rr"]})

    plan.sizing = {
        "applicable": True,
        "equity_usdt": equity,
        "risk_pct": risk_pct,
        "risk_amount_usdt": round(risk_amount, 2),
        "contracts": int(contracts),
        "contract_size": cs,
        "position_coin": round(contracts * cs, 8),
        "notional_usdt": round(notional, 2),
        "leverage": leverage,
        "margin_usdt": round(margin, 2),
        "margin_pct_of_equity": round(margin / equity * 100, 2),
        "stop_distance": round(stop_dist, 8),
        "loss_if_stopped_usdt": round(loss, 2),
        "loss_pct_of_equity": round(loss / equity * 100, 2),
        "targets": tps,
        "note": ("保证金超出上限，已自动缩减张数。" if reduced
                 else f"单笔亏损控制在账户 {risk_pct:.1f}% 以内。"),
        "fee_estimate_usdt": round(notional * 0.0005 * 2, 3),
    }


def _downgrade(plan: Plan, snap: Snapshot, equity: float, risk_pct: float,
               leverage: int, max_margin_pct: float, atr_v: float | None,
               reason: str) -> Plan:
    """把计划降级为观望，并清掉会误导人的入场/止损数据。"""
    plan.direction = "wait"
    plan.confidence = "低"
    plan.entry_note = reason
    plan.entry_low = plan.entry_high = None
    plan.stop = None
    plan.stop_pct = None
    plan.targets = []
    plan.rr = None
    plan.invalidation = ""
    _attach_sizing(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v)
    plan.warnings.extend(snap.errors)
    return plan
