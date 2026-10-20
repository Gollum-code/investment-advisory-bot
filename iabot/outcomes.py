"""信号结局判定：拿信号之后的 K 线，判断先碰止损还是先到目标位。

只读公开行情，不做任何假设；判定规则保持保守：

- **先要求挂单成交**：入场是一个"回撤区间"，价格没进过区间就是没做上，
  返回 outcome="no_fill"，不该计进任何胜负统计。多单挂在现价下方，
  价格直接涨走就永远不会成交——不判成交的话，这些单会因为"后来涨到了止盈"
  被算成胜利，只往里灌水（实测 14% 的信号从未成交，其中 99.7% 被判胜利，
  把胜率从 46.7% 虚抬到 54.0%）。
- 多头：某根 K 线 low <= 止损 -> 止损；high >= 某档目标 -> 命中该档
- 空头：反过来
- 同一根 K 线同时碰止损和目标（插针/宽幅）-> 按止损算（更悲观）
- 窗口内没碰到任何位置 -> timeout（视为未了结，不参与胜率统计）
"""

from __future__ import annotations

from .market import Candle

# 信号最多等多久判结局。日内用 15m 周期（48h = 192 根），波段用日线（21 天），
# 超卖反弹也是日线、持有 1~2 天，给 10 天窗口让反弹要么回到 SMA20、要么打止损。
DEFAULT_WINDOW_HOURS = {"intraday": 48.0, "swing": 21 * 24.0, "reversion": 10 * 24.0}
EXTRA_BUFFER_SEC = 300  # 信号生成到 K 线根结束之间的余量

# no_fill = 挂单从未成交；它不是"没结果"，而是"这笔单不存在"
OUTCOMES_OK = ("tp1", "tp2", "tp3", "stopped", "timeout", "no_fill")


def evaluate_outcome(plan: dict, candles: list[Candle],
                     *, max_hours: float | None = None,
                     require_fill: bool = True,
                     breakeven_at_r: float | None = None,
                     trail_atr_mult: float | None = None,
                     atr: float | None = None) -> dict | None:
    """返回 {outcome, ts, price, rr, mfe_pct, mae_pct, bars}，无法判定时返回 None。

    require_fill=True 时，价格必须先进入 [entry_low, entry_high] 才算成交，
    未成交返回 outcome="no_fill"（调用方应把它排除在胜率/期望之外）。

    breakeven_at_r: 浮盈到这么多 R 时把止损移到入场价（保本）。之后的回撤
                    按 0R 出场，而不是 -1R。
    trail_atr_mult: 移动止损，止损跟着最优价走，距离 = 倍数 × ATR。
                    需要同时传 atr，否则忽略。

    两者都只影响**后续** K 线：同一根 K 线里不会"看到高点再把止损追上去"，
    避免用未来信息抬高胜率。
    """
    direction = plan.get("direction")
    if direction not in ("long", "short"):
        return None
    stop = plan.get("stop")
    entry_low, entry_high = plan.get("entry_low"), plan.get("entry_high")
    targets = [t for t in (plan.get("targets") or [])
               if isinstance(t, dict) and t.get("price") is not None]
    if not stop or not entry_low or not entry_high or not targets:
        return None
    mid = (entry_low + entry_high) / 2.0
    if mid <= 0 or stop <= 0:
        return None
    # 每单位风险（价格单位）。rr 一律用"相对原始 1R"表示。
    risk_unit = abs(mid - stop)
    if risk_unit <= 0:
        return None
    trail_on = bool(trail_atr_mult and atr and atr > 0)

    start = float(plan.get("generated_at") or 0)
    hours = max_hours if max_hours is not None else DEFAULT_WINDOW_HOURS.get(
        plan.get("mode"), DEFAULT_WINDOW_HOURS["intraday"])
    horizon = start + hours * 3600
    # 目标按离入场最近的优先（多头先到低价档，空头先到高价档）
    ordered = (sorted(targets, key=lambda t: t["price"]) if direction == "long"
               else sorted(targets, key=lambda t: -t["price"]))

    def rr_at(px: float) -> float:
        return ((px - mid) / risk_unit if direction == "long"
                else (mid - px) / risk_unit)

    best_mfe = 0.0
    best_mae = 0.0
    bars = 0
    filled = not require_fill
    stop_dyn = float(stop)
    best_price = mid          # 入场后的最优价（多头最高价 / 空头最低价）
    for c in candles:
        if c.ts <= start + EXTRA_BUFFER_SEC:
            continue
        if c.ts > horizon:
            break
        bars += 1
        if not filled:
            # 挂单要价格进过入场区间才算成交（区间有重叠即视为触及）。
            # 没成交就不看止损/止盈 —— 这笔单根本不存在。
            if not (c.low <= entry_high and c.high >= entry_low):
                continue
            filled = True
        if direction == "long":
            mfe = (c.high - mid) / mid * 100.0
            mae = (c.low - mid) / mid * 100.0
            best_price = max(best_price, c.high)
        else:
            mfe = (mid - c.low) / mid * 100.0
            mae = (mid - c.high) / mid * 100.0
            best_price = min(best_price, c.low)
        best_mfe = max(best_mfe, mfe)
        best_mae = min(best_mae, mae)

        stop_hit = (c.low <= stop_dyn) if direction == "long" else (c.high >= stop_dyn)
        if stop_hit:
            # 用动态止损价算实际 R：保本出场约 0R，移动止损可能锁住部分盈利。
            return {"outcome": "stopped", "ts": c.ts, "price": stop_dyn,
                    "rr": round(rr_at(stop_dyn), 4),
                    "mfe_pct": round(best_mfe, 3),
                    "mae_pct": round(best_mae, 3), "bars": bars}
        for i, t in enumerate(ordered, 1):
            hit = (c.high >= t["price"]) if direction == "long" else (c.low <= t["price"])
            if hit:
                return {"outcome": f"tp{i}", "ts": c.ts, "price": t["price"],
                        "rr": t.get("rr"), "mfe_pct": round(best_mfe, 3),
                        "mae_pct": round(best_mae, 3), "bars": bars}

        # ---- 收盘后更新动态止损（只作用于下一根 K 线）----
        if breakeven_at_r and rr_at(best_price) >= breakeven_at_r:
            if direction == "long":
                stop_dyn = max(stop_dyn, mid)
            else:
                stop_dyn = min(stop_dyn, mid)
        if trail_on:
            dist = float(trail_atr_mult) * float(atr)
            if direction == "long":
                stop_dyn = max(stop_dyn, best_price - dist)
            else:
                stop_dyn = min(stop_dyn, best_price + dist)

    if bars == 0:
        return None  # 还没有任何后续 K 线，等下一轮再说
    if not filled:
        # 窗口内价格从没进过挂单区 -> 这笔单不存在，不计胜负
        return {"outcome": "no_fill", "ts": horizon, "price": None, "rr": None,
                "mfe_pct": 0.0, "mae_pct": 0.0, "bars": bars}
    return {"outcome": "timeout", "ts": horizon, "price": None, "rr": None,
            "mfe_pct": round(best_mfe, 3), "mae_pct": round(best_mae, 3),
            "bars": bars}


def window_atr_period(mode: str) -> str:
    from .signals import MODES
    return (MODES.get(mode) or MODES["intraday"])["atr_period"]