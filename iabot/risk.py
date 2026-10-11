"""风险度量：强平价估算与止损有效性校验。

HTX 的公开只读接口不暴露维持保证金率（margin tier）档位——那需要 API Key，
而本项目坚持"只读公开行情、不需要 Key"。所以这里用保守的行业近似值估算强平价：

    多仓：Liq ≈ 入场 × (1 − 1/杠杆 + 维持保证金率)
    空仓：Liq ≈ 入场 × (1 + 1/杠杆 − 维持保证金率)

维持保证金率取 0.5%（USDT 本位永续主流币高杠杆档的常见量级）。
这个近似偏保守（算出的强平价比实际更近一点），宁可多提醒，不可漏警。
"""

from __future__ import annotations

import math

# 维持保证金率近似值（0.5%）。宁可偏保守。
DEFAULT_MMR = 0.005

# 杠杆高于这个值时，1/杠杆 已经小于 MMR，估算公式开始不可靠
MAX_RELIABLE_LEVERAGE = 125.0


def liquidation_price(entry: float, leverage: float, direction: str,
                      mmr: float = DEFAULT_MMR) -> float | None:
    """近似强平价。杠杆 <= 1（无杠杆）时没有强平概念，返回 None。"""
    if not entry or entry <= 0 or leverage <= 1:
        return None
    lev = min(leverage, MAX_RELIABLE_LEVERAGE)
    if direction == "long":
        return entry * (1.0 - 1.0 / lev + mmr)
    if direction == "short":
        return entry * (1.0 + 1.0 / lev - mmr)
    return None


def stop_is_effective(entry: float, stop: float, leverage: float, direction: str,
                      mmr: float = DEFAULT_MMR) -> tuple[bool, float | None]:
    """判断止损是否真的能执行。

    返回 (有效?, 强平价)。多仓若强平价高于止损（更靠近入场），说明价格会先
    被强平、再也走不到止损位——止损形同虚设，必须告警。空仓反之。
    """
    liq = liquidation_price(entry, leverage, direction, mmr)
    if liq is None or not stop:
        return (True, liq)
    if direction == "long":
        effective = liq < stop          # 强平价在止损下方才轮得到止损
    else:
        effective = liq > stop          # 强平价在止损上方才轮得到止损
    return (effective, liq)


def safe_max_leverage(stop_pct: float, direction: str = "long",
                      mmr: float = DEFAULT_MMR) -> float:
    """给定止损幅度，反推"止损能先生效"的最大杠杆。

    推导（多仓）：强平价 entry(1 − 1/lev + mmr) 必须低于止损 entry(1 − s)：
        1 − 1/lev + mmr < 1 − s   →   1/lev > s + mmr   →   lev < 1/(s + mmr)
    空仓对称，结果相同。

    注意是 (s + mmr) 不是 (s − mmr)：维持保证金率会让强平价比纯 1/杠杆 更靠近
    入场，所以止损要多覆盖这部分才安全。
    """
    if not stop_pct or stop_pct <= 0:
        return 1.0
    s = stop_pct / 100.0
    denom = s + mmr
    if denom <= 0:
        return 1.0
    # 需 1/lev > s+mmr（严格大于）。严格小于 X 的最大整数是 ceil(X)-1：
    # 若 1/denom 恰好是整数（如 0.5% 止损 → 100），该整数正好在边界不安全，取 99。
    lev = math.ceil(1.0 / denom) - 1
    return float(max(1, min(int(MAX_RELIABLE_LEVERAGE), lev)))