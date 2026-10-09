"""技术指标：只用标准库实现，不依赖 numpy / pandas。

所有函数输入都是等长的 list[float]（按时间正序，最后一个是当前值），
输出也是等长 list，前几根算不出来的位置用 None 占位，方便直接对齐画图。
"""

from __future__ import annotations

import math

Num = float | None


def sma(values: list[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if n <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: list[float], n: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if n <= 0 or not values:
        return out
    k = 2.0 / (n + 1.0)
    prev: float | None = None
    for i, v in enumerate(values):
        if prev is None:
            if i >= n - 1:
                prev = sum(values[i - n + 1:i + 1]) / n
                out[i] = prev
        else:
            prev = v * k + prev * (1 - k)
            out[i] = prev
    return out


def rsi(values: list[float], n: int = 14) -> list[Num]:
    """Wilder RSI（用 EMA 平滑，和主流行情软件一致）。"""
    out: list[Num] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (dif, dea, hist)。"""
    ef, es = ema(values, fast), ema(values, slow)
    dif: list[Num] = [
        (a - b) if (a is not None and b is not None) else None for a, b in zip(ef, es)
    ]
    idx = [i for i, v in enumerate(dif) if v is not None]
    dea: list[Num] = [None] * len(values)
    if idx:
        seq = [dif[i] for i in idx]
        sm = ema(seq, signal)  # type: ignore[arg-type]
        for j, i in enumerate(idx):
            dea[i] = sm[j]
    hist: list[Num] = [
        (a - b) if (a is not None and b is not None) else None for a, b in zip(dif, dea)
    ]
    return dif, dea, hist


def true_range(high: list[float], low: list[float], close: list[float]) -> list[Num]:
    out: list[Num] = [None] * len(close)
    for i in range(len(close)):
        if i == 0:
            out[i] = high[i] - low[i]
        else:
            pc = close[i - 1]
            out[i] = max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc))
    return out


def atr(high: list[float], low: list[float], close: list[float], n: int = 14) -> list[Num]:
    tr = true_range(high, low, close)
    out: list[Num] = [None] * len(close)
    if len(close) <= n:
        return out
    seed = sum(tr[1:n + 1]) / n  # type: ignore[arg-type]
    out[n] = seed
    for i in range(n + 1, len(close)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n  # type: ignore[operator]
    return out


def bollinger(values: list[float], n: int = 20, k: float = 2.0):
    mid = sma(values, n)
    up: list[Num] = [None] * len(values)
    dn: list[Num] = [None] * len(values)
    for i in range(len(values)):
        m = mid[i]
        if m is None:
            continue
        window = values[i - n + 1:i + 1]
        var = sum((x - m) ** 2 for x in window) / n
        sd = math.sqrt(var)
        up[i] = m + k * sd
        dn[i] = m - k * sd
    return up, mid, dn


def vwap(high: list[float], low: list[float], close: list[float],
         volume: list[float], start: int = 0) -> list[Num]:
    """成交量加权均价，从 start 开始累计（做日内 VWAP 时把 start 设在当日首根）。"""
    out: list[Num] = [None] * len(close)
    pv = 0.0
    vv = 0.0
    for i in range(start, len(close)):
        tp = (high[i] + low[i] + close[i]) / 3.0
        pv += tp * volume[i]
        vv += volume[i]
        if vv > 0:
            out[i] = pv / vv
    return out


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def realized_vol_pct(closes: list[float], n: int, periods_per_year: float) -> float | None:
    """年化已实现波动率(%)。"""
    if len(closes) < n + 1:
        return None
    rets: list[float] = []
    for i in range(len(closes) - n, len(closes)):
        if closes[i - 1] > 0:
            rets.append(closes[i] / closes[i - 1] - 1.0)
    if len(rets) < 2:
        return None
    return stdev(rets) * math.sqrt(periods_per_year) * 100.0


def adx(high: list[float], low: list[float], close: list[float], n: int = 14) -> list[Num]:
    """平均趋向指数：>25 趋势市，<20 震荡市。"""
    ln = len(close)
    out: list[Num] = [None] * ln
    if ln < 2 * n + 1:
        return out
    plus_dm = [0.0] * ln
    minus_dm = [0.0] * ln
    tr = true_range(high, low, close)
    for i in range(1, ln):
        up_move = high[i] - high[i - 1]
        dn_move = low[i - 1] - low[i]
        plus_dm[i] = up_move if (up_move > dn_move and up_move > 0) else 0.0
        minus_dm[i] = dn_move if (dn_move > up_move and dn_move > 0) else 0.0

    def _wilder(seq: list[float]) -> list[Num]:
        r: list[Num] = [None] * ln
        s = sum(seq[1:n + 1])
        r[n] = s
        for i in range(n + 1, ln):
            r[i] = r[i - 1] - (r[i - 1] / n) + seq[i]  # type: ignore[operator]
        return r

    atr_s = _wilder([v or 0.0 for v in tr])
    pdm_s = _wilder(plus_dm)
    mdm_s = _wilder(minus_dm)
    dxs: list[Num] = [None] * ln
    for i in range(n, ln):
        a = atr_s[i]
        if not a or a <= 0 or pdm_s[i] is None or mdm_s[i] is None:
            continue
        pdi = 100.0 * pdm_s[i] / a  # type: ignore[operator]
        mdi = 100.0 * mdm_s[i] / a  # type: ignore[operator]
        denom = pdi + mdi
        dxs[i] = 0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom
    # ADX = DX 的 Wilder 平滑
    first = next((i for i in range(ln) if dxs[i] is not None), None)
    if first is None:
        return out
    start = first + n - 1
    if start >= ln:
        return out
    seeded = [dxs[i] for i in range(first, start + 1)]
    if any(v is None for v in seeded):
        return out
    prev = sum(seeded) / len(seeded)  # type: ignore[arg-type]
    out[start] = prev
    for i in range(start + 1, ln):
        if dxs[i] is None:
            continue
        prev = (prev * (n - 1) + dxs[i]) / n  # type: ignore[operator]
        out[i] = prev
    return out


def last(seq: list[Num]) -> Num:
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def pct_change(values: list[float], bars: int) -> float | None:
    if len(values) <= bars or values[-1 - bars] == 0:
        return None
    return (values[-1] / values[-1 - bars] - 1.0) * 100.0
