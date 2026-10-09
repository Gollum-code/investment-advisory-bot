"""关键价位计算：摆动点、成交量密集区、斐波那契、盘口挂单墙。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .market import Candle


@dataclass
class Level:
    price: float
    kind: str            # support | resistance
    weight: float        # 0~1，越大越重要
    source: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"price": round(self.price, 8), "kind": self.kind,
                "weight": round(self.weight, 3), "source": self.source, "detail": self.detail}


def swing_points(candles: list[Candle], width: int = 3):
    """分形摆动点。返回 (highs, lows)，元素是 (index, price)。"""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    n = len(candles)
    for i in range(width, n - width):
        h = candles[i].high
        l = candles[i].low
        if h >= max(c.high for c in candles[i - width:i + width + 1]):
            highs.append((i, h))
        if l <= min(c.low for c in candles[i - width:i + width + 1]):
            lows.append((i, l))
    return highs, lows


def volume_profile(candles: list[Candle], bins: int = 40) -> list[tuple[float, float, float]]:
    """成交量密集区。返回 [(low, high, volume), ...] 按价格升序。"""
    if not candles:
        return []
    lo = min(c.low for c in candles)
    hi = max(c.high for c in candles)
    if hi <= lo:
        return [(lo, hi, sum(c.volume for c in candles))]
    step = (hi - lo) / bins
    buckets = [[0.0, lo + i * step, lo + (i + 1) * step] for i in range(bins)]
    for c in candles:
        tp = (c.high + c.low + c.close) / 3.0
        idx = min(int((tp - lo) / step), bins - 1)
        buckets[idx][0] += c.volume
    return [(b[1], b[2], b[0]) for b in buckets if b[0] > 0]


def top_volume_nodes(candles: list[Candle], bins: int = 40, top: int = 5,
                     price: float | None = None,
                     max_dist_pct: float = 12.0) -> list[Level]:
    """成交量密集区。相邻的高量簇会合并成一条"价格带"，避免只盯单个 bin。"""
    prof = volume_profile(candles, bins)
    if not prof:
        return []
    total = sum(v for _, _, v in prof) or 1.0
    step = prof[0][1] - prof[0][0] if len(prof) > 1 else 0.0

    # 先按成交量排序，取前若干，再合并价格相邻的簇
    ranked = sorted(range(len(prof)), key=lambda i: -prof[i][2])
    picked: list[int] = []
    avg = total / len(prof)
    for i in ranked:
        if prof[i][2] < avg * 1.15:          # 低于均值的直接跳过，噪声
            break
        if any(abs(i - j) <= 1 for j in picked):
            continue
        picked.append(i)
        if len(picked) >= top * 2:
            break

    out: list[Level] = []
    for i in picked:
        lo, hi, v = prof[i]
        # 向两侧扩张到低于均值的位置，形成一条带
        a, b = i, i
        while a - 1 >= 0 and prof[a - 1][2] >= avg * 0.85:
            a -= 1
        while b + 1 < len(prof) and prof[b + 1][2] >= avg * 0.85:
            b += 1
        lo2, hi2 = prof[a][0], prof[b][1]
        zone_vol = sum(prof[k][2] for k in range(a, b + 1))
        mid = (lo2 + hi2) / 2.0
        if price and abs(mid / price - 1.0) * 100 > max_dist_pct:
            continue
        kind = "resistance" if (price and mid > price) else "support"
        out.append(Level(mid, kind, min(0.85, zone_vol / total * 5.0), "成交密集区",
                         f"{lo2:,.2f}-{hi2:,.2f}"))
    out.sort(key=lambda l: -l.weight)
    return out[:top]


def fib_levels(high: float, low: float,
               ratios: Iterable[float] = (0.236, 0.382, 0.5, 0.618, 0.786)):
    """从 high 到 low 的回撤位。"""
    rng = high - low
    return [(r, high - rng * r) for r in ratios]


def orderbook_walls(depth: dict, price: float, pct: float = 1.5,
                    contract_size: float = 0.001, min_multiple: float = 2.0,
                    top_each: int = 3, min_coin: float = 1.0) -> list[Level]:
    """盘口挂单墙。

    只保留明显大于同侧中位数的挂单（min_multiple 倍），并且每侧最多取 top_each 条。
    盘口挂单变化极快，权重也压低到 0.5 以下——它是"当下阻力"，不是结构位。
    """
    if not depth or not price:
        return []
    out: list[Level] = []
    for side, kind in (("asks", "resistance"), ("bids", "support")):
        rows = [(p, q) for p, q in (depth.get(side) or [])
                if abs(p / price - 1.0) * 100 <= pct]
        rows = [(p, q) for p, q in rows if q * contract_size >= min_coin]
        if not rows:
            continue
        sizes = sorted(q for _, q in rows)
        median = sizes[len(sizes) // 2]
        if median <= 0:
            continue
        for p, q in rows:
            if q < median * min_multiple:
                continue
            w = min(0.5, 0.22 + 0.28 * min(2.0, q / (median * min_multiple)))
            out.append(Level(p, kind, w, "盘口挂单",
                             f"{q * contract_size:,.2f} 币 / {q:,.0f} 张"))
    out.sort(key=lambda l: -l.weight)
    # 每侧只留最强的几条
    kept: list[Level] = []
    for kind in ("resistance", "support"):
        kept += [l for l in out if l.kind == kind][:top_each]
    return kept


def round_number_levels(price: float, span_pct: float = 1.2,
                        steps: tuple[float, ...] = (500, 1000, 2000, 5000)) -> list[Level]:
    """整数关口。价格越"整"，心理意义越强。"""
    if not price:
        return []
    out: list[Level] = []
    for step in steps:
        if step / price > 0.04:          # 步长相对价格太大就没意义（比如 PEPE）
            continue
        base = (price // step) * step
        for k in range(-2, 3):
            lv = base + k * step
            if lv <= 0:
                continue
            dist = abs(lv / price - 1.0) * 100
            if dist > span_pct:
                continue
            w = {500: 0.28, 1000: 0.40, 2000: 0.55, 5000: 0.65}.get(int(step), 0.28)
            out.append(Level(lv, "resistance" if lv > price else "support",
                             w, "整数关口", f"间隔 {step:,.0f}"))
    out.sort(key=lambda l: -l.weight)
    return out


def moving_average_levels(price: float, mas: dict[str, float | None],
                          span_pct: float = 3.0) -> list[Level]:
    weights = {"SMA20": 0.60, "SMA50": 0.75, "SMA200": 0.90, "EMA12": 0.45, "EMA26": 0.50,
               "MA20(15m)": 0.45, "MA50(1h)": 0.55, "VWAP": 0.50}
    out: list[Level] = []
    for name, v in mas.items():
        if not v or not price:
            continue
        if abs(v / price - 1.0) * 100 > span_pct:
            continue
        out.append(Level(v, "resistance" if v > price else "support",
                         weights.get(name, 0.4), name, "均线/均价"))
    out.sort(key=lambda l: -l.weight)
    return out


def merge_levels(price: float, levels: list[Level], cluster_pct: float = 0.18) -> list[Level]:
    """合并距离相近的价位（相对差 < cluster_pct%），权重相加。"""
    items = sorted([l for l in levels if l.price and l.price > 0], key=lambda l: l.price)
    merged: list[Level] = []
    for lv in items:
        if merged and abs(lv.price - merged[-1].price) / price * 100 < cluster_pct:
            prev = merged[-1]
            wsum = prev.weight + lv.weight
            px = (prev.price * prev.weight + lv.price * lv.weight) / wsum if wsum else prev.price
            src = prev.source if lv.source in prev.source else f"{prev.source}+{lv.source}"
            merged[-1] = Level(px, prev.kind, min(1.0, wsum), src, lv.detail or prev.detail)
        else:
            merged.append(lv)
    return merged


def price_ladder(price: float, levels: list[Level], *,
                 min_dist: float, max_dist: float,
                 max_each: int = 5, direction: str = "up") -> list[dict]:
    """按距离窗口筛选 + 合并，产出有序的一侧价位阶梯。

    min_dist / max_dist 是绝对价格距离：太近的（噪声）和太远的（不现实）都剔除。
    """
    if not price:
        return []
    if direction == "up":
        cand = [l for l in levels if price + min_dist < l.price < price + max_dist]
        cand.sort(key=lambda l: (-l.weight, l.price))
    else:
        cand = [l for l in levels if price - max_dist < l.price < price - min_dist]
        cand.sort(key=lambda l: (-l.weight, -l.price))
    merged = merge_levels(price, cand, cluster_pct=max(0.12, min_dist / price * 100 * 0.5))
    if direction == "up":
        merged.sort(key=lambda l: (-l.weight, l.price))
    else:
        merged.sort(key=lambda l: (-l.weight, -l.price))
    return [l.to_dict() for l in merged[:max_each]]


def atr_stop_buffer(atr_value: float | None, mult: float = 0.6) -> float:
    return (atr_value or 0.0) * mult
