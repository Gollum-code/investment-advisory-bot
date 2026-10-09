"""历史回测：用真实 K 线回放策略，统计"如果当时按规则做"的胜率与期望值。

与 results 回填的区别：
- outcomes（阶段1）：只判定已经发生的信号。
- backtest（本模块）：在历史 K 线上滑动窗口，每隔 step 根构造一个"当时的快照"，
  走真实的 build_plan + 评分 + 两道闸门，再用后续 K 线判定那个计划的结果。
  这样能在几秒内用几个月历史回答"这套规则的胜率到底多少、期望值多少"。

注意：不重放历史数据到真实下单；只做"规则在这段历史上表现如何"的统计。
无未来函数：每个建仓点只用该点及之前的数据构造快照，判定只看之后的数据。
"""

from __future__ import annotations

from dataclasses import dataclass

from .market import Candle, period_seconds, Snapshot
from .outcomes import evaluate_outcome
from .signals import build_plan


@dataclass
class BacktestResult:
    symbol: str
    mode: str
    bars: int
    trades: int                 # 过了两道闸门、给出明确方向的信号数
    wins: int
    losses: int
    timeouts: int               # 窗口内既没到目标也没碰止损
    total_r: float              # 所有已了结交易的 R 之和（用满仓单风险）
    avg_win_r: float
    avg_loss_r: float
    win_rate: float
    expectancy_r: float         # 每笔期望 R = total_r / trades
    max_mfe: float
    max_mae: float
    equity_curve: list[float]   # 以 1R 为单位累加（实际风险由 sizing 控制）

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "mode": self.mode, "bars": self.bars,
            "trades": self.trades, "wins": self.wins, "losses": self.losses,
            "timeouts": self.timeouts,
            "win_rate": round(self.win_rate, 4),
            "expectancy_r": round(self.expectancy_r, 3),
            "avg_win_r": round(self.avg_win_r, 3),
            "avg_loss_r": round(self.avg_loss_r, 3),
            "total_r": round(self.total_r, 2),
            "max_mfe": round(self.max_mfe, 3),
            "max_mae": round(self.max_mae, 3),
            "equity_curve": [round(x, 3) for x in self.equity_curve],
        }


def _slice_snapshot(base: Snapshot, klines: dict[str, list[Candle]],
                    idx_by_period: dict[str, int], price: float) -> Snapshot:
    """构造"截至 idx_by_period 那一刻"的快照：只含各周期 idx 及之前的 K 线。"""
    snap = Snapshot(
        symbol=base.symbol,
        price=price,
        index_price=base.index_price,
        contract_size=base.contract_size,
        funding_rate=base.funding_rate,
        open_interest=base.open_interest,
    )
    for period, idx in idx_by_period.items():
        rows = klines.get(period) or []
        snap.klines[period] = rows[: idx + 1]
    return snap


def backtest(base: Snapshot, cfg: dict, *, mode: str = "intraday",
             step: int = 10, warmup: int = 60, max_points: int = 400) -> BacktestResult:
    """在 base 快照的历史 K 线上滑动回测。

    base: 已经 fetch 好的完整快照（含各周期 K 线），由调用方从接口拿。
    step:  每隔多少根主周期 K 线构造一个建仓点（越小越密、越慢）。
    warmup: 前多少根跳过，保证指标有足够数据。
    max_points: 最多构造多少个建仓点（防跑太久）。
    """
    m_period = "15min" if mode == "intraday" else "1day"
    main = base.klines.get(m_period) or []
    n = len(main)
    if n < warmup + step + 30:
        return BacktestResult(base.symbol, mode, n, 0, 0, 0, n, 0.0, 0.0, 0.0,
                              0.0, 0.0, 0.0, 0.0, [])

    # 各周期与主周期对齐：找每个主周期 ts 之前、<=它 的最后一根
    idx_maps: dict[str, list[int]] = {}
    for period, rows in base.klines.items():
        ts_list = [c.ts for c in rows]
        idx_maps[period] = ts_list

    def bisect_idx(rows_ts: list[int], ts: int) -> int:
        lo, hi = 0, len(rows_ts) - 1
        ans = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if rows_ts[mid] <= ts:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    wins = losses = timeouts = 0
    total_r = 0.0
    win_rs: list[float] = []
    loss_rs: list[float] = []
    mfes: list[float] = []
    maes: list[float] = []
    curve: list[float] = []
    cum_r = 0.0
    trades = 0

    start = warmup
    end = n - 5  # 留出后续判定空间
    points = list(range(start, end, max(1, step)))[:max_points]

    for i in points:
        ts = main[i].ts
        idx_by_period = {}
        for period, rows_ts in idx_maps.items():
            # 数据不足的周期退化为空（不跳过整点）：各因子对数据不足都有
            # "降权为 0 分"的保护，早期点只是少几个因子而已。
            j = bisect_idx(rows_ts, ts)
            idx_by_period[period] = j if j >= 0 else -1

        price = main[i].close
        snap = _slice_snapshot(base, base.klines, idx_by_period, price)
        # 判定用"之后"的 K 线：从 i+1 开始
        future = main[i + 1:]

        plan = build_plan(snap, cfg, mode=mode)
        if plan.direction not in ("long", "short"):
            continue  # 观望或被闸门拦下 -> 不算一笔交易
        # 关键：把"信号时间"改成该建仓点的历史时间 ts，否则 evaluate_outcome
        # 会把所有历史 K 线当成"信号之前"而跳过，永远判不出结果
        plan.generated_at = float(ts)
        trades += 1

        res = evaluate_outcome(plan.to_dict(), future)
        if not res:
            # 后续 K 线不足以判定窗口，跳过（不算交易）
            trades -= 1
            continue
        outcome = res["outcome"]
        mfes.append(res["mfe_pct"])
        maes.append(res["mae_pct"])
        if outcome.startswith("tp"):
            r = res["rr"] if res["rr"] is not None else 1.0
            wins += 1
            total_r += r
            cum_r += r
            win_rs.append(r)
        elif outcome == "stopped":
            losses += 1
            total_r += -1.0
            cum_r += -1.0
            loss_rs.append(-1.0)
        else:  # timeout
            timeouts += 1
        curve.append(cum_r)

    resolved = wins + losses
    win_rate = wins / resolved if resolved else 0.0
    expectancy = total_r / trades if trades else 0.0
    return BacktestResult(
        symbol=base.symbol, mode=mode, bars=n, trades=trades,
        wins=wins, losses=losses, timeouts=timeouts,
        total_r=total_r,
        avg_win_r=(sum(win_rs) / len(win_rs)) if win_rs else 0.0,
        avg_loss_r=(sum(loss_rs) / len(loss_rs)) if loss_rs else 0.0,
        win_rate=win_rate, expectancy_r=expectancy,
        max_mfe=(max(mfes) if mfes else 0.0),
        max_mae=(min(maes) if maes else 0.0),
        equity_curve=curve,
    )