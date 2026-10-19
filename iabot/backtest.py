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

# 成本模型默认值（bps = 万分之一）。一次往返 = 开仓一次 + 平仓一次。
DEFAULT_COSTS = {"taker_fee_bps": 5.0, "slippage_bps": 2.0, "funding_rate": 0.0}


@dataclass
class BacktestResult:
    symbol: str
    mode: str
    bars: int
    trades: int                 # 过了两道闸门、给出明确方向的信号数
    wins: int
    losses: int
    timeouts: int               # 窗口内既没到目标也没碰止损
    no_fills: int               # 挂单从未成交（价格没进过入场区），不计胜负
    total_r: float              # 所有已了结交易的 R 之和（用满仓单风险）
    avg_win_r: float
    avg_loss_r: float
    win_rate: float
    expectancy_r: float         # 每笔期望 R = total_r / trades（已扣成本）
    avg_cost_r: float           # 每笔平均成本（手续费+滑点+资金费，折算成 R）
    avg_fee_r: float
    avg_slippage_r: float
    avg_funding_r: float
    avg_holding_h: float        # 平均持仓时长（小时）
    max_mfe: float
    max_mae: float
    equity_curve: list[float]   # 以 1R 为单位累加（实际风险由 sizing 控制）
    equity_usdt: float          # 初始权益（用于把 R 折算成金额）
    risk_pct: float             # 单笔风险占权益 %
    risk_amount_usdt: float     # 单笔风险金额
    equity_curve_usdt: list[float]  # 累计权益曲线（按单笔风险金额折算成 U）
    insufficient_data: bool = False  # True = 主周期 K 线不够，一笔都没跑
    missing_periods: tuple = ()       # 快照里缺失/过短的周期（抓取失败导致）

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "mode": self.mode, "bars": self.bars,
            "trades": self.trades, "wins": self.wins, "losses": self.losses,
            "timeouts": self.timeouts, "no_fills": self.no_fills,
            "insufficient_data": self.insufficient_data,
            "missing_periods": list(self.missing_periods),
            "win_rate": round(self.win_rate, 4),
            "expectancy_r": round(self.expectancy_r, 3),
            "avg_win_r": round(self.avg_win_r, 3),
            "avg_loss_r": round(self.avg_loss_r, 3),
            "avg_cost_r": round(self.avg_cost_r, 3),
            "avg_fee_r": round(self.avg_fee_r, 4),
            "avg_slippage_r": round(self.avg_slippage_r, 4),
            "avg_funding_r": round(self.avg_funding_r, 4),
            "avg_holding_h": round(self.avg_holding_h, 2),
            "total_r": round(self.total_r, 2),
            "equity_usdt": round(self.equity_usdt, 2),
            "risk_amount_usdt": round(self.risk_amount_usdt, 2),
            "final_equity_usdt": round(self.equity_usdt + self.total_r * self.risk_amount_usdt, 2),
            "return_pct": round(self.total_r * self.risk_amount_usdt / self.equity_usdt * 100, 2)
            if self.equity_usdt else 0.0,
            "max_mfe": round(self.max_mfe, 3),
            "max_mae": round(self.max_mae, 3),
            "equity_curve": [round(x, 3) for x in self.equity_curve],
            "equity_curve_usdt": [round(x, 2) for x in self.equity_curve_usdt],
        }


def _upto(rows: list[dict], ts_field: str, ts: float,
          keep: int | None = None) -> list[dict]:
    """取"时间戳 <= ts"的那些行（输入已按时间正序）。

    回测要复现"当时能看到什么"。之前只切了 K 线，持仓量/大户持仓/资金费历史
    一律为空，于是 _f_oi、_f_elite、_f_funding 在回测里恒为 0 分——
    合起来 0.31 的权重从来没被验证过，回测的期望值并不代表 live 的表现。
    """
    out = []
    for r in rows:
        try:
            v = float(r.get(ts_field) or 0)
        except (TypeError, ValueError):
            continue
        if v / (1000.0 if v > 1e11 else 1.0) <= ts:
            out.append(r)
        else:
            break                     # 正序，遇到未来就停
    return out[-keep:] if keep else out


