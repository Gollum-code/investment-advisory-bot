"""组合级回测：把多个标的的交易放到同一条时间轴上，按资金和并发上限模拟。

为什么单笔期望不够：

- backtest 是**按币独立**算的，每笔都假设有满额资金可用。真实账户只有一个，
  同时开了 8 个仓，第 9 个信号就没钱做了。
- 山寨币相关性极高，"同时 8 个多头"实际约等于"1 个 8 倍杠杆的多头"。
  单笔期望再正，组合回撤也可能远超预期。
- 单笔期望假设每笔都按固定风险金额下单；组合的权益随盈亏变化，风险金额
  也应随之变化（复利/缩水），这条也只有组合级模拟才看得到。

本模块做的是能确定性复现的那部分：并发上限 + 资金约束 + 权益复利。
相关性敞口限制在 signals/risk 层另做，这里先把"钱够不够"算清楚。
"""

from __future__ import annotations

from .backtest import _bootstrap_mean_ci


def _max_drawdown(curve: list[float]) -> tuple[float, float]:
    """返回 (最大回撤金额, 最大回撤占峰值比例)。curve 是权益（U）。"""
    peak = None
    worst_abs = 0.0
    worst_pct = 0.0
    for v in curve:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = peak - v
            if dd > worst_abs:
                worst_abs = dd
                worst_pct = dd / peak
    return worst_abs, worst_pct


def portfolio_backtest(trade_logs: list[dict], *, equity: float = 450.0,
                       risk_pct: float = 2.0, max_concurrent: int = 5,
                       compound: bool = True) -> dict:
    """按时间顺序模拟一个账户做这批交易。

    trade_logs: 各标的 backtest().trade_log 合并后的列表。每笔需要
                entry_ts / exit_ts / r。
    max_concurrent: 同时最多持有几个仓位（<=0 表示不限制）。
    compound: True = 每笔风险按当前权益的 risk_pct 计算（复利）；False = 固定
              用初始权益算（与单币回测的口径一致，便于对照）。

    并发已满时**跳过**该入场（保守：不缩小仓位，直接不做）。
    """
    logs = [t for t in trade_logs if t.get("entry_ts") is not None]
    logs.sort(key=lambda t: (t["entry_ts"], t.get("symbol") or ""))

    cash = float(equity)
    open_positions: list[dict] = []
    curve: list[float] = [cash]
    taken_rs: list[float] = []
    skipped_capacity = 0
    skipped_no_exit = 0

    def settle(until_ts: float) -> None:
        nonlocal cash
        still = []
        for pos in open_positions:
            if pos["exit_ts"] is not None and pos["exit_ts"] <= until_ts:
                cash += pos["r"] * pos["risk_amount"]
                curve.append(cash)
            else:
                still.append(pos)
        open_positions[:] = still

    for t in logs:
        settle(t["entry_ts"])
        if t.get("exit_ts") is None:
            skipped_no_exit += 1
            continue
        if max_concurrent and len(open_positions) >= max_concurrent:
            skipped_capacity += 1
            continue
        base_equity = cash if compound else float(equity)
        r = float(t.get("r") or 0.0)
        open_positions.append({
            "exit_ts": t["exit_ts"],
            "r": r,
            "symbol": t.get("symbol") or "",
            "risk_amount": base_equity * risk_pct / 100.0,
        })
        taken_rs.append(r)

    if open_positions:
        settle(max((p["exit_ts"] or 0) for p in open_positions))

    dd_abs, dd_pct = _max_drawdown(curve)
    ci = _bootstrap_mean_ci(taken_rs)
    total_return = (cash / equity - 1.0) if equity else 0.0
    return {
        "signals": len(logs),
        "taken": len(taken_rs),
        "skipped_capacity": skipped_capacity,
        "skipped_no_exit": skipped_no_exit,
        "max_concurrent": max_concurrent,
        "equity_start": round(equity, 2),
        "equity_end": round(cash, 2),
        "total_return_pct": round(total_return * 100, 2),
        "max_drawdown_usdt": round(dd_abs, 2),
        "max_drawdown_pct": round(dd_pct * 100, 2),
        "expectancy_r": round(
            (sum(taken_rs) / len(taken_rs)) if taken_rs else 0.0, 4),
        "expectancy_ci": ([round(x, 4) for x in ci] if ci else None),
        "per_trade_r_sum": round(sum(taken_rs), 3),
        "equity_curve": [round(x, 2) for x in curve],
    }
