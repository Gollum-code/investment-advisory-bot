"""模拟盘持仓追踪：录入持仓后，用现价和最新 K 线跟踪浮动盈亏与止损/止盈。

不下单、不动真钱。程序只做两件事：
1. 算当前浮动盈亏（按方向 × 仓位 × 价差，扣一层估算手续费）。
2. 判定该持仓是否已被止损/止盈打掉（用信号之后的 K 线高低价）。

与 signals 的结果回填不同：这里跟踪的是"用户主动录入的模拟持仓"。
"""

from __future__ import annotations

from typing import Any


def pnl_of(pos: dict, price: float | None, fee_bps: float = 5.0) -> dict:
    """给定现价，算某持仓的浮动盈亏与关键位状态。"""
    entry = pos.get("entry_price")
    size = pos.get("size_coin") or 0.0
    leverage = pos.get("leverage") or 1.0
    direction = pos.get("direction")
    out = {
        "mark_price": price,
        "unrealized_pnl_usdt": 0.0,
        "pnl_pct": 0.0,           # 相对名义价值
        "pnl_of_margin_pct": 0.0,  # 相对保证金
        "margin_usdt": 0.0,
        "notional_usdt": 0.0,
        "distance_to_stop_pct": None,
        "distance_to_tp1_pct": None,
        "status": pos.get("status", "open"),
    }
    if not entry or not size or price is None:
        return out
    notional = entry * size
    margin = notional / max(1.0, leverage)
    if direction == "long":
        pnl = (price - entry) * size
        dist_stop = ((pos.get("stop") or 0) / price - 1.0) * 100 if pos.get("stop") else None
        dist_tp1 = ((pos.get("tp1") or 0) / price - 1.0) * 100 if pos.get("tp1") else None
    else:
        pnl = (entry - price) * size
        dist_stop = (1.0 - (pos.get("stop") or 0) / price) * 100 if pos.get("stop") else None
        dist_tp1 = (1.0 - (pos.get("tp1") or 0) / price) * 100 if pos.get("tp1") else None
    out["unrealized_pnl_usdt"] = round(pnl, 2)
    out["pnl_pct"] = round(pnl / notional * 100, 3) if notional else 0.0
    out["pnl_of_margin_pct"] = round(pnl / margin * 100, 2) if margin else 0.0
    out["margin_usdt"] = round(margin, 2)
    out["notional_usdt"] = round(notional, 2)
    out["distance_to_stop_pct"] = round(dist_stop, 3) if dist_stop is not None else None
    out["distance_to_tp1_pct"] = round(dist_tp1, 3) if dist_tp1 is not None else None
    return out


def check_exit(pos: dict, candles: list[Any]) -> dict | None:
    """用持仓开仓之后的 K 线判定是否被止损/止盈打掉。

    返回 {reason, price, ts}；还没触发返回 None。先到先算，同根 K 线止损优先。
    """
    direction = pos.get("direction")
    entry = pos.get("entry_price")
    stop = pos.get("stop")
    tps = [pos.get(k) for k in ("tp1", "tp2", "tp3") if pos.get(k)]
    opened = pos.get("opened_at") or 0
    if direction not in ("long", "short") or not entry:
        return None
    ordered = sorted(tps) if direction == "long" else sorted(tps, reverse=True)
    for c in candles:
        ts = getattr(c, "ts", None)
        if ts and ts <= opened:
            continue
        high, low = c.high, c.low
        if direction == "long":
            if stop and low <= stop:
                return {"reason": "stop", "price": stop, "ts": ts}
            for tp in ordered:
                if high >= tp:
                    return {"reason": "tp", "tp_index": tps.index(tp) + 1,
                            "price": tp, "ts": ts}
        else:
            if stop and high >= stop:
                return {"reason": "stop", "price": stop, "ts": ts}
            for tp in ordered:
                if low <= tp:
                    return {"reason": "tp", "tp_index": tps.index(tp) + 1,
                            "price": tp, "ts": ts}
    return None