def _slice_snapshot(base: Snapshot, klines: dict[str, list[Candle]],
                    idx_by_period: dict[str, int], price: float,
                    ts: float | None = None) -> Snapshot:
    """构造"截至 idx_by_period 那一刻"的快照：只含各周期 idx 及之前的 K 线。

    ts 给出时，持仓量/大户/资金费历史也一并切到该时刻（无未来函数）。
    """
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
    if ts is not None:
        snap.oi_history = _upto(base.oi_history, "ts", ts, keep=90)
        snap.elite_position = _upto(base.elite_position, "ts", ts, keep=90)
        snap.funding_history = _upto(base.funding_history, "funding_time", ts, keep=90)
        if snap.oi_history:
            try:
                snap.open_interest = float(
                    snap.oi_history[-1].get("volume") or 0) or base.open_interest
            except (TypeError, ValueError):
                pass
    return snap


def _funding_rate_at(funding_hist: list[dict], ts: float) -> float:
    """取某个历史时刻生效的资金费率（取该时刻之前最近一条）。

    funding_hist 来自 MarketClient.funding_history，已按 funding_time 正序。
    没有历史数据时返回 0（表示不计资金费）。
    """
    if not funding_hist:
        return 0.0
    last = 0.0
    # funding_time 是毫秒，正序，最后一条=最新
    for row in funding_hist:
        ft = float(row.get("funding_time") or 0) / 1000.0
        if ft <= ts:
            last = float(row.get("funding_rate") or 0)
        else:
            break   # 已按正序，遇到未来就停
    return last


