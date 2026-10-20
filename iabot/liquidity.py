"""流动性体检：24h 成交额 + 买卖价差。

回测里滑点是固定 2bps，那个假设在小币上完全不成立——挂单可能根本进不去，
出场时也可能滑出去好几倍。信号再准，在没有流动性的合约上也做不成。

这里做两件事：
1. 给每个计划挂一份"流动性体检"，不达标就明确警告（默认只警告）。
2. 可选地（analysis.liquidity.enforce）直接把不达标的合约降级为观望，
   不让实盘碰它。

阈值都放在 config 里，默认值偏保守：
- min_turnover_24h 500 万 U：24h 成交额低于这个的合约，进出都容易滑。
- max_spread_bps 20：买卖价差超过 0.2% 时，一手小单就吃掉大半成本。
"""

from __future__ import annotations

from .config import DEFAULT_MAX_SPREAD_BPS, DEFAULT_MIN_TURNOVER
from .market import Snapshot


def spread_bps(snap: Snapshot) -> float | None:
    """买卖价差（bps）。没有盘口数据时返回 None。"""
    bid, ask = snap.best_bid, snap.best_ask
    if not bid or not ask or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


def assess(snap: Snapshot, cfg: dict) -> dict:
    """做一次流动性体检，返回体检结果（可直接挂到 plan.market 上）。"""
    lc = ((cfg.get("analysis") or {}).get("liquidity") or {})
    min_turnover = float(lc.get("min_turnover_24h", DEFAULT_MIN_TURNOVER))
    max_spread = float(lc.get("max_spread_bps", DEFAULT_MAX_SPREAD_BPS))
    enforce = bool(lc.get("enforce"))

    # 成交额：字段缺失（接口没给/测试夹具没填）不算不达标——
    # "没数据"和"没流动性"是两回事，误报会把警告撑爆。
    raw_turnover = snap.turnover_24h
    turnover: float | None = (float(raw_turnover) if raw_turnover is not None
                              else None)
    sbps = spread_bps(snap)

    reasons: list[str] = []
    if min_turnover > 0 and turnover is not None and turnover < min_turnover:
        reasons.append(
            f"24h 成交额只有 {turnover/1e6:.2f}M U（低于 {min_turnover/1e6:.1f}M），"
            f"进出都容易滑")
    if max_spread > 0 and sbps is not None and sbps > max_spread:
        reasons.append(
            f"买卖价差 {sbps:.1f}bps（高于 {max_spread:.0f}bps），小单成本会被吃掉")
    return {
        "turnover_24h": turnover,
        "spread_bps": (round(sbps, 2) if sbps is not None else None),
        "min_turnover_24h": min_turnover,
        "max_spread_bps": max_spread,
        "enforce": enforce,
        "ok": not reasons,
        "reasons": reasons,
    }
