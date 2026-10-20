"""交易成本模型：手续费 + 固定滑点 + 按订单规模估算的价格冲击。

原来的回测把滑点当成常数（2bps）。这在 BTC 上够用，在小币上不对：同样的
下单金额，占当日成交额的比例越高，冲击越大。这里用平方根冲击模型做一个
保守估计，只在有成交额数据时启用（没数据就不假装精确）。

平方根模型（业内常用的经验式）：

    impact_bps = coeff * sqrt(notional / adv)

coeff=30 表示"下单金额等于当日成交额时，冲击约 30bps"。
450 U 的账户在 5000 万 U 成交额的合约上下单，占比约 2e-5，冲击≈0.13bps，
几乎可以忽略；同样的账户在只有 100 万 U 成交额的小币上，冲击≈0.7bps。
真正拉开差距的是大资金：100 万 U 单在 100 万 U 成交额的币上，冲击≈30bps。
"""

from __future__ import annotations

import math


def participation(notional_usd: float, adv_usd: float) -> float:
    """下单金额占当日成交额的比例（0~1，超过 1 按 1 算）。"""
    if notional_usd <= 0 or adv_usd <= 0:
        return 0.0
    return min(notional_usd / adv_usd, 1.0)


def impact_bps(notional_usd: float, adv_usd: float,
               coeff_bps: float = 30.0) -> float:
    """平方根价格冲击（bps）。没有成交额数据时返回 0（不假装精确）。"""
    part = participation(notional_usd, adv_usd)
    if part <= 0 or coeff_bps <= 0:
        return 0.0
    return coeff_bps * math.sqrt(part)


def trade_cost_bps(notional_usd: float, adv_usd: float, *,
                   taker_fee_bps: float = 5.0, maker_fee_bps: float = 2.0,
                   slippage_bps: float = 2.0, impact_coeff_bps: float = 30.0,
                   maker: bool = False) -> dict:
    """单笔往返（开+平）的成本拆分，单位 bps。

    maker=True 时用挂单费率，但固定滑点按 0 算（挂单本来就是要省这个），
    价格冲击仍然算——挂单成交后一样要吃冲击。现实中 maker 不一定成交，
    这里只做成本口径的对比，不代表一定能挂上。
    """
    fee_one_side = maker_fee_bps if maker else taker_fee_bps
    slip_one_side = 0.0 if maker else slippage_bps
    impact = impact_bps(notional_usd, adv_usd, impact_coeff_bps)
    fee = fee_one_side * 2
    slip = (slip_one_side + impact) * 2
    return {
        "fee_bps": round(fee, 4),
        "slippage_bps": round(slip, 4),
        "impact_bps": round(impact * 2, 4),
        "total_bps": round(fee + slip, 4),
        "participation": round(participation(notional_usd, adv_usd), 8),
        "maker": bool(maker),
    }