def backtest(base: Snapshot, cfg: dict, *, mode: str = "intraday",
             step: int = 10, warmup: int = 60, max_points: int = 400,
             apply_costs: bool | None = None, costs: dict | None = None,
             equity_usdt: float | None = None, risk_pct: float | None = None) -> BacktestResult:
    """在 base 快照的历史 K 线上滑动回测。

    base: 已经 fetch 好的完整快照（含各周期 K 线），由调用方从接口拿。
    step:  每隔多少根主周期 K 线构造一个建仓点（越小越密、越慢）。
    warmup: 前多少根跳过，保证指标有足够数据。
    apply_costs: 每笔交易扣除真实成本（手续费 + 滑点 + 按持仓时长的资金费）。
                 None=读 config 的 analysis.backtest（默认开启）。
    costs:  覆盖默认成本参数；资金费率从 base.funding_rate 取（作为近似）。
    equity_usdt / risk_pct: 用于把 R 折算成金额（默认取 cfg.account）。
    """
    acc = cfg.get("account") or {}
    eq = float(equity_usdt if equity_usdt is not None else (acc.get("equity_usdt") or 450))
    rp = float(risk_pct if risk_pct is not None else (acc.get("risk_pct_per_trade") or 2.0))
    risk_amount = eq * rp / 100.0

    # 成本参数读 config.analysis.backtest：README 与 btlog 都写了这个块，
    # 之前却没人把它传进来，改配置完全不生效，等于一段死配置。
    bc = (cfg.get("analysis") or {}).get("backtest") or {}
    if apply_costs is None:
        apply_costs = bool(bc.get("enabled", True))
    if not costs:
        costs = {k: bc[k] for k in ("taker_fee_bps", "slippage_bps") if k in bc}
    funding_on = bool(bc.get("funding", True))
    try:
        funding_period_h = float(bc.get("funding_period_sec") or 28800) / 3600.0
    except (TypeError, ValueError):
        funding_period_h = 8.0
    if funding_period_h <= 0:
        funding_period_h = 8.0

    C = dict(DEFAULT_COSTS)
    if costs:
        C.update({k: float(v) for k, v in costs.items() if k in C})
    m_period = "15min" if mode == "intraday" else "1day"
    m_period_sec = period_seconds(m_period)
    # 资金费率：优先用显式传入；否则优先用历史资金费率序列（按各建仓点时刻取），
    # 都没有才退回快照当前费率作近似。config 的 analysis.backtest.funding=false 可整体不计。
    fixed_funding = float(C.get("funding_rate") or 0.0)
    funding_hist = list(base.funding_history or []) if funding_on else []
    use_history = bool(funding_hist) and fixed_funding == 0.0
    fallback_funding = 0.0 if (use_history or not funding_on) else float(base.funding_rate or 0.0)
    main = base.klines.get(m_period) or []
    n = len(main)

    # 快照残缺检测：某个周期抓取失败时 Snapshot 只把那几根丢掉、记进 errors，
    # 回测会照常用剩下的数据跑出一个"看起来正常"的数字——这正是最危险的
    # 失败模式（15min 全空的币会跑 0 笔，1day 缺失则因子静默降级）。
    # 这里把缺失/过短的周期显式标出来；只要主周期或关键周期不全，就明确
    # 标 insufficient_data，不给出可被误读的期望值。
    missing: list[str] = []
    for period, rows in (base.klines or {}).items():
        if rows is not None and len(rows) < 2:
            missing.append(period)
    kline_errors = [e for e in (getattr(base, "errors", None) or [])
                    if str(e).startswith("kline:")]
    for err in kline_errors:
        # "kline:15min: ..." -> "15min"
        parts = str(err).split(":", 2)
        if len(parts) >= 2 and parts[1] not in missing:
            missing.append(parts[1])
    if n < warmup + step + 30 or missing:
        # 数据不足：这里曾经把 `timeouts` 填成 K 线根数 n。那是个假值——
        # 一笔信号都没有，"超时"从何而来？后果是任何用
        # wins/(trades-timeouts) 算胜率的消费方都会拿到错误的分母
        # （实测 ZEC 只有 370 根日线，凭空多出 372 个"超时"，
        #   把一段真实胜率 48.5% 的区间算成 65.4%）。
        # 现在用 insufficient_data 明确表达，timeouts 归零。
        #
        # `missing` 非空时同样拒绝出结果：只要有一个周期没抓全，因子就在
        # 静默降级，跑出来的期望值看似正常却不可信。宁可明确报缺失，也不
        # 让调用方把一个残缺快照算出的数字当真。
        return BacktestResult(
            symbol=base.symbol, mode=mode, bars=n, trades=0, wins=0, losses=0,
            timeouts=0, no_fills=0, total_r=0.0, avg_win_r=0.0, avg_loss_r=0.0,
            avg_cost_r=0.0, avg_fee_r=0.0, avg_slippage_r=0.0, avg_funding_r=0.0,
            avg_holding_h=0.0, win_rate=0.0, expectancy_r=0.0,
            max_mfe=0.0, max_mae=0.0, equity_curve=[],
            equity_usdt=eq, risk_pct=rp, risk_amount_usdt=risk_amount,
            equity_curve_usdt=[], insufficient_data=True,
            missing_periods=tuple(missing))

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
    no_fills = 0
    total_r = 0.0
    win_rs: list[float] = []
    loss_rs: list[float] = []
    mfes: list[float] = []
    maes: list[float] = []
    curve: list[float] = []
    curve_usdt: list[float] = []
    cum_r = 0.0
    trades = 0
    cost_rs: list[float] = []
    fee_rs: list[float] = []
    slip_rs: list[float] = []
    fund_rs: list[float] = []
    holding_hs: list[float] = []

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
        snap = _slice_snapshot(base, base.klines, idx_by_period, price, ts=ts)
        # 判定用"之后"的 K 线：从 i+1 开始
        future = main[i + 1:]

        plan = build_plan(snap, cfg, mode=mode)
        if plan.direction not in ("long", "short"):
            continue  # 观望或被闸门拦下 -> 不算一笔交易
        # 关键：把"信号时间"改成该建仓点的历史时间 ts，否则 evaluate_outcome
        # 会把所有历史 K 线当成"信号之前"而跳过，永远判不出结果
        plan.generated_at = float(ts)

        res = evaluate_outcome(plan.to_dict(), future)
        if not res:
            continue  # 后续 K 线不足以判定窗口，跳过（不算交易）
        if res["outcome"] == "no_fill":
            # 价格从没进过挂单区 -> 这笔单根本没做上，既不记胜负也不算成本
            no_fills += 1
            continue
        trades += 1
        outcome = res["outcome"]
        mfes.append(res["mfe_pct"])
        maes.append(res["mae_pct"])

        # 持仓时长（用于资金费累计）
        holding_h = res["bars"] * m_period_sec / 3600.0
        holding_hs.append(holding_h)

        # ---- 真实成本（折算成 R：成本占单笔风险的比例）----
        # 成本 = 手续费 + 滑点（都与名义价值成正比，再按 名义/风险 转 R）
        #      + 资金费（与名义×持仓时长成正比；多头付、空头收，随费率正负）
        cost_r = fee_r = slip_r = fund_r = 0.0
        if apply_costs:
            sz = plan.sizing or {}
            risk_usd = sz.get("risk_amount_usdt") or 0
            notional = sz.get("notional_usdt") or 0
            if risk_usd > 0 and notional > 0:
                nr = notional / risk_usd  # 名义 / 风险
                fee_r = (C["taker_fee_bps"] / 10000.0) * 2 * nr   # 开平各一次
                slip_r = (C["slippage_bps"] / 10000.0) * 2 * nr   # 开平各一次
                # 资金费率取该建仓点历史时刻生效的那一期，而不是用"当前费率"
                # 套全部历史（那会让回测在费率剧烈波动的行情里严重失真）。
                fr = (fixed_funding if fixed_funding
                      else (_funding_rate_at(funding_hist, ts) if use_history
                            else fallback_funding))
                if fr:
                    periods = holding_h / funding_period_h
                    sign = 1.0 if plan.direction == "long" else -1.0
                    # 费率为正时多头付、空头收；对 R 的影响取反号
                    fund_r = -sign * fr * periods * nr
            cost_r = fee_r + slip_r + fund_r
            fee_rs.append(fee_r)
            slip_rs.append(slip_r)
            fund_rs.append(fund_r)
            cost_rs.append(cost_r)

        if outcome.startswith("tp"):
            r = (res["rr"] if res["rr"] is not None else 1.0) - cost_r
            wins += 1
            total_r += r
            cum_r += r
            win_rs.append(r)
        elif outcome == "stopped":
            r = -1.0 - cost_r
            losses += 1
            total_r += r
            cum_r += r
            loss_rs.append(r)
        else:  # timeout
            timeouts += 1
        curve.append(cum_r)
        curve_usdt.append(cum_r * risk_amount)

    resolved = wins + losses
    win_rate = wins / resolved if resolved else 0.0
    expectancy = total_r / trades if trades else 0.0
    mean = lambda xs: (sum(xs) / len(xs)) if xs else 0.0
    return BacktestResult(
        symbol=base.symbol, mode=mode, bars=n, trades=trades,
        wins=wins, losses=losses, timeouts=timeouts, no_fills=no_fills,
        total_r=total_r,
        avg_win_r=mean(win_rs),
        avg_loss_r=mean(loss_rs),
        avg_cost_r=mean(cost_rs),
        avg_fee_r=mean(fee_rs),
        avg_slippage_r=mean(slip_rs),
        avg_funding_r=mean(fund_rs),
        avg_holding_h=mean(holding_hs),
        win_rate=win_rate, expectancy_r=expectancy,
        max_mfe=(max(mfes) if mfes else 0.0),
        max_mae=(min(maes) if maes else 0.0),
        equity_curve=curve,
        equity_usdt=eq, risk_pct=rp, risk_amount_usdt=risk_amount,
        equity_curve_usdt=curve_usdt,
    )