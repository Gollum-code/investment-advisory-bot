"""回测实验记录：把"这次回测用了什么参数、得到什么结果"存成可追溯的档案。

记录的价值在于**可复现和可对比**：
- 复现：几个月后想复现某个结果，靠 params 快照就能知道当时用的权重/阈值/成本。
- 对比：同一个合约改了参数重跑，几次结果并排看，才知道改动到底有没有用。

所以 params 必须完整到"换个环境重跑能得到同样结果"，而不只是记个模式名。
"""

from __future__ import annotations

import time
from typing import Any

from .signals import FACTOR_DEFAULTS


def build_params(cfg: dict, *, symbol: str, mode: str, bars: int, step: int,
                 warmup: int, equity_usdt: float, risk_pct: float) -> dict[str, Any]:
    """从 cfg 抽出一份足以复现该次回测的参数快照。"""
    an = cfg.get("analysis") or {}
    acc = cfg.get("account") or {}
    bt = an.get("backtest") or {}
    res = an.get("resonance") or {}
    weights = dict(FACTOR_DEFAULTS)
    for k, v in (an.get("factor_weights") or {}).items():
        if isinstance(v, (int, float)) and v > 0:
            weights[k] = float(v)
    return {
        "symbol": symbol,
        "mode": mode,
        "bars": int(bars),
        "step": int(step),
        "warmup": int(warmup),
        "max_points": 2000,
        "profile": cfg.get("active_profile") or "",
        "equity_usdt": float(equity_usdt),
        "risk_pct": float(risk_pct),
        "score_threshold": float(an.get("score_threshold") or 30),
        "factor_weights": weights,
        "resonance": {"enabled": bool(res.get("enabled")),
                      "weight": float(res.get("weight") or 0.06)},
        "costs": {
            "taker_fee_bps": float(bt.get("taker_fee_bps") or 5.0),
            "slippage_bps": float(bt.get("slippage_bps") or 2.0),
            "funding": bool(bt.get("funding", True)),
            "funding_period_sec": int(bt.get("funding_period_sec") or 28800),
        },
        "account": {k: acc.get(k) for k in ("equity_usdt", "preferred_leverage",
                                            "risk_pct_per_trade", "max_margin_pct")},
        "recorded_at": time.time(),
    }


def summarize_result(result: dict) -> dict[str, Any]:
    """从回测结果里挑出适合并排对比的关键指标。"""
    keys = ("trades", "wins", "losses", "timeouts", "win_rate", "expectancy_r",
            "avg_win_r", "avg_loss_r", "avg_cost_r", "avg_fee_r", "avg_slippage_r",
            "avg_funding_r", "avg_holding_h", "total_r", "equity_usdt",
            "risk_amount_usdt", "final_equity_usdt", "return_pct", "bars",
            "max_mfe", "max_mae")
    return {k: result.get(k) for k in keys if k in result}


def diff_params(a: dict, b: dict) -> list[str]:
    """比较两次回测的参数差异（用于 UI 提示"你改了 X"）。"""
    out: list[str] = []
    if not a or not b:
        return out
    scalar = (("mode", "模式"), ("bars", "K线根数"), ("step", "建仓点步长"),
              ("score_threshold", "评分阈值"), ("equity_usdt", "账户权益"),
              ("risk_pct", "单笔风险%"), ("profile", "参数方案"))
    for key, label in scalar:
        if a.get(key) != b.get(key):
            out.append(f"{label}: {a.get(key)} → {b.get(key)}")
    aw, bw = a.get("factor_weights") or {}, b.get("factor_weights") or {}
    for k in FACTOR_DEFAULTS:
        if abs(float(aw.get(k, 0)) - float(bw.get(k, 0))) > 1e-9:
            out.append(f"权重 {k}: {aw.get(k)} → {bw.get(k)}")
    ar, br = a.get("resonance") or {}, b.get("resonance") or {}
    if ar.get("enabled") != br.get("enabled"):
        out.append(f"共振因子: {'开' if ar.get('enabled') else '关'} → "
                   f"{'开' if br.get('enabled') else '关'}")
    ac, bc = a.get("costs") or {}, b.get("costs") or {}
    for key, label in (("taker_fee_bps", "手续费bps"), ("slippage_bps", "滑点bps")):
        if float(ac.get(key, 0)) != float(bc.get(key, 0)):
            out.append(f"{label}: {ac.get(key)} → {bc.get(key)}")
    return out