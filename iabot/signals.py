"""信号模型：多因子打分 -> 方向 -> 入场/止损/止盈 -> 仓位。

设计原则和"Agent 写规则、引擎跑规则"一致：把人工看盘的那套判断固化成本地
确定性计算，任何一次分析都可以复现，也不依赖大模型。

支持两种模式：
  intraday（日内）  —— 用 15 分钟 ATR 定止损（约 0.4~0.8%），目标位取自 5m/15m/1h
                        的结构位与盘口挂单，持仓周期数小时。
  swing（波段）     —— 用日线 ATR 定止损（约 2~3%），目标位取自日线结构、
                        斐波那契与成交密集区，持仓周期数天到数周。
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field

from . import indicators as ind
from .config import DEFAULT_MODE
from .levels import (Level, fib_levels, merge_levels, moving_average_levels,
                     orderbook_walls, price_ladder, round_number_levels,
                     swing_points, top_volume_nodes)
from .liquidity import assess as assess_liquidity
from .market import Snapshot, bars_per_year
from .risk import liquidation_price, safe_max_leverage, stop_is_effective

# tanh 归一化尺度：加权均值除以它之后过 tanh。
# 0.40 => 加权均值 ±0.40 约对应 ±76 分；±0.20 约对应 ±46 分。
SCORE_SCALE = 0.40

# 趋势确认因子在方向判据里的折价。趋势因子 IC 在两段时序切分间会翻转
# （train -0.089 / test -0.162），所以它只能"确认"不能"发起"：
# 方向只认 状态因子 + TREND_CONFIRM_SCALE × 趋势因子。
TREND_CONFIRM_SCALE = 0.5
# 状态闸门：状态因子（波动率/成交量/资金费）加权均值低于此值就不开仓。
STATE_GATE_MIN = 0.10

# 因子"没数据"的详情里必然出现的词。用它判断这个因子是否真的算过，
# 而不是靠"分数是 0"——分数 0 也可能是"算出来正好中性"（例如 RSI=50），
# 那种参与合成是对的，不能剔除。
_DEAD_MARKS = ("数据不足", "无法计算", "接口失败", "无盘口", "无成交",
               "无资金费", "空数据", "数据为空")


def _dead_factor_names(factors: dict) -> set:
    """哪些因子是"没数据"而不是"算出来是中性"。

    注意：只按详情判定，不看分数。分数为 0 且详情正常 => 中性，参与合成。
    """
    dead = set()
    for name, f in factors.items():
        detail = str(getattr(f, "detail", "") or "")
        if any(m in detail for m in _DEAD_MARKS):
            dead.add(name)
    return dead

# ---- 模式定位：谁是主策略、谁是实验品 ----
# 用历史数据定的。关键教训：**样本窗口决定结论**。
#
# 第一次（每币 300 根日线，即最近约 3 个月）：
#   swing      n=737  期望 +0.302R  95%CI [+0.222, +0.386]
#   reversion  n= 71  期望 +0.367R  —— 看起来和 swing 一样好
# 据此把 reversion 定为"从属、半仓"。**这个结论是被短窗口骗的。**
#
# 放宽到 2000 根日线（约 5.5 年、覆盖 2021-2026 四个区制）后：
#   swing      n=5418  期望 +0.184R  95%CI [+0.150, +0.217]  三个区制全正
#   reversion  n= 752  期望 -0.384R  95%CI [-0.478, -0.285]  20 个币全负
# 而且 reversion 的**原始前向收益本来就是 0**（1 日 +0.08% / 2 日 -0.01% /
# 3 日 +0.06%），正的只有 2025-2026 一段，2021-2022 熊市是负的。
# 即：信号本身没有 edge，不是出场设计的问题——0% 的原始收益扣掉成本必为负。
# 之前那个 +0.367R 纯属"最近三个月恰好顺手"+ 两个币撑起来的样本假象。
#
# 结论：reversion 判为**无 edge，不建议实盘**。保留代码仅供研究/回测，
# 默认不启用；它曾经那套"从属半仓"的定位作废（见下面常量注释）。
PRIMARY_MODE = "swing"
# 作废的定位参数：曾用于"从属半仓"，但完整历史证明该通道无 edge，
# 保留只是为了不破坏既有测试与配置引用。不要据此认为它有正期望。
SUBORDINATE_RISK_SCALE = 0.5
SUBORDINATE_NEEDS_PRIMARY_SILENT = True
# 判为无 edge、不建议实盘的模式（信号在完整历史上原始收益为 0）。
NO_EDGE_MODES = ("reversion",)

MODES = {
    "intraday": {
        "label": "日内",
        "atr_period": "15min",
        "stop_atr_mult": 2.0,      # 止损 = 2.0 × ATR(15m)
        # 目标位候选的门槛不再用 ATR 倍数，而用盈亏比（analysis.gate.min_target_rr）：
        # "离现价多远"和"离入场多远"是两件事，后者直接决定这单值不值得做。
        "max_target_atr": 14.0,
        "min_dist_atr": 0.45,      # 入场基准离现价至少这么远（剔除贴近现价的噪声位）
        "level_lookback": {"5min": 288, "15min": 200, "60min": 160},
        "use_daily_fib": False,
        "stop_pct_range": (0.30, 2.50),   # 止损幅度（占价格 %）的合理区间
        "entry_width_pct": 0.12,          # 入场带最小宽度
    },
    "swing": {
        "label": "波段",
        "atr_period": "1day",
        "stop_atr_mult": 0.8,
        "max_target_atr": 9.0,
        "min_dist_atr": 0.35,
        "level_lookback": {"60min": 240, "1day": 90},
        "use_daily_fib": True,
        "stop_pct_range": (1.50, 6.00),
        "entry_width_pct": 0.50,
    },
    # 超卖反弹通道：**无 edge，不建议实盘**。代码保留供研究/回测。
    #
    # 来源：swing 在跌段里做多是亏的，试过五种闸门都修不掉（根因见 README：
    # edge 非对称，做空两段都赚、做多只在涨段赚）。于是想另开一条补缺口。
    #
    # 结果是被短样本骗了两次，完整历史推翻了它：
    #   300 根日线（最近约 3 个月）：n=71  期望 +0.367R  —— 看着能打
    #   2000 根日线（约 5.5 年）：    n=752 期望 -0.384R  20 个币全负
    # 更根本的是**原始前向收益就是 0**（1/2/3 日：+0.08% / -0.01% / +0.06%），
    # 只有 2025-2026 一段为正、2021-2022 熊市为负。
    # 0% 的原始信号扣掉手续费滑点必然为负，所以不是出场设计能救的。
    #
    # 保留原因：触发逻辑（RSI2/SMA20/SMA200）本身是干净的实现，
    # 后续若换标的/换市场（例如只在震荡市启用）仍可复用这套框架回测验证。
    # 但默认不启用，且不要把它当成正期望策略来配仓位。
    "reversion": {
        "label": "超卖反弹",
        "atr_period": "1day",
        "stop_atr_mult": 0.8,
        "max_target_atr": 9.0,
        "min_dist_atr": 0.35,
        "level_lookback": {"60min": 240, "1day": 90},
        "use_daily_fib": True,
        "stop_pct_range": (1.50, 6.00),
        "entry_width_pct": 0.50,
        # 触发条件：RSI(2) 低于此值 且 收盘低于 SMA20×(1+gap) 且 收盘低于 SMA200。
        # 三者分别管"跌透了""还没涨回来""整体仍是跌势"。
        "oversold_rsi_period": 2,
        "oversold_rsi_max": 10.0,
        "oversold_sma20_gap": 0.03,
        "oversold_sma200_period": 200,
        # 目标位就是回到 SMA20（这是回抽的定义终点），不再往远处找压力位
        "reversion_target": "sma20",
    },
}


@dataclass
class Factor:
    name: str
    label: str
    score: float          # -1(极空) .. +1(极多)
    weight: float
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "score": round(self.score, 3),
                "weight": round(self.weight, 3), "detail": self.detail,
                "contribution": round(self.score * self.weight, 4)}


@dataclass
class Plan:
    symbol: str
    mode: str = "intraday"
    direction: str = "wait"          # long | short | wait
    score: float = 0.0               # -100 .. +100
    confidence: str = "低"
    confidence_score: float = 0.0    # 0-100 数值置信度（同向因子加权占比 × 评分强度）
    entry_low: float | None = None
    entry_high: float | None = None
    entry_note: str = ""
    stop: float | None = None
    stop_pct: float | None = None
    targets: list[dict] = field(default_factory=list)
    rr: float | None = None
    invalidation: str = ""
    sizing: dict = field(default_factory=dict)
    factors: list[Factor] = field(default_factory=list)
    market: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 被风控闸门拦下来时的解释：评分给了方向，但位置不划算
    gate: dict = field(default_factory=dict)
    # 被状态闸门拦下来时的解释：趋势因子想给方向，但横截面状态不友好
    state_gate: dict = field(default_factory=dict)
    price: float | None = None
    generated_at: float = 0.0
    profile: str = ""
    # 本次评分有多少权重是真有数据的。1.0 = 全部因子都在；
    # 历史回测里可能只有 0.5 左右（盘中周期只有最近 2000 根），
    # 这时候"评分"只代表少数因子的意见，不该被当成全体一致。
    factor_coverage: float = 1.0
    dead_factors: list[str] = field(default_factory=list)
    # 11 个因子的加权平均分（仅供参考）。它**不是**决定方向的那个分数：
    # 方向用 state/trend 拆分后的 plan.score。两者可以差很多，
    # 混用会造成"显示 -84 却观望"。
    score_all_factors: float = 0.0
    # 多空不对称风险倍数：完整历史上空单在两个方向都赚钱、多单只在上涨市
    # 赚钱，所以允许给空单更大的单笔风险（默认 1.0 = 不调整，安全起见）。
    risk_mult_long: float = 1.0
    risk_mult_short: float = 1.0
    # 生效中的回测成本参数（taker/maker/滑点/冲击系数），供 sizing 摊开成本
    costs_cfg: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "factors"}
        d["factors"] = [f.to_dict() for f in self.factors]
        return d

    def headline(self) -> str:
        if self.direction == "wait":
            return f"{self.symbol} 观望（{self.score:+.0f}）"
        cn = "做多" if self.direction == "long" else "做空"
        return f"{self.symbol} {cn}（{self.score:+.0f}，置信度{self.confidence}）"


# --------------------------------------------------------------------------
# 因子
# --------------------------------------------------------------------------

def _slope_pct(vals: list[float | None], n: int) -> float:
    """末段线性斜率，归一化为每 bar 变化率（%）。

    ind.sma/ema 返回带前导 None 的序列，必须先把 None 剥掉再算，
    否则 sum(vals[-n:]) 会在数据不足时 TypeError。"""
    w = [v for v in vals[-n:] if v is not None]
    if len(w) < 2:
        return 0.0
    k = len(w) - 1
    mx = sum(range(k + 1)) / (k + 1)
    my = sum(w) / (k + 1)
    num = sum((i - mx) * (w[i] - my) for i in range(k + 1))
    den = sum((i - mx) ** 2 for i in range(k + 1)) or 1.0
    b = num / den
    return b / my if my else 0.0


def _dampen_trend(score: float, cfg: dict | None) -> float:
    """对极端的日线趋势读数做衰减（默认关）。

    动机是实测：评分 79% 的单子由"日线趋势"主导，而趋势越极端越可能是行情
    尾声——低分2 档期望 +0.288R，高分档只有 +0.111R。衰减只削超出
    dampen_at 的那部分（保留方向），不动接近中性的那一段。

    这是因子层改动，默认关；要先用影子账户验证，不能凭一次回测就打开。
    """
    td = ((cfg or {}).get("analysis") or {}).get("trend_dampen") or {}
    if not td.get("enabled"):
        return score
    at = float(td.get("dampen_at") or 0.6)
    k = float(td.get("k") or 1.0)
    if k <= 0:
        return score
    sign = 1.0 if score >= 0 else -1.0
    over = abs(score) - at
    if over <= 0:
        return score
    # 超出部分按 1/(1+over/k) 衰减：k 越大衰减越弱
    kept = at + over / (1.0 + over / k)
    return sign * max(0.0, min(1.0, kept))


def _f_trend(snap: Snapshot) -> Factor:
    """日线趋势。

    旧版只比"价格在哪一侧"，三条均线等权布尔打分，SMA200 权重最高(1.6)
    却最滞后。实测 BTC：价格在 SMA20 下 1.7%、SMA50 上 0.6%、SMA200 上 13.0%
    —— 已经跌回短期线下方、中期线贴脸，仍是 +0.776 强烈看多。
    因为 SMA200 那票只看"还在上方"，13.0% 和 0.6% 同票。

    均线关系是**状态量**不是动量：价格从 SMA200 上方跌到下方，全程拿正票，
    只在穿线那一刻翻符号 —— 顶部附近会一路报多直到跌穿完成。
    这个因子天然不能做反转信号，只能做趋势确认。

    修法：三项都换成"带距离衰减的方向"。
      - 每票从布尔 ±w 改为 w · tanh(dist / k)，距离越近票越轻
        （k 按均线周期给：短均线贴近是常态，长均线贴近才是信号）
      - 增加均线自身斜率项：均线在往下走时，价格站在它上方不算多
      - 斜率与距离同向才给满票，反向时互相抵消
    """
    cs = snap.closes("1day")
    if len(cs) < 60:
        return Factor("trend_daily", "日线趋势", 0.0, _w("trend_daily"), "日线数据不足")
    price = snap.price or cs[-1]

    specs = (
        ("SMA20", ind.sma(cs, 20), 1.0, 20),
        ("SMA50", ind.sma(cs, 50), 1.3, 50),
        ("SMA200", ind.sma(cs, 200), 1.6, 200),
    )
    pts = 0.0
    parts = []
    for name, vals, w, n in specs:
        v = ind.last(vals)
        if not v:
            continue
        dist = price / v - 1.0
        s = _slope_pct(vals, min(20, n))
        # 距离票：贴近均线时收缩。短均线允许更宽的中性区。
        k = 0.02 if n <= 20 else (0.04 if n <= 50 else 0.08)
        d_vote = w * math.tanh(dist / k)
        # 斜率票：均线自己在走，方向决定站在它上方算不算多。
        # 只有均线向上时"价格在上方"才给同向票，否则扣掉。
        s_vote = w * math.tanh(s / (0.004 if n <= 50 else 0.002))
        above = price > v
        # 距离与斜率同号才保留，反号时斜率票削弱距离票
        if (d_vote > 0) == (s_vote > 0):
            pts += d_vote + 0.5 * s_vote
        else:
            pts += d_vote + 0.5 * s_vote
        parts.append(f"{'上' if above else '下'}穿{name}({dist * 100:+.1f}%)"
                     f"{'↑' if s > 0 else '↓'}")
    e12, e26 = ind.last(ind.ema(cs, 12)), ind.last(ind.ema(cs, 26))
    if e12 and e26:
        pts += 0.8 if e12 > e26 else -0.8
        parts.append(f"EMA12{'＞' if e12 > e26 else '＜'}EMA26")
    score = max(-1.0, min(1.0, pts / 6.6 * 1.35))
    return Factor("trend_daily", "日线趋势", score, _w("trend_daily"),
                  "；".join(parts))


def _f_intraday(snap: Snapshot) -> Factor:
    """1 小时趋势。

    旧版布尔打分：现价在 60min SMA20 上方就给 +1.0，与"高出多少"无关。
    实测 IC = **-0.199**（20 币 × 2060 点，5 日持有）——
    这是全部因子里最严重的反向：它在小时线反弹的末端报多，反弹结束即回落。
    根因是布尔票在反弹启动和反弹见顶时输出完全相同。

    改成与 _f_trend 同构：距离衰减 + 均线自身斜率。
    """
    cs = snap.closes("60min")
    if len(cs) < 30:
        return Factor("trend_intraday", "1小时趋势", 0.0, _w("trend_intraday"), "1小时数据不足")
    price = snap.price or cs[-1]

    pts = 0.0
    parts = []
    for name, vals, w in (("SMA20", ind.sma(cs, 20), 1.0),
                          ("SMA50", ind.sma(cs, 50), 1.2)):
        v = ind.last(vals)
        if not v:
            continue
        dist = price / v - 1.0
        s = _slope_pct(vals, 20)
        pts += w * math.tanh(dist / 0.01)      # 1% 即饱和
        pts += 0.5 * w * math.tanh(s / 0.003)
        above = price > v
        parts.append(f"现价在 {name} {'上' if above else '下'}方 {dist * 100:+.2f}%"
                     f"{'↑' if s > 0 else '↓'}")
    _, _, hist = ind.macd(cs)
    h = ind.last(hist)
    if h is not None:
        # MACD 柱本身是动量差，保留布尔但降权：它的 IC 未单独验证，
        # 而距离项已验证过方向，不能让未验证项盖过已验证项。
        pts += 0.5 if h > 0 else -0.5
        parts.append(f"MACD柱{'正' if h > 0 else '负'}({h:+,.2f})")
    return Factor("trend_intraday", "1小时趋势", max(-1.0, min(1.0, pts / 3.1)),
                  _w("trend_intraday"), "；".join(parts))


def _f_momentum(snap: Snapshot) -> Factor:
    cs15, cs60 = snap.closes("15min"), snap.closes("60min")
    r15 = ind.last(ind.rsi(cs15, 14)) if len(cs15) > 20 else None
    r60 = ind.last(ind.rsi(cs60, 14)) if len(cs60) > 20 else None
    if r15 is None and r60 is None:
        return Factor("momentum", "动量(RSI)", 0.0, _w("momentum"), "RSI 数据不足")
    pts, parts = 0.0, []
    for label, r in (("15m", r15), ("1h", r60)):
        if r is None:
            continue
        if r >= 75:
            pts -= 0.7; parts.append(f"{label} RSI {r:.0f} 超买")
        elif r >= 60:
            pts += 0.5; parts.append(f"{label} RSI {r:.0f} 偏强")
        elif r <= 25:
            pts += 0.7; parts.append(f"{label} RSI {r:.0f} 超卖")
        elif r <= 40:
            pts -= 0.5; parts.append(f"{label} RSI {r:.0f} 偏弱")
        else:
            parts.append(f"{label} RSI {r:.0f} 中性")
    return Factor("momentum", "动量(RSI)", max(-1.0, min(1.0, pts / 1.4)), _w("momentum"), "；".join(parts))


def _f_structure(snap: Snapshot) -> Factor:
    cs = snap.klines.get("60min", [])
    if len(cs) < 40:
        return Factor("structure", "摆动结构", 0.0, _w("structure"), "数据不足")
    highs, lows = swing_points(cs, width=3)
    pts, parts = 0.0, []
    if len(highs) >= 2:
        up = highs[-1][1] > highs[-2][1]
        pts += 0.7 if up else -0.7
        parts.append(f"高点{'抬升' if up else '降低'} {highs[-2][1]:,.2f}→{highs[-1][1]:,.2f}")
    if len(lows) >= 2:
        up = lows[-1][1] > lows[-2][1]
        pts += 0.7 if up else -0.7
        parts.append(f"低点{'抬升' if up else '降低'} {lows[-2][1]:,.2f}→{lows[-1][1]:,.2f}")
    a = ind.last(ind.adx([c.high for c in cs], [c.low for c in cs], [c.close for c in cs], 14))
    if a is not None:
        parts.append(f"ADX {a:.0f} {'趋势市' if a >= 25 else ('震荡市' if a < 20 else '过渡')}")
    return Factor("structure", "摆动结构", max(-1.0, min(1.0, pts / 1.4)), _w("structure"), "；".join(parts))


def _f_vwap(snap: Snapshot) -> Factor:
    cs = snap.klines.get("5min", [])
    if len(cs) < 30:
        return Factor("vwap", "日内VWAP", 0.0, _w("vwap"), "数据不足")
    day_start = (cs[-1].ts // 86400) * 86400
    start = next((i for i, c in enumerate(cs) if c.ts >= day_start), 0)
    v = ind.last(ind.vwap([c.high for c in cs], [c.low for c in cs],
                          [c.close for c in cs], [c.volume for c in cs], start=start))
    price = snap.price
    if v is None or not price:
        return Factor("vwap", "日内VWAP", 0.0, _w("vwap"), "无法计算")
    dev = (price / v - 1.0) * 100
    return Factor("vwap", "日内VWAP", max(-1.0, min(1.0, dev / 0.8)), _w("vwap"),
                  f"现价 vs VWAP {v:,.2f}（{dev:+.2f}%）")


def _f_funding(snap: Snapshot) -> Factor:
    """资金费率。

    同样要相对自身基线：费率有强结构性偏向（牛市里绝大多数合约长期正费率），
    用固定阈值打分会让几乎所有币长期落在"多头拥挤"那一档。这里改成
    "当前费率相对 30 日均值的偏离"，既保留极值反转的意图，又有横截面区分度。
    """
    r = snap.funding_rate
    if r is None:
        return Factor("funding", "资金费率", 0.0, _w("funding"), "无数据")
    ann = r * 3 * 365 * 100
    vals = [float(x.get("funding_rate") or 0) for x in (snap.funding_history or [])[-90:]]
    mean = sum(vals) / len(vals) if vals else None
    detail = f"当前 {r * 100:+.4f}%/8h（年化 {ann:+.1f}%）"
    if mean is None or abs(mean) < 1e-9:
        # 没有历史可比时退回绝对阈值
        if r >= 0.0005:
            score = -0.9
        elif r >= 0.0002:
            score = -0.45
        elif r <= -0.0005:
            score = 0.9
        elif r <= -0.0002:
            score = 0.45
        else:
            score = 0.0
    else:
        scale = 0.0
        if len(vals) >= 3:
            scale = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
        if scale <= 1e-9:
            scale = abs(mean) or 0.0001
        # 费率高于自身常态 = 多头拥挤 -> 看空（反向），与原来的方向一致
        score = max(-1.0, min(1.0, -(r - mean) / (2.0 * scale)))
        detail += f"，30日均 {mean * 100:+.4f}%"
    detail += "，多头拥挤" if score < 0 else ("空头拥挤" if score > 0 else "，情绪中性")
    return Factor("funding", "资金费率", score, _w("funding"), detail)


def _f_oi(snap: Snapshot) -> Factor:
    hist = snap.oi_history
    if len(hist) < 8 or not snap.price:
        return Factor("open_interest", "持仓量", 0.0, _w("open_interest"), "数据不足")
    now = float(hist[-1].get("volume") or 0)
    ago = float(hist[-8].get("volume") or 0)
    if not now or not ago:
        return Factor("open_interest", "持仓量", 0.0, _w("open_interest"), "数据不足")
    oi_chg = (now / ago - 1.0) * 100
    cs = snap.closes("1day")
    px_chg = ((cs[-1] / cs[-8] - 1.0) * 100) if len(cs) > 8 else None
    if px_chg is None:
        return Factor("open_interest", "持仓量", 0.0, _w("open_interest"), f"7日持仓 {oi_chg:+.1f}%")
    if px_chg > 0.5 and oi_chg > 1.0:
        score, tag = 0.6, "价涨仓增（多头主动进场）"
    elif px_chg > 0.5 and oi_chg < -1.0:
        score, tag = -0.3, "价涨仓减（空头回补，易衰竭）"
    elif px_chg < -0.5 and oi_chg > 1.0:
        score, tag = -0.7, "价跌仓增（空头主动进场）"
    elif px_chg < -0.5 and oi_chg < -1.0:
        score, tag = 0.3, "价跌仓减（多头止损离场，接近洗盘尾声）"
    else:
        score, tag = 0.0, "量价背离不明显"
    return Factor("open_interest", "持仓量", score, _w("open_interest"),
                  f"7日持仓 {oi_chg:+.1f}%、价格 {px_chg:+.1f}% · {tag}")


def _f_elite(snap: Snapshot) -> Factor:
    """大户多空净比。

    必须打"相对自己近期基线"的偏离，不能直接打绝对值：
    实测 40 个合约里 39 个的净比 <= 0（范围 [-0.116, +0.006]），
    因为大户拿永续空单对冲现货是常态。用绝对值打分的话这个 0.12 权重的因子
    永远只会输出负分，既没有横截面区分度，又给所有币一个恒定的看空拖累。
    """
    rows = snap.elite_position
    if not rows:
        return Factor("elite", "大户持仓", 0.0, _w("elite"), "接口无数据")

    def net(r):
        return float(r.get("buy_ratio") or 0) - float(r.get("sell_ratio") or 0)

    latest = net(rows[-1])
    window = rows[-14:] if len(rows) >= 14 else rows
    nets = [net(r) for r in window]
    baseline = sum(nets) / len(nets)
    dev = latest - baseline
    # 尺度用自身波动：净比的日常波动远小于 0.12，固定除数是拍脑袋的
    scale = 0.0
    if len(nets) >= 3:
        mu = baseline
        scale = (sum((x - mu) ** 2 for x in nets) / (len(nets) - 1)) ** 0.5
    if scale <= 1e-6:
        scale = 0.02                      # 退化时给一个保守尺度，避免除零放大噪声
    score = max(-1.0, min(1.0, dev / (2.0 * scale)))
    trend = ""
    if len(rows) >= 4 and dev > 0.3 * scale:
        trend = "，高于自身基线，近端在转多"
    elif len(rows) >= 4 and dev < -0.3 * scale:
        trend = "，低于自身基线，近端在转空"
    return Factor("elite", "大户持仓", score, _w("elite"),
                  f"多空净比 {latest:+.3f}（14日均 {baseline:+.3f}，"
                  f"偏离 {dev:+.3f} = {-score:.2f}σ）{trend}")


def _f_orderbook(snap: Snapshot, price: float | None) -> Factor:
    if not snap.depth or not price:
        return Factor("orderbook", "盘口深度", 0.0, _w("orderbook"), "无盘口数据")
    cs = snap.contract_size
    bid = sum(q for p, q in (snap.depth.get("bids") or []) if p >= price * 0.99)
    ask = sum(q for p, q in (snap.depth.get("asks") or []) if p <= price * 1.01)
    if bid + ask <= 0:
        return Factor("orderbook", "盘口深度", 0.0, _w("orderbook"), "无盘口数据")
    ratio = bid / ask
    score = max(-1.0, min(1.0, math.log(ratio) / math.log(3))) if ratio > 0 else 0.0
    tag = "买盘占优" if ratio > 1.15 else ("卖盘占优" if ratio < 0.87 else "均衡")
    return Factor("orderbook", "盘口深度", score, _w("orderbook"),
                  f"±1% 买 {bid * cs:,.1f} / 卖 {ask * cs:,.1f} 币，比值 {ratio:.2f}（{tag}）")


def _f_volatility(snap: Snapshot) -> Factor:
    """波动率：低波动溢价。

    这是唯一经过样本外验证、且跨期符号稳定的方向信号（见 README「哪些信号真的有
    预测力」）。用"当前 ATR% 在自己近 250 日里的分位"做双向打分：
    分位低（相对自己而言很平静）-> 看多；分位高 -> 看空。

    注意它和"风险提示"是两件事：波动高时仓位本该更小，那是风控（由仓位计算
    与强平校验负责）；这里只回答方向。
    """
    k = snap.klines.get("1day") or []
    cs = [c.close for c in k]
    atr = ind.atr([c.high for c in k], [c.low for c in k], cs, 14) if len(k) > 20 else []
    atrp = [a / c if (a and c) else None for a, c in zip(atr, cs)]
    detail = []
    rv = ind.realized_vol_pct(cs, 30, bars_per_year("1day")) if len(cs) > 31 else None
    if rv is not None:
        detail.append(f"30日年化波动 {rv:.0f}%")
    cur = atrp[-1] if atrp else None
    window = [v for v in atrp[-250:] if v is not None]
    if cur is None or len(window) < 30:
        if cur and snap.price:
            detail.append(f"日ATR {cur * 100:.2f}%")
        return Factor("volatility", "波动率", 0.0, _w("volatility"),
                      "；".join(detail) or "无数据")
    rank = sum(1 for v in window if v <= cur) / len(window)   # 0=最平静 1=最躁动
    score = max(-1.0, min(1.0, 1.0 - 2.0 * rank))
    detail.append(f"日ATR {cur * 100:.2f}%，处于自身近 {len(window)} 日的 "
                  f"{rank * 100:.0f}% 分位")
    detail.append("低波动（偏多）" if score > 0.15 else
                  ("高波动（偏空）" if score < -0.15 else "波动中性"))
    return Factor("volatility", "波动率", score, _w("volatility"), "；".join(detail))


def _f_volume(snap: Snapshot) -> Factor:
    """成交量放大：另一个跨期符号稳定的正向信号（放量 -> 偏多）。"""
    k = snap.klines.get("1day") or []
    if len(k) < 25:
        return Factor("volume", "成交量", 0.0, _w("volume"), "数据不足")
    vs = [c.volume for c in k]
    base = sum(vs[-21:-1]) / 20
    if not base:
        return Factor("volume", "成交量", 0.0, _w("volume"), "无成交量数据")
    z = (vs[-1] / base) - 1.0
    score = max(-1.0, min(1.0, z / 0.8))
    tag = "放量" if score > 0.2 else ("缩量" if score < -0.2 else "量能持平")
    return Factor("volume", "成交量", score, _w("volume"),
                  f"当日 {vs[-1]:,.0f} 张，20日均 {base:,.0f} 张（{z * 100:+.0f}%，{tag}）")


# (因子函数, 是否需要现价)。只有盘口因子额外要一个 price 参数，
# 显式写在这里，比在调用处做 `fn is _f_orderbook` 的标识比较更不容易出错。
FACTOR_FNS = [
    (_f_trend, False), (_f_intraday, False), (_f_momentum, False),
    (_f_structure, False), (_f_vwap, False), (_f_funding, False),
    (_f_oi, False), (_f_elite, False), (_f_orderbook, True), (_f_volatility, False),
    (_f_volume, False),
]

# 因子内部硬编码的默认权重；config 的 analysis.factor_weights 可覆盖同名项。
#
# 短周期因子的权重被特意压低（trend_intraday 0.12→0.06 等）。原因是实测：
# trend_intraday 用 60min 的 SMA20/SMA50/MACD 三项，三者同向时就饱和到 ±1.0，
# 19 个币里 15 个吃满上限，平均得分 +0.84，平均贡献 +0.101 —— 是日线趋势
# （权重 0.20）的两倍多。于是这个"波段"模型实际由 1 小时动量主导：日线在跌、
# 小时在反弹，它就一路喊多，用户看着日线图会觉得结论是反的。
# 压低后（实测 19 币 × 1000 根）：期望 R +0.332→+0.354、总 R +1116→+1345、
# 评分为正的币 16/19→14/19，同时不再有因子独占话语权。
FACTOR_DEFAULTS = {
    # 权重来自"样本外验证过符号稳定性"的那一小组信号：
    #   波动率（低波动溢价）、成交量（放量）、MACD 弱反转
    # 其余趋势类因子（均线位置、摆动结构、VWAP、RSI）在时序切分下**符号会翻转**
    # （训练段 IC 为负、测试段转正或归零），没有稳定的方向能力，故大幅压低，
    # 只作为背景而非主导。详见 README「哪些信号真的有预测力」。
    "volatility": 0.30,
    "volume": 0.18,
    "trend_daily": 0.10, "trend_intraday": 0.04, "momentum": 0.06,
    "structure": 0.05, "vwap": 0.03, "funding": 0.08,
    "open_interest": 0.06, "elite": 0.06, "orderbook": 0.04,
}

# 多周期共振因子是可选的第 11 个因子（默认关闭，保持 10 因子模型）。
# 权重独立于 FACTOR_DEFAULTS，不进权重滑块面板。
RESONANCE_DEFAULT_WEIGHT = 0.06


def _w(name: str) -> float:
    """因子的默认权重。只认 FACTOR_DEFAULTS 这一处，避免函数里的字面量漂移。"""
    return float(FACTOR_DEFAULTS.get(name, 0.0))


def _bias_daily(snap: Snapshot) -> float:
    """日线方向偏置 [-1,1]：现价相对均线/EMA 的位置。"""
    cs = snap.closes("1day")
    if len(cs) < 30:
        return 0.0
    price = snap.price or cs[-1]
    pts, w = 0.0, 0.0
    for v, wt in ((ind.last(ind.sma(cs, 20)), 1.0),
                  (ind.last(ind.sma(cs, 50)), 1.2),
                  (ind.last(ind.sma(cs, 200)), 1.5)):
        if v:
            pts += wt if price > v else -wt
            w += wt
    e12, e26 = ind.last(ind.ema(cs, 12)), ind.last(ind.ema(cs, 26))
    if e12 and e26:
        pts += 0.8 if e12 > e26 else -0.8
        w += 0.8
    return max(-1.0, min(1.0, pts / w)) if w else 0.0


def _bias_intraday(snap: Snapshot) -> float:
    """60min 方向偏置 [-1,1]。"""
    cs = snap.closes("60min")
    if len(cs) < 30:
        return 0.0
    price = snap.price or cs[-1]
    pts, w = 0.0, 0.0
    for v, wt in ((ind.last(ind.sma(cs, 20)), 1.0), (ind.last(ind.sma(cs, 50)), 1.2)):
        if v:
            pts += wt if price > v else -wt
            w += wt
    _, _, hist = ind.macd(cs)
    h = ind.last(hist)
    if h is not None:
        pts += 0.9 if h > 0 else -0.9
        w += 0.9
    return max(-1.0, min(1.0, pts / w)) if w else 0.0


def _f_resonance(snap: Snapshot, weight: float) -> Factor:
    """多周期共振 = 日线偏置 × 60min 偏置。

    同向（都多或都空）-> 正，表示趋势共振，顺势信号更可靠；
    背离 -> 负，表示在逆着高周期做，要谨慎。
    """
    hi = _bias_daily(snap)
    lo = _bias_intraday(snap)
    score = max(-1.0, min(1.0, hi * lo))
    if hi == 0 or lo == 0:
        detail = f"共振无法判断（日线偏置 {hi:+.2f} / 1h 偏置 {lo:+.2f}）"
    else:
        same = "同向共振" if score > 0 else "周期背离"
        detail = f"{same}：日线 {hi:+.2f} × 1h {lo:+.2f}"
    return Factor("resonance", "多周期共振", score, weight, detail)


def apply_factor_weights(factors: list[Factor], weights: dict | None = None) -> list[Factor]:
    """按 config 里的权重覆盖因子的 weight（只覆盖存在且 >0 的项）。"""
    if not weights:
        return factors
    for f in factors:
        w = weights.get(f.name)
        if w is not None and float(w) > 0:
            f.weight = float(w)
    return factors


def _confidence_score(factors: list[Factor], score: float, direction: str, agree: int) -> float:
    """0-100 置信度。

    两部分相乘：
    - 一致度 = 与方向同向的因子权重占比（0-1），用权重而非个数，
      避免一个低权重因子和日线趋势被等同看待；
    - 强度 = tanh(|score|/70)，把评分饱和到 0-1。
    一致但评分不强、或评分强但因子打架，都拿不到高置信度。
    """
    total_w = sum(f.weight for f in factors if f.weight > 0) or 1.0
    if direction == "wait":
        return 0.0
    long = direction == "long"
    agree_w = sum(f.weight for f in factors
                  if abs(f.score) > 0.05 and (f.score > 0) == long)
    consensus = agree_w / total_w
    intensity = math.tanh(abs(score) / 70.0)
    return round(100.0 * consensus * intensity, 1)


def _confidence_label(cs: float) -> str:
    return "高" if cs >= 60 else ("中" if cs >= 40 else "低")


# 闸门判据。tp1 只看最近那档目标——evaluate_outcome 是"先到先算"，
# TP1 正是实际最先命中的那档，所以它天然等于"这单实际能拿到多少 R"。
# weighted / max 承认远端空间，理论上更宽松。
#
# 但 19 个币 × 1000 根 K 线回测（三档权重全试过）显示：
#   swing   tp1 +0.327R | weighted +0.310R | max +0.308R
#   intraday tp1 -0.046R | weighted -0.047R | max -0.044R
# 三者差异在噪声内，tp1 反而略优。所以默认值是 tp1 —— 换判据并不能提高期望值。
# 真正的问题不在这里，见 README「为什么全是观望」。
#
# 补充：放宽到 20 币 × 2000 根 15min（约 20 天，该周期接口上限）复测 intraday，
# 10056 笔了结单期望 -0.094R，18 个有数据的币里 14 个为负。这坐实了 intraday
# 是负期望，所以 DEFAULT_MODE（config.py）取 swing 而非 intraday。
GATE_MEASURES = ("tp1", "weighted", "max")
GATE_DEFAULT_MEASURE = "tp1"
GATE_DEFAULT_WEIGHTS = (0.5, 0.3, 0.2)
_GATE_CN = {"tp1": "第一目标", "weighted": "加权目标", "max": "最好目标"}


def _max_target_rr(gate_cfg: dict) -> float | None:
    """目标位盈亏比上限；None / 非法值 = 不限制。"""
    raw = (gate_cfg or {}).get("max_target_rr")
    if raw is None:
        return None
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def _gate_settings(gate_cfg: dict) -> tuple[str, float, tuple[float, ...]]:
    """从 config 的 analysis.gate 解出 (判据, 最低盈亏比, 权重)。"""
    name = str((gate_cfg or {}).get("measure") or GATE_DEFAULT_MEASURE).lower()
    if name not in GATE_MEASURES:
        name = GATE_DEFAULT_MEASURE
    try:
        min_rr = float((gate_cfg or {}).get("min_rr", 1.0))
    except (TypeError, ValueError):
        min_rr = 1.0
    raw_w = (gate_cfg or {}).get("weights") or GATE_DEFAULT_WEIGHTS
    try:
        w = tuple(float(x) for x in raw_w)[:3]
    except (TypeError, ValueError):
        w = GATE_DEFAULT_WEIGHTS
    if len(w) < 3:                      # 补足三档；权重全 0 时退回默认
        w = (w + GATE_DEFAULT_WEIGHTS)[:3]
    if sum(w) <= 0:
        w = GATE_DEFAULT_WEIGHTS
    return name, min_rr, w


def _target_rr(target_price: float, ref: float, risk: float, direction: str) -> float:
    """目标位盈亏比 = 目标到入场中点的距离 / 止损距离。"""
    if risk <= 0:
        return 0.0
    return (target_price - ref) / risk if direction == "long" else (ref - target_price) / risk


def _gate_measure(targets: list[dict], measure: str,
                  weights: tuple[float, ...]) -> float | None:
    """按判据把多档目标压成"这一个闸门要看的盈亏比"。

    只有一档时三种判据等价；档数不足时权重按实际档数归一，避免因为
    "只有 TP1 却按 0.5 权重算"而把数值压小。
    """
    rrs = [float(t.get("rr") or 0.0) for t in targets]
    if not rrs:
        return None
    if measure == "max":
        return max(rrs)
    if measure == "weighted":
        w = weights[:len(rrs)]
        total = sum(w)
        if total <= 0:
            return rrs[0]
        return sum(v * wi for v, wi in zip(rrs, w)) / total
    return rrs[0]


def compute_factors(snap: Snapshot, *, resonance: bool = False,
                    resonance_weight: float = RESONANCE_DEFAULT_WEIGHT,
                    trend_dampen: dict | None = None) -> list[Factor]:
    out: list[Factor] = []
    for fn, needs_price in FACTOR_FNS:
        try:
            out.append(fn(snap, snap.price) if needs_price else fn(snap))
        except Exception as exc:
            out.append(Factor(fn.__name__.replace("_f_", ""), "计算异常", 0.0, 0.0,
                              f"{type(exc).__name__}: {exc}"))
    # 权重的唯一来源是 FACTOR_DEFAULTS。"计算异常"那条 weight=0 的不动。
    # 这层兜底是为了防止再出现"函数里写死一个数、FACTOR_DEFAULTS 写另一个数"的漂移：
    # 那种情况下网页显示的是 FACTOR_DEFAULTS，实际算的却是字面量，面板会骗人。
    for f in out:
        if f.name in FACTOR_DEFAULTS:
            f.weight = float(FACTOR_DEFAULTS[f.name])
    if trend_dampen and trend_dampen.get("enabled"):
        for f in out:
            if f.name == "trend_daily":
                f.score = _dampen_trend(f.score, {"analysis": {
                    "trend_dampen": trend_dampen}})
    if resonance:
        try:
            out.append(_f_resonance(snap, resonance_weight))
        except Exception as exc:
            out.append(Factor("resonance", "计算异常", 0.0, 0.0,
                              f"{type(exc).__name__}: {exc}"))
    return out


# --------------------------------------------------------------------------
# 价位阶梯
# --------------------------------------------------------------------------

def collect_levels(snap: Snapshot, price: float, mode: str = "intraday") -> list[Level]:
    m = MODES.get(mode, MODES["intraday"])
    lb = m["level_lookback"]
    lv: list[Level] = []

    for period, n in lb.items():
        k = snap.klines.get(period)
        if not k:
            continue
        window = k[-n:]
        if len(window) >= 20:
            lv += top_volume_nodes(window, bins=36, top=4, price=price,
                                   max_dist_pct=10.0 if mode == "intraday" else 25.0)
            hs, ls = swing_points(window, 3)
            w = 0.5 if mode == "intraday" else 0.6
            for _, p in hs[-6:]:
                lv.append(Level(p, "resistance" if p > price else "support", w,
                                f"{period}摆动高点", ""))
            for _, p in ls[-6:]:
                lv.append(Level(p, "resistance" if p > price else "support", w,
                                f"{period}摆动低点", ""))

    if m.get("use_daily_fib") and snap.klines.get("1day"):
        k1 = snap.klines["1day"][-90:]
        hi, lo = max(c.high for c in k1), min(c.low for c in k1)
        if hi > lo:
            for r, p in fib_levels(hi, lo):
                lv.append(Level(p, "resistance" if p > price else "support",
                                0.5 if 0.3 <= r <= 0.7 else 0.35,
                                f"斐波{r * 100:.1f}%", f"{lo:,.2f}~{hi:,.2f}"))
    elif snap.klines.get("60min"):
        k60 = snap.klines["60min"][-72:]
        hi, lo = max(c.high for c in k60), min(c.low for c in k60)
        if hi > lo:
            for r, p in fib_levels(hi, lo, (0.382, 0.5, 0.618)):
                lv.append(Level(p, "resistance" if p > price else "support", 0.35,
                                f"1h斐波{r * 100:.1f}%", f"{lo:,.2f}~{hi:,.2f}"))

    # 均线 / VWAP
    mas: dict[str, float | None] = {}
    cs60 = snap.closes("60min")
    cs15 = snap.closes("15min")
    cs1d = snap.closes("1day")
    if len(cs15) >= 50:
        mas["MA20(15m)"] = ind.last(ind.sma(cs15, 20))
    if len(cs60) >= 50:
        mas["MA50(1h)"] = ind.last(ind.sma(cs60, 50))
    if len(cs1d) >= 20:
        mas["EMA12"] = ind.last(ind.ema(cs1d, 12))
        mas["EMA26"] = ind.last(ind.ema(cs1d, 26))
    if len(cs1d) >= 50:
        mas["SMA20"] = ind.last(ind.sma(cs1d, 20))
        mas["SMA50"] = ind.last(ind.sma(cs1d, 50))
    if len(cs1d) >= 200:
        mas["SMA200"] = ind.last(ind.sma(cs1d, 200))
    ks = snap.klines.get("5min") or []
    if len(ks) >= 30:
        day_start = (ks[-1].ts // 86400) * 86400
        st = next((i for i, c in enumerate(ks) if c.ts >= day_start), 0)
        mas["VWAP"] = ind.last(ind.vwap([c.high for c in ks], [c.low for c in ks],
                                        [c.close for c in ks], [c.volume for c in ks], start=st))
    lv += moving_average_levels(price, mas, span_pct=3.0 if mode == "intraday" else 8.0)

    lv += round_number_levels(price, span_pct=1.0 if mode == "intraday" else 2.5)
    lv += orderbook_walls(snap.depth, price, pct=1.5, contract_size=snap.contract_size)
    return merge_levels(price, lv, cluster_pct=0.10)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def _reversion_downgrade(plan: Plan, snap: Snapshot, cfg: dict, atr_v: float,
                          reason: str) -> Plan:
    """反弹通道的降级：清掉会误导人的入场/止损数据，回到观望。"""
    plan.direction = "wait"
    plan.confidence = "低"
    plan.confidence_score = 0.0
    plan.entry_note = reason
    plan.entry_low = plan.entry_high = None
    plan.stop = None
    plan.stop_pct = None
    plan.targets = []
    plan.rr = None
    plan.invalidation = ""
    account = cfg.get("account") or {}
    _attach_sizing(plan, snap, float(account.get("equity_usdt") or 0),
                   float(account.get("risk_pct_per_trade") or 2.0),
                   int(account.get("preferred_leverage") or 5),
                   float(account.get("max_margin_pct") or 50.0), atr_v)
    plan.warnings.extend(snap.errors)
    return plan


def _oversold_reversion_plan(snap: Snapshot, cfg: dict, mode: str,
                             m: dict, plan: Plan) -> Plan:
    """超卖反弹通道：只在"跌势里跌透了"时做多，目标位就是回到 SMA20。

    和 swing/intraday 的关系：**不同策略**。swing 在跌段做多没有 alpha（见 MODES
    里 "reversion" 的注释），这条通道补的是那个缺口，两者互不干扰。

    触发要同时满足三条，缺一不可：
      RSI(oversold_rsi_period) < oversold_rsi_max   跌透了（短周期超卖）
      close < SMA20 × (1 + gap)                     还没涨回来
      close < SMA200                                整体仍是跌势
    任何一条不满足都直接观望——不满足"跌透了"就不该抢反弹，不满足"还是跌势"
    就不该按反弹策略做（那是趋势策略的事，归 swing）。

    只做多。反弹没有空头对应物：跌势里追空是趋势，归 swing。
    """
    account = cfg.get("account") or {}
    _equity = float(account.get("equity_usdt") or 0)
    _risk = float(account.get("risk_pct_per_trade") or 2.0)
    _lev = int(account.get("preferred_leverage") or 5)
    _maxm = float(account.get("max_margin_pct") or 50.0)

    price = snap.price
    if not price:
        plan.notes.append("拿不到最新价，无法分析。")
        _attach_sizing(plan, snap, _equity, _risk, _lev, _maxm, None)
        plan.warnings.extend(snap.errors)
        return plan

    k = snap.klines.get(m["atr_period"]) or []
    cs = [c.close for c in k]
    rsi_n = int(m.get("oversold_rsi_period", 2))
    rsi_max = float(m.get("oversold_rsi_max", 10.0))
    gap = float(m.get("oversold_sma20_gap", 0.03))
    sma200_n = int(m.get("oversold_sma200_period", 200))

    need = max(sma200_n + 5, rsi_n + 10, 25)
    if len(cs) < need:
        plan.direction = "wait"
        plan.confidence = "低"
        plan.confidence_score = 0.0
        plan.entry_note = f"日线只有 {len(cs)} 根（需要 ≥{need}），判不了跌势与超卖。"
        _attach_sizing(plan, snap, _equity, _risk, _lev, _maxm, None)
        plan.warnings.extend(snap.errors)
        return plan

    rsi_v = ind.last(ind.rsi(cs, rsi_n))
    sma20 = ind.last(ind.sma(cs, 20))
    sma200 = ind.last(ind.sma(cs, sma200_n))
    plan.market = {
        "mode": mode, "mode_label": m["label"], "atr_period": m["atr_period"],
        "change_24h_pct": round(snap.change_24h_pct, 2) if snap.change_24h_pct is not None else None,
        "basis_pct": round(snap.basis_pct, 4) if snap.basis_pct is not None else None,
    }

    # 三条触发条件逐个记下来，面板上要能看到是卡在哪一条
    cond_rsi = rsi_v is not None and rsi_v < rsi_max
    cond_sma20 = sma20 is not None and price < sma20 * (1.0 + gap)
    cond_sma200 = sma200 is not None and price < sma200
    checks = [
        ("rsi", cond_rsi,
         f"RSI({rsi_n}) {rsi_v:.1f}" if rsi_v is not None else f"RSI({rsi_n}) 无数据"),
        ("sma20", cond_sma20,
         f"低于 SMA20 {sma20:,.4f}" if sma20 is not None else "SMA20 无数据"),
        ("sma200", cond_sma200,
         f"低于 SMA200 {sma200:,.4f}" if sma200 is not None else "SMA200 无数据"),
    ]

    if not (cond_rsi and cond_sma20 and cond_sma200):
        plan.direction = "wait"
        plan.confidence = "低"
        plan.confidence_score = 0.0
        failed = "；".join(c[2] for c in checks if not c[1])
        plan.entry_note = f"未触发反弹：{failed}。"
        _attach_sizing(plan, snap, _equity, _risk, _lev, _maxm, None)
        plan.warnings.extend(snap.errors)
        return plan

    # ---- 触发成立：做多，目标位 = SMA20 ----
    plan.direction = "long"
    rsi_txt = f"{rsi_v:.1f}"
    plan.factors = [
        Factor(f"rsi{rsi_n}", f"RSI({rsi_n}) 超卖", 1.0, 1.0,
               f"RSI({rsi_n}) {rsi_txt} < {rsi_max:.0f}（跌透）"),
        Factor("sma20", "未涨回 SMA20", 1.0, 1.0,
               f"收盘 {price:,.4f} 低于 SMA20 {sma20:,.4f}（还有回抽空间）"),
        Factor(f"sma{sma200_n}", "整体跌势", 1.0, 1.0,
               f"收盘低于 SMA{sma200_n} {sma200:,.4f}"
               f"（跌势中的反弹，不逆大势做趋势单）"),
    ]

    # ---- 从属定位：只有主策略观望时，这个通道才有资格出手 ----
    # 依据：reversion 有 63% 的触发点 ±2 根内 swing 也给了 long。那部分没有
    # 分散价值——同一个点开两张同向单只是把同一笔风险放大一倍，不是两个策略。
    # 去掉重叠后剩下的 26 笔期望 +0.709R，比全量的 +0.367R 高一倍。
    if SUBORDINATE_NEEDS_PRIMARY_SILENT:
        primary = build_plan(snap, cfg, mode=PRIMARY_MODE)
        if primary.direction not in ("wait", "short"):
            plan.direction = "wait"
            plan.confidence = "低"
            plan.confidence_score = 0.0
            plan.entry_note = (f"超卖条件成立，但主策略「{MODES[PRIMARY_MODE]['label']}」"
                               f"已给出同向 {primary.direction}，不重复开仓"
                               f"（本通道是补充，不与主策略叠加）。")
            plan.state_gate = {"subordinate_blocked": True,
                               "primary_mode": PRIMARY_MODE,
                               "primary_direction": primary.direction}
            _attach_sizing(plan, snap, _equity, _risk, _lev, _maxm, None)
            plan.warnings.extend(snap.errors)
            return plan

    # 止损：日线 ATR。数据不足时退回 2.5%
    atr_v = ind.last(ind.atr([c.high for c in k], [c.low for c in k], cs, 14)) if len(k) > 15 else None
    if not atr_v:
        atr_v = price * 0.025
    plan.market.update({
        "atr": round(atr_v, 8),
        "atr_pct": round(atr_v / price * 100, 3),
        "rsi2": round(rsi_v, 2) if rsi_v is not None else None,
        "sma20": round(sma20, 4) if sma20 is not None else None,
        "sma200": round(sma200, 4) if sma200 is not None else None,
        "regime": "跌势超卖",
    })

    # 入场：现价直接进（反弹信号不等人，等回踩就错过起点了）
    plan.entry_low = round(price, 8)
    plan.entry_high = round(price, 8)
    mid = price
    stop_dist = atr_v * m["stop_atr_mult"]
    lo_pct, hi_pct = m["stop_pct_range"]
    stop_dist = max(lo_pct / 100.0 * mid, min(hi_pct / 100.0 * mid, stop_dist))
    plan.stop = round(mid - stop_dist, 8)
    plan.stop_pct = round(stop_dist / mid * 100, 3)

    # 目标位：回到 SMA20。SMA20 不在现价上方就说明没有反弹空间可做
    if sma20 is None or sma20 <= mid:
        return _reversion_downgrade(plan, snap, cfg, atr_v,
                                    "反弹目标位（SMA20）不在现价上方，没有可交易的回抽空间。")

    rr = _target_rr(sma20, mid, stop_dist, "long")
    if rr < 1.0:
        plan.gate = {"blocked": True, "reason": "rr", "score": plan.score,
                     "would_be": "long", "rr": round(rr, 2), "need": 1.0,
                     "measure": "sma20"}
        plan.warnings.append(f"回到 SMA20 的盈亏比仅 {rr:.2f}（< 1），风险大于收益。")
        return _reversion_downgrade(plan, snap, cfg, atr_v,
                                    "盈亏比不足 1:1（反弹目标位太近），不出手。")

    plan.targets = [{"price": round(sma20, 8), "label": "TP1",
                     "rr": round(rr, 2), "source": "SMA20（反弹终点）"}]
    plan.rr = round(rr, 2)
    plan.score = round(100.0 * math.tanh(1.0 / SCORE_SCALE), 1)
    plan.invalidation = f"日线收盘跌破 {plan.stop:,.4f} 则反弹逻辑作废"
    plan.entry_note = "跌势中的超卖反弹，现价进，目标回到 SMA20。"
    plan.notes.append(f"触发：RSI({rsi_n}) {rsi_txt} / 低于 SMA20 / 低于 SMA{sma200_n}。")
    plan.notes.append(f"目标位 SMA20 {sma20:,.4f}，盈亏比 {rr:.2f}，"
                      f"止损 {stop_dist / mid * 100:.2f}%。")
    plan.notes.append("这条通道只做多：反弹没有空头对应物，跌势追空归波段模式。")

    conf = _confidence_score(plan.factors, plan.score, "long", len(plan.factors))
    plan.confidence = _confidence_label(conf)
    plan.confidence_score = conf

    account = cfg.get("account") or {}
    # 从属模式单笔风险减半：它的样本量只有主策略的 1/10、CI 下界刚过 0、
    # 且时序上不稳定（前段亏、后段赚）。拿它做等仓位赌注没依据。
    risk_scale = SUBORDINATE_RISK_SCALE
    plan.notes.append(f"从属通道：单笔风险按主策略的 {risk_scale:.0%} 计"
                      f"（样本量与稳定性均不足）。")
    _attach_sizing(plan, snap, float(account.get("equity_usdt") or 0),
                   float(account.get("risk_pct_per_trade") or 2.0) * risk_scale,
                   int(account.get("preferred_leverage") or 5),
                   float(account.get("max_margin_pct") or 50.0), atr_v)
    plan.warnings.extend(snap.errors)
    return plan


def build_plan(snap: Snapshot, cfg: dict, mode: str = DEFAULT_MODE) -> Plan:
    mode = mode if mode in MODES else DEFAULT_MODE
    m = MODES[mode]
    account = cfg.get("account") or {}
    equity = float(account.get("equity_usdt") or 0)
    risk_pct = float(account.get("risk_pct_per_trade") or 2.0)
    leverage = int(account.get("preferred_leverage") or 5)
    leverage = max(1, min(leverage, int(account.get("max_leverage") or 10)))
    max_margin_pct = float(account.get("max_margin_pct") or 50.0)
    threshold = float((cfg.get("analysis") or {}).get("score_threshold") or 30)

    plan = Plan(symbol=snap.symbol, mode=mode, price=snap.price, generated_at=_time.time())
    # 记录当前生效的参数方案（若有），胜率统计按方案分组时用
    plan.profile = cfg.get("active_profile") or ""
    _an = cfg.get("analysis") or {}
    plan.risk_mult_long = float(_an.get("risk_mult_long") or 1.0)
    plan.risk_mult_short = float(_an.get("risk_mult_short") or 1.0)
    plan.costs_cfg = dict(_an.get("backtest") or {})
    if not snap.price:
        plan.notes.append("拿不到最新价，无法分析。")
        plan.warnings.extend(snap.errors)
        # 显式标注原因：否则闸门漏斗会把"没数据"算成"评分不够"，
        # 让人误以为是因子问题。
        plan.gate = {"blocked": True, "reason": "no_price",
                     "score": 0.0, "would_be": "wait"}
        return plan

    # 超卖反弹通道走独立判据，不进多因子打分/闸门那条路
    if mode == "reversion":
        out = _oversold_reversion_plan(snap, cfg, mode, m, plan)
        # 完整历史（2000 根日线 / 20 币）证明该信号原始收益为 0、扣成本后为负，
        # 所以在任何输出上都挂一条明确警告，避免被当成正期望策略下单。
        out.warnings.append(
            "超卖反弹模式在完整历史回测中无正期望（原始收益≈0，扣成本后为负），"
            "仅供研究，不建议实盘。主策略请用 swing。")
        return out

    price = snap.price
    an = cfg.get("analysis") or {}
    res_cfg = an.get("resonance") or {}
    factors = apply_factor_weights(
        compute_factors(snap,
                        resonance=bool(res_cfg.get("enabled")),
                        resonance_weight=float(res_cfg.get("weight") or RESONANCE_DEFAULT_WEIGHT),
                        trend_dampen=an.get("trend_dampen")),
        an.get("factor_weights"))
    plan.factors = factors
    raw = sum(f.score * f.weight for f in factors)
    total_w = sum(f.weight for f in factors if f.weight > 0) or 1.0
    # 加权平均天然向 0 收缩（因子之间经常互相抵消），线性映射会让评分永远落在
    # ±30 内、阈值形同虚设。tanh 压缩后中间区域被放大，极端区域饱和。
    #
    # 注意：这只是"11 个因子的加权平均"，**不是**决定方向的分数。
    # 方向用的是后面的 dir_score（状态因子定观点 + 趋势因子只做确认）。
    # 以前一直把这两个数混用：对外显示/排序用这个平均值，阈值却判 dir_score，
    # 结果出现过"评分 -84 却观望"（24 个币里 9 个）。现在平均值单独保存，
    # plan.score 只放真正决定方向的 dir_score。
    score_all = round(100.0 * math.tanh((raw / total_w) / SCORE_SCALE), 1)
    plan.score_all_factors = score_all

    # ---- 波动率与市场状态 ----
    atr_k = snap.klines.get(m["atr_period"]) or []
    atr_v = None
    if len(atr_k) > 15:
        atr_v = ind.last(ind.atr([c.high for c in atr_k], [c.low for c in atr_k],
                                 [c.close for c in atr_k], 14))
    k60 = snap.klines.get("60min", [])
    adx_v = ind.last(ind.adx([c.high for c in k60], [c.low for c in k60],
                             [c.close for c in k60], 14)) if len(k60) > 30 else None
    rv = ind.realized_vol_pct(snap.closes("1day"), 30, 365) if len(snap.closes("1day")) > 31 else None
    plan.market = {
        "mode": mode, "mode_label": m["label"],
        "threshold": threshold,
        "atr_period": m["atr_period"],
        "atr": round(atr_v, 8) if atr_v else None,
        "atr_pct": round(atr_v / price * 100, 3) if atr_v else None,
        "adx": round(adx_v, 1) if adx_v is not None else None,
        "regime": ("趋势" if (adx_v or 0) >= 25 else ("震荡" if (adx_v or 0) < 20 else "过渡")),
        "rv_30d_ann_pct": round(rv, 1) if rv is not None else None,
        "change_24h_pct": round(snap.change_24h_pct, 2) if snap.change_24h_pct is not None else None,
        "basis_pct": round(snap.basis_pct, 4) if snap.basis_pct is not None else None,
    }

    # 流动性体检：成交额 / 买卖价差。滑点模型假设的 2bps 在薄合约上根本不
    # 成立，所以先把"这个合约能不能做得进"单独说清楚；enforce 开启时直接拦。
    liq = assess_liquidity(snap, cfg)
    plan.market["liquidity"] = liq
    if liq["reasons"]:
        plan.warnings.append("；".join(liq["reasons"]))
        if liq["enforce"]:
            _attach_sizing(plan, snap, equity, risk_pct, leverage,
                           max_margin_pct, atr_v)
            plan.direction = "wait"
            plan.confidence = "低"
            plan.confidence_score = 0.0
            plan.entry_note = "流动性不足，本单不做（analysis.liquidity.enforce 已开启）。"
            plan.state_gate = {
                "liquidity": True,
                "reasons": list(liq["reasons"]),
                "reason": "成交额或买卖价差不达标，实盘进出都会滑。",
            }
            return plan

    if not atr_v:
        plan.direction = "wait"
        plan.notes.append(f"缺少 {m['atr_period']} 数据，无法计算止损距离。")
        plan.warnings.extend(snap.errors)
        plan.gate = {"blocked": True, "reason": "no_atr",
                     "score": 0.0, "would_be": "wait"}
        return plan

    # ---- 价位阶梯 ----
    all_levels = collect_levels(snap, price, mode)
    min_dist = atr_v * m["min_dist_atr"]
    max_dist = atr_v * m["max_target_atr"]
    up = price_ladder(price, all_levels, min_dist=min_dist, max_dist=max_dist, direction="up")
    dn = price_ladder(price, all_levels, min_dist=min_dist, max_dist=max_dist, direction="down")
    plan.levels = {"resistance": up, "support": dn}

    # ---- 方向判据：状态因子定观点，趋势因子只确认 ----
    # 旧版是 plan.score >= threshold -> long，一个标量符号定生死。
    # 实测问题（20 币 × 2060 点，5 日持有，时序切分 60/40）：
    #   train（跌段）long 命中 41.3%、short 57.0%
    #   test （涨段）long 命中 52.7%、short 50.4%
    #   score [+30,+60) 档在跌段只有 38.3% 上涨、在涨段 51.3%
    # 也就是：模型永远站在当前趋势那一侧，趋势一反转就整体反向。
    # 压掉全部负 IC 因子，train 段 long 命中仍是 41.3% 不动 —— 不是因子注反，
    # 是"趋势确认因子"在反转点给出强信号，盖过"状态因子"的弱信号。
    #
    # 拆开两类：状态因子（波动率/成交量/资金费，IC 两段符号稳定）决定观点，
    # 趋势确认因子（IC 两段翻转）只做确认、不能发起。
    # 注意闸门必须是**双向**的：低波动只是"偏多"，不是"唯一可交易状态"。
    # 若把闸门写成"状态分必须 > 0"，下跌（高波动）里就永远不许做空，
    # 而 train 段 short 恰恰是全程唯一正命中的一侧。
    state_names = ("volatility", "volume", "funding")
    trend_names = ("trend_daily", "trend_intraday", "momentum", "structure")
    sf = {f.name: f for f in factors}
    # 缺数据的因子必须**不参与**合成，不能按 0 分算进去。
    #
    # 根因：交易所的 history/kline 只给最近 2000 根，5min 只覆盖 6.9 天、
    # 60min 只覆盖 83 天，而日线覆盖 5.5 年。于是历史回测里 96.9% 的
    # 检查点只有 5 个因子有真实数据，其余 6 个（0.42 权重）全是 0 分。
    # 按 0 分参与加权平均，等于把"没意见"当成"中性偏空"——方向被稀释，
    # |评分| 掉到阈值以下的检查点从 27.8% 涨到 38.5%。实盘不这样（数据全在），
    # 所以回测分数比实盘低、比实盘保守，是个静默失真。
    # 用"该因子的分数恰好为 0 且详情标注了缺失"来识别，不用字符串猜。
    dead_all = _dead_factor_names(sf)
    dead = set(dead_all)
    # 是否剔除缺数据的因子由配置决定。默认不剔：实测剔除会放大 |评分|，
    # 让阈值 30 变相放松、放出边际更差的单子（期望 +0.209R -> +0.167R）。
    # 保留开关是为了能复现这个对比，不是为了默认开启。
    if not bool((an.get("factor_normalize") or {}).get("enabled")):
        dead = set()
    state_raw = sum(sf[n].score * sf[n].weight for n in state_names
                    if n in sf and n not in dead)
    state_w = sum(sf[n].weight for n in state_names if n in sf and n not in dead) or 1.0
    state_score = state_raw / state_w
    trend_raw = sum(sf[n].score * sf[n].weight for n in trend_names
                    if n in sf and n not in dead)
    trend_w = sum(sf[n].weight for n in trend_names if n in sf and n not in dead) or 1.0

    dir_raw = state_raw + TREND_CONFIRM_SCALE * trend_raw
    dir_total = state_w + TREND_CONFIRM_SCALE * trend_w
    dir_mean = dir_raw / dir_total
    dir_score = round(100.0 * math.tanh(dir_mean / SCORE_SCALE), 1)

    # plan.score 必须就是决定方向的这个数。对外显示、评分排序诊断、
    # 分数带闸门都应该跟阈值看同一个尺度，否则会出现"评分 -84 却观望"
    # 这种自相矛盾的输出（实测 24 个币里 9 个）。
    plan.score = dir_score

    # 覆盖率始终按"真实有数据"算，与是否归一化无关——它描述的是事实，
    # 不是策略选择。只有 state_names + trend_names 这 7 个真正进入
    # dir_score（vwap/oi/elite/orderbook 不参与方向），所以分母取这 7 个
    # 的最大加权和，否则覆盖率永远到不了 1。
    live_state = sum(sf[n].weight for n in state_names
                     if n in sf and n not in dead_all)
    live_trend = sum(sf[n].weight for n in trend_names
                     if n in sf and n not in dead_all)
    max_state = sum(sf[n].weight for n in state_names if n in sf)
    max_trend = sum(sf[n].weight for n in trend_names if n in sf)
    max_total = max_state + TREND_CONFIRM_SCALE * max_trend
    live_total = live_state + TREND_CONFIRM_SCALE * live_trend
    plan.factor_coverage = (round(min(1.0, live_total / max_total), 3)
                            if max_total > 0 else 0.0)
    plan.dead_factors = sorted(dead_all)

    # ---- 趋势对齐闸门：只做与日线趋势同向的单 ----
    # 由来：之前一直想修"跌段做多亏"，五种候选闸门（price vs SMA 锚、状态与趋势
    # 同向、纯趋势跟随、long 需确认、rr 分桶）在 300 根窗口下全失败。放回 2000 根
    # 日线（约 5.5 年）再测，发现真正的浪费在逆势 long：逆势 long n=1600 只有
    # +0.085R（2021-2022 是 -0.002R），逆势 short 反而是 +0.312R。
    # 也就是该砍的是"下跌里做多"，不是所有逆势单。
    #
    # 实测（20 币 × 2000 根日线，真实规则逐笔）：
    #   swing 原样                n=5415  期望 +0.183R
    #     其中 long               n=2799  期望 +0.119R  ← 拖后腿的在这里
    #     其中 short              n=2616  期望 +0.252R
    #   只保留与趋势同向          n=2941  期望 +0.199R
    #     其中 long               n=1199  期望 +0.165R  ← 提升最明显
    #     其中 short              n=1742  期望 +0.222R
    # 砍掉的那部分逆势单总体 +0.165R，看着不亏，但它把 long 从 +0.119 稀释到 +0.085；
    # 而同向过滤把 long 提到 +0.165、总体提到 +0.199。
    #
    # 注意这**不是**把 trend_daily 提成方向源：它只是"否决权"（veto），
    # 方向仍然由状态因子 + 趋势确认因子共同决定。趋势因子只在方向已定后，
    # 检查它是否强烈反对，强烈反对就降级为观望。
    trend_veto = bool(an.get("trend_veto", False))
    trend_daily_score = sf.get("trend_daily")
    trend_daily_v = trend_daily_score.score if trend_daily_score else 0.0

    if abs(state_score) < STATE_GATE_MIN:
        # 状态因子没有观点（既不明显偏多也不明显偏空）-> 不靠趋势因子单独开仓
        plan.direction = "wait"
        plan.state_gate = {
            "state_score": round(state_score, 3),
            "threshold": STATE_GATE_MIN,
            "reason": "横截面状态无明确观点（波动率/成交量/资金费都在中性区），不靠趋势因子单独开仓",
        }
    elif dir_score >= threshold:
        plan.direction = "long"
    elif dir_score <= -threshold:
        plan.direction = "short"
    else:
        plan.direction = "wait"

    # ---- 分数带闸门：只接受 |评分| 落在甜点区间的信号 ----
    # 由来是排序诊断：高分档期望反而最低（+0.111R），中分档最好（+0.288R）。
    # 所以"分数越高越该做"不成立，与其调阈值（调高会选中最差那档），
    # 不如直接圈定甜点区间。低于 min 说明信号本身不够强，高于 max
    # 说明趋势已经走过头，两头都不做。
    band = an.get("score_band") or {}
    if (band.get("enabled") and plan.direction != "wait"
            and abs(plan.score) > 0):
        lo = float(band.get("min") or 0.0)
        hi = float(band.get("max") or 0.0)
        mag = abs(plan.score)
        if mag < lo or mag > hi:
            plan.direction = "wait"
            plan.state_gate = {
                "score_band": True,
                "score": round(plan.score, 2),
                "band": [lo, hi],
                "reason": (f"评分 {mag:.1f} 落在甜点区间 {lo:.0f}–{hi:.0f} 之外"
                           f"（{'分数偏高，趋势可能已走过头' if mag > hi else '分数偏低，信号不够强'}），"
                           f"历史上这两头的期望都不如区间内。"),
            }
            plan.confidence = "低"
            plan.confidence_score = 0.0
            plan.entry_note = plan.state_gate["reason"]
            plan.warnings.extend(snap.errors)
            _attach_sizing(plan, snap, equity, risk_pct, leverage,
                           max_margin_pct, atr_v)
            return plan

    # 否决只落在"方向已定、且日线趋势明确反对"时（阈值 0.2 = 趋势因子轻微偏向不算）
    if trend_veto and plan.direction != "wait" and abs(trend_daily_v) > 0.2:
        opposed = ((plan.direction == "long" and trend_daily_v < 0)
                   or (plan.direction == "short" and trend_daily_v > 0))
        if opposed:
            side_cn = "多" if plan.direction == "long" else "空"
            plan.direction = "wait"
            plan.state_gate = {
                "trend_veto": True,
                "trend_daily": round(trend_daily_v, 3),
                "reason": (f"逆日线趋势（trend_daily {trend_daily_v:+.2f}）做{side_cn}，"
                           f"完整历史上逆势单会稀释期望，降级观望。"),
            }
            plan.confidence = "低"
            plan.confidence_score = 0.0
            plan.entry_note = "逆日线趋势，本单不做（趋势否决闸门）。"
            plan.warnings.extend(snap.errors)
            _attach_sizing(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v)
            return plan

    stop_mult = m["stop_atr_mult"]
    buf = atr_v * stop_mult

    if plan.direction == "wait":
        plan.confidence = "低"
        plan.confidence_score = 0.0
        plan.entry_note = f"评分 {plan.score:+.0f} 未达到 ±{threshold:.0f} 阈值，方向不明。"
        if dn and up:
            plan.entry_note += f" 关注区间 {dn[0]['price']:,.2f} – {up[0]['price']:,.2f}。"
            plan.invalidation = (f"站稳 {up[0]['price']:,.2f} 转多；"
                                 f"跌破 {dn[0]['price']:,.2f} 转空。")
        plan.warnings.extend(snap.errors)
        _attach_sizing(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v)
        return plan

    # ---- 入场 / 止损 / 止盈 ----
    # 顺序很重要：入场带与止损先"定稿"（含夹紧与最小宽度），再用最终的入场中点
    # mid 算目标位的盈亏比。之前筛选目标位用的是临时的 entry_low，而判定用的是
    # mid（两者差 0.175×ATR），同一个目标位在筛选和计时用的是两把尺子，
    # 这才冒出 TP1_RR = 0.66 这种低于筛选门槛的目标。
    if plan.direction == "long":
        support = dn[0]["price"] if dn else None
        # 支撑离现价合适就挂到支撑上，否则用现价下方一小段回撤位
        if support and (price - support) <= atr_v * 1.2:
            base = support + atr_v * 0.10
        else:
            base = price - atr_v * 0.30
        plan.entry_low = round(base, 8)
        plan.entry_high = round(base + atr_v * 0.35, 8)
        raw_stop = base - buf
        ladder = up
    else:
        resist = up[0]["price"] if up else None
        if resist and (resist - price) <= atr_v * 1.2:
            base = resist - atr_v * 0.10
        else:
            base = price + atr_v * 0.30
        plan.entry_high = round(base, 8)
        plan.entry_low = round(base - atr_v * 0.35, 8)
        raw_stop = base + buf
        ladder = dn

    # ---- 止损距离夹紧：太窄容易被噪声打掉，太宽则仓位小到没意义 ----
    lo_pct, hi_pct = m["stop_pct_range"]
    mid = (plan.entry_low + plan.entry_high) / 2
    if plan.direction == "long":
        stop_dist = mid - raw_stop
    else:
        stop_dist = raw_stop - mid
    clamped_lo, clamped_hi = mid * lo_pct / 100.0, mid * hi_pct / 100.0
    if stop_dist < clamped_lo:
        plan.warnings.append(
            f"原始止损仅 {stop_dist / mid * 100:.2f}%（低于 {lo_pct}%），"
            f"已被放宽到 {lo_pct}% 以避免被噪声扫掉。")
        stop_dist = clamped_lo
    elif stop_dist > clamped_hi:
        plan.warnings.append(
            f"原始止损 {stop_dist / mid * 100:.2f}% 过宽（超过 {hi_pct}%），"
            f"已收紧到 {hi_pct}%，请以更近的结构位为准。")
        stop_dist = clamped_hi
    if plan.direction == "long":
        plan.stop = round(mid - stop_dist, 8)
    else:
        plan.stop = round(mid + stop_dist, 8)

    # ---- 入场带最小宽度：避免出现 0.02% 这种挂不进去的区间 ----
    min_w = mid * m["entry_width_pct"] / 100.0
    width = abs(plan.entry_high - plan.entry_low)
    if width < min_w:
        half = min_w / 2
        plan.entry_low = round(mid - half, 8)
        plan.entry_high = round(mid + half, 8)
        # 入场带变了，中点得跟着动，否则盈亏比基准还是旧值
        mid = (plan.entry_low + plan.entry_high) / 2

    # ---- 目标位：用最终的 mid 与止损距离算盈亏比（筛选和判定同一把尺子）----
    risk = abs(mid - plan.stop)
    gate_cfg = (cfg.get("analysis") or {}).get("gate") or {}
    try:
        floor = float(gate_cfg.get("min_target_rr", 0.8))
    except (TypeError, ValueError):
        floor = 0.8
    cands = []
    for t in ladder:
        rr = _target_rr(t["price"], mid, risk, plan.direction)
        if rr >= floor:
            cands.append((rr, t))
    # 按"先到的先算"排序：RR 越小离得越近
    cands.sort(key=lambda x: x[0])

    # ---- 目标位上限：最近的合格结构位都超过上限就不做 ----
    # TP1 取的是"最近的"合格位，所以超上限说明前方没有近距离结构可依托，
    # 不是我们挑远了，是只有远的可选。截断只能靠不做，挪近目标是自欺欺人。
    # 详见 config.py 里 analysis.gate.max_target_rr 的实测表。
    cap_rr = _max_target_rr(gate_cfg)
    if cap_rr is not None and cands and cands[0][0] > cap_rr:
        near = cands[0][0]
        plan.warnings.append(
            f"最近的目标位也在 {near:.2f}R 之外（上限 {cap_rr:.2f}R），"
            f"前方没有近距离结构可依托，这单要走出很远才回本。")
        plan.gate = {"blocked": True, "reason": "target_too_far",
                     "score": plan.score, "would_be": plan.direction,
                     "rr": round(near, 2), "need": cap_rr}
        return _downgrade(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v,
                          f"最近目标 {near:.2f}R 超过上限 {cap_rr:.2f}R"
                          f"（无近距离结构），不出手。")

    for i, (rr, t) in enumerate(cands[:3], 1):
        plan.targets.append({"price": t["price"], "label": f"TP{i}",
                             "rr": round(rr, 2), "source": t.get("source", "")})

    if plan.direction == "long":
        plan.invalidation = f"日线收盘跌破 {plan.stop:,.2f} 则多头逻辑作废"
        plan.entry_note = ("现价已进入入场区，可挂单或直接进。" if price <= plan.entry_high
                           else f"等回踩到 {plan.entry_low:,.2f}–{plan.entry_high:,.2f} 再进，现价追多风险高。")
    else:
        plan.invalidation = f"日线收盘站上 {plan.stop:,.2f} 则空头逻辑作废"
        plan.entry_note = ("现价已进入入场区，可挂单或直接进。" if price >= plan.entry_low
                           else f"等反抽到 {plan.entry_low:,.2f}–{plan.entry_high:,.2f} 再进，现价追空风险高。")

    plan.stop_pct = round(stop_dist / mid * 100, 3)
    plan.rr = plan.targets[0]["rr"] if plan.targets else None

    # ---- 闸门：只看目标位过不过关，不用再重算一遍（RR 已是最终值）----
    measure_name, min_rr, measure_w = _gate_settings(gate_cfg)
    measure_rr = _gate_measure(plan.targets, measure_name, measure_w)
    if not plan.targets:
        plan.warnings.append(
            f"{m['label']}级别找不到 ≥{floor} 盈亏比的目标位（价格正卡在关键位中间）。")
        plan.gate = {"blocked": True, "reason": "no_target", "score": plan.score,
                     "would_be": plan.direction, "rr": None, "need": floor}
        return _downgrade(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v,
                          "评分有方向，但前方没有像样的空间，暂不出手。")
    if measure_rr < min_rr:
        cn = _GATE_CN.get(measure_name, measure_name)
        plan.warnings.append(f"调整止损后{cn}盈亏比 {round(measure_rr, 2)}（< {min_rr}），风险大于收益。")
        plan.gate = {"blocked": True, "reason": "rr", "score": plan.score,
                     "would_be": plan.direction, "rr": round(measure_rr, 2), "need": min_rr,
                     "measure": measure_name}
        return _downgrade(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v,
                          "盈亏比不足 1:1，不出手。")
    if measure_rr < min_rr + 0.2:
        cn = _GATE_CN.get(measure_name, measure_name)
        plan.warnings.append(f"{cn}盈亏比仅 {round(measure_rr, 2)}，性价比一般，建议等更好的位置。")

    agree = sum(1 for f in factors
                if abs(f.score) > 0.1 and (f.score > 0) == (plan.direction == "long"))
    strong = sum(1 for f in factors if abs(f.score) > 0.4)
    conf_score = _confidence_score(factors, plan.score, plan.direction, agree)
    plan.confidence = _confidence_label(conf_score)
    plan.confidence_score = conf_score
    plan.notes.append(f"{len(factors)} 个因子中 {agree} 个与方向一致，其中 {strong} 个为强信号。")
    plan.notes.append(f"信号强度：{'很强' if abs(plan.score) >= 70 else ('较强' if abs(plan.score) >= 50 else '一般')}"
                      f"；止损用 {stop_mult}×ATR({m['atr_period']})。")
    plan.notes.append(f"闸门判据：{_GATE_CN.get(measure_name, measure_name)} "
                      f"{round(measure_rr, 2)} ≥ {min_rr}。")
    if any(f.name == "volatility" and f.score < 0 for f in factors):
        plan.warnings.append("当前波动率偏高，建议把杠杆降到 5 倍以内。")

    _attach_sizing(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v)
    plan.warnings.extend(snap.errors)
    return plan


def _attach_sizing(plan: Plan, snap: Snapshot, equity: float, risk_pct: float,
                   leverage: int, max_margin_pct: float, atr_v: float | None) -> None:
    """按"单笔亏损不超过账户 risk_pct%"反推张数。"""
    if plan.direction == "wait" or not plan.stop or not plan.price or equity <= 0:
        plan.sizing = {"applicable": False,
                       "note": "没有明确方向，暂不给仓位。" if plan.direction == "wait"
                               else "缺少保证金参数。"}
        return
    mid = (plan.entry_low + plan.entry_high) / 2 if plan.entry_low and plan.entry_high else plan.price
    stop_dist = abs(mid - plan.stop)
    if stop_dist <= 0:
        plan.sizing = {"applicable": False, "note": "止损距离为 0。"}
        return

    cs = snap.contract_size or 0.001
    # 多空不对称：空单在历史上两个方向都赚钱，多单只在上涨市赚钱，
    # 所以允许按方向给不同的单笔风险。默认两边都是 1.0（不改变原行为）。
    if plan.direction == "long":
        mult = float(getattr(plan, "risk_mult_long", 1.0) or 1.0)
    elif plan.direction == "short":
        mult = float(getattr(plan, "risk_mult_short", 1.0) or 1.0)
    else:
        mult = 1.0
    risk_amount = equity * risk_pct / 100.0 * mult
    contracts = max(1.0, math.floor(risk_amount / (stop_dist * cs)))
    notional = contracts * cs * mid
    margin = notional / leverage
    max_margin = equity * max_margin_pct / 100.0
    reduced = False
    if margin > max_margin:
        contracts = max(1.0, math.floor(max_margin * leverage / (cs * mid)))
        notional = contracts * cs * mid
        margin = notional / leverage
        reduced = True

    loss = contracts * cs * stop_dist
    # 成本预估：把"这笔单子真的会花多少"直接摊开，而不是藏在回测里。
    # 反推用的是本单的实际名义价值与 24h 成交额（没有成交额就不给冲击估计）。
    bc = getattr(plan, "costs_cfg", {}) or {}
    try:
        from .costs import trade_cost_bps as _tcb
        plan.sizing["costs"] = _tcb(
            notional, float(snap.turnover_24h or 0.0),
            taker_fee_bps=float(bc.get("taker_fee_bps", 5.0)),
            maker_fee_bps=float(bc.get("maker_fee_bps", 2.0)),
            slippage_bps=float(bc.get("slippage_bps", 2.0)),
            impact_coeff_bps=float(bc.get("impact_coeff_bps", 30.0)),
            maker=bool(bc.get("maker", False)))
    except Exception:      # 成本预估失败不该拦住下单计划
        pass
    tps = []
    for t in plan.targets:
        gain = contracts * cs * abs(t["price"] - mid)
        tps.append({"label": t["label"], "price": t["price"],
                    "profit_usdt": round(gain, 2),
                    "profit_pct_of_equity": round(gain / equity * 100, 2),
                    "rr": t["rr"]})

    # ---- 强平价校验：杠杆高到止损形同虚设时自动降杠杆并告警 ----
    liq = liquidation_price(mid, leverage, plan.direction)
    stop_pct_eff = abs(mid - plan.stop) / mid * 100 if mid else 0.0
    lev_capped = False
    if leverage > 1 and liq is not None:
        ok, _ = stop_is_effective(mid, plan.stop, leverage, plan.direction)
        if not ok:
            safe = int(safe_max_leverage(stop_pct_eff, plan.direction))
            safe = max(1, min(leverage - 1, safe))
            if safe < leverage:
                lev_capped = True
                plan.warnings.append(
                    f"{leverage}x 杠杆下强平价 {liq:,.4f} 比止损 {plan.stop:,.4f} 更靠近入场，"
                    f"止损不会被执行。已自动降到 {safe}x（如需原杠杆，请把止损放宽或降杠杆）。")
                leverage = safe
                margin = notional / leverage

    plan.sizing = {
        "applicable": True,
        "equity_usdt": equity,
        "risk_pct": risk_pct,
        "risk_mult": round(mult, 3),
        "risk_pct_effective": round(risk_pct * mult, 3),
        "risk_amount_usdt": round(risk_amount, 2),
        "contracts": int(contracts),
        "contract_size": cs,
        "position_coin": round(contracts * cs, 8),
        "notional_usdt": round(notional, 2),
        "leverage": leverage,
        "leverage_capped": lev_capped,
        "liquidation_price": round(liq, 8) if liq else None,
        "margin_usdt": round(margin, 2),
        "margin_pct_of_equity": round(margin / equity * 100, 2),
        "stop_distance": round(stop_dist, 8),
        "loss_if_stopped_usdt": round(loss, 2),
        "loss_pct_of_equity": round(loss / equity * 100, 2),
        "targets": tps,
        "note": ("保证金超出上限，已自动缩减张数。" if reduced
                 else f"单笔亏损控制在账户 {risk_pct:.1f}% 以内。"),
        "fee_estimate_usdt": round(notional * 0.0005 * 2, 3),
    }


def _downgrade(plan: Plan, snap: Snapshot, equity: float, risk_pct: float,
               leverage: int, max_margin_pct: float, atr_v: float | None,
               reason: str) -> Plan:
    """把计划降级为观望，并清掉会误导人的入场/止损数据。"""
    plan.direction = "wait"
    plan.confidence = "低"
    plan.confidence_score = 0.0
    plan.entry_note = reason
    plan.entry_low = plan.entry_high = None
    plan.stop = None
    plan.stop_pct = None
    plan.targets = []
    plan.rr = None
    plan.invalidation = ""
    _attach_sizing(plan, snap, equity, risk_pct, leverage, max_margin_pct, atr_v)
    plan.warnings.extend(snap.errors)
    return plan
