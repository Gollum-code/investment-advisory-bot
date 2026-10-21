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

import math

from .backtest import _bootstrap_mean_ci
from .market import Snapshot


def _daily_returns(snap: Snapshot, window: int) -> dict[float, float]:
    """日线对数收益，按 ts 索引。取最近 window 根日线。"""
    rows = snap.klines.get("1day") or []
    if len(rows) < 3:
        return {}
    out: dict[float, float] = {}
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1].close, rows[i].close
        if prev <= 0 or cur <= 0:
            continue
        out[float(rows[i].ts)] = math.log(cur / prev)
    if len(out) > window:
        keys = sorted(out)[-window:]
        out = {k: out[k] for k in keys}
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def cluster_symbols(snapshots: dict[str, Snapshot], *, threshold: float = 0.7,
                    window: int = 90) -> dict[str, int]:
    """按日线收益相关性把标的聚成簇。

    用并查集：相关性 >= threshold 的两个标的合并，最后连通分量就是簇。
    阈值 0.7 大致对应"大部分时间同涨同跌"——山寨币之间普遍 0.8~0.95，
    BTC 与主流币之间 0.6~0.85，BTC 与某些山寨 0.4~0.6。
    """
    rets = {sym: _daily_returns(snap, window) for sym, snap in snapshots.items()}
    syms = sorted(rets)
    parent = {s: s for s in syms}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            ra, rb = rets[a], rets[b]
            common = sorted(set(ra) & set(rb))
            if len(common) < 20:
                continue
            xs = [ra[t] for t in common]
            ys = [rb[t] for t in common]
            if _pearson(xs, ys) >= threshold:
                union(a, b)

    groups: dict[str, int] = {}
    out: dict[str, int] = {}
    for s in syms:
        root = find(s)
        if root not in groups:
            groups[root] = len(groups)
        out[s] = groups[root]
    return out


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
                       compound: bool = True, max_heat_pct: float | None = None,
                       clusters: dict[str, int] | None = None,
                       max_per_cluster: int | None = None,
                       direction_mults: dict[str, float] | None = None) -> dict:
    """按时间顺序模拟一个账户做这批交易。

    trade_logs: 各标的 backtest().trade_log 合并后的列表。每笔需要
                entry_ts / exit_ts / r。
    max_concurrent: 同时最多持有几个仓位（<=0 表示不限制）。
    compound: True = 每笔风险按当前权益的 risk_pct 计算（复利）；False = 固定
              用初始权益算（与单币回测的口径一致，便于对照）。
    max_heat_pct: 组合热度上限（所有未平仓仓位的风险金额之和占权益的 %）。
                 比"最多几个仓"更准：不同币的止损宽度不同，风险金额并不相等。
    clusters / max_per_cluster: 相关性簇限制。同一个簇（如"山寨币"）里最多
                 同时持有几个仓位——它们一起涨跌，五个仓约等于一个 5 倍仓位。

    触发任一上限时**跳过**该入场（保守：不缩小仓位，直接不做）。
    """
    logs = [t for t in trade_logs if t.get("entry_ts") is not None]
    logs.sort(key=lambda t: (t["entry_ts"], t.get("symbol") or ""))

    cash = float(equity)
    open_positions: list[dict] = []
    curve: list[float] = [cash]
    taken_rs: list[float] = []
    skipped_rs: list[float] = []          # 被任何上限挡掉的单子，用来对比质量
    skipped_by: dict[str, list[float]] = {"capacity": [], "heat": [],
                                          "cluster": []}
    skipped_capacity = 0
    skipped_heat = 0
    skipped_cluster = 0
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
            skipped_by["capacity"].append(float(t.get("r") or 0.0))
            continue
        base_equity = cash if compound else float(equity)
        # 多空不对称：按方向给不同的单笔风险（R 保持不变，改的是下注金额）。
        mult = 1.0
        if direction_mults:
            mult = float(direction_mults.get(t.get("direction") or "", 1.0) or 1.0)
        risk_amount = base_equity * risk_pct / 100.0 * mult
        # 热度：所有未平仓仓位的风险金额之和不能超过权益的这个比例
        if max_heat_pct:
            heat = sum(p["risk_amount"] for p in open_positions)
            if (heat + risk_amount) > base_equity * max_heat_pct / 100.0:
                skipped_heat += 1
                skipped_by["heat"].append(float(t.get("r") or 0.0))
                continue
        # 相关性簇：同一簇里最多持有几个
        if clusters and max_per_cluster:
            sym = t.get("symbol") or ""
            cid = clusters.get(sym)
            if cid is not None:
                same = sum(1 for p in open_positions
                           if clusters.get(p["symbol"]) == cid)
                if same >= max_per_cluster:
                    skipped_cluster += 1
                    skipped_by["cluster"].append(float(t.get("r") or 0.0))
                    continue
        r = float(t.get("r") or 0.0)
        open_positions.append({
            "exit_ts": t["exit_ts"],
            "r": r,
            "symbol": t.get("symbol") or "",
            "risk_amount": risk_amount,
        })
        taken_rs.append(r)

    if open_positions:
        settle(max((p["exit_ts"] or 0) for p in open_positions))

    dd_abs, dd_pct = _max_drawdown(curve)
    ci = _bootstrap_mean_ci(taken_rs)
    total_return = (cash / equity - 1.0) if equity else 0.0
    skipped_rs = (skipped_by["capacity"] + skipped_by["heat"]
                  + skipped_by["cluster"])

    def _mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 4) if xs else None

    return {
        "signals": len(logs),
        "taken": len(taken_rs),
        "skipped_capacity": skipped_capacity,
        "skipped_heat": skipped_heat,
        "skipped_cluster": skipped_cluster,
        "skipped_no_exit": skipped_no_exit,
        "max_concurrent": max_concurrent,
        "max_heat_pct": max_heat_pct,
        "max_per_cluster": max_per_cluster,
        "direction_mults": dict(direction_mults or {}),
        "equity_start": round(equity, 2),
        "equity_end": round(cash, 2),
        "total_return_pct": round(total_return * 100, 2),
        "max_drawdown_usdt": round(dd_abs, 2),
        "max_drawdown_pct": round(dd_pct * 100, 2),
        "expectancy_r": round(
            (sum(taken_rs) / len(taken_rs)) if taken_rs else 0.0, 4),
        "expectancy_ci": ([round(x, 4) for x in ci] if ci else None),
        "per_trade_r_sum": round(sum(taken_rs), 3),
        # 被挡掉的单子：如果它们的期望和做上的差不多，说明上限主要是"安全"
        # 而不是"省钱"；如果明显更好，就说明限太紧了。
        "skipped_expectancy_r": _mean(skipped_rs),
        "taken_expectancy_r": _mean(taken_rs),
        "skipped_breakdown": {
            k: {"count": len(v), "expectancy_r": _mean(v)}
            for k, v in skipped_by.items() if v
        },
        "equity_curve": [round(x, 2) for x in curve],
    }
