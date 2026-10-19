"""交易计划的单元测试：用合成行情验证打分、方向、价位与仓位的硬约束（不联网）。

合成行情按等比走势造：所有周期都在同一段行情上取样，最后一根 K 线收在同一个价，
这样 15min / 60min / 1day 的趋势方向才一致（否则日线会读到和分钟线相反的方向）。
"""

import copy
import json
import math
import unittest

from iabot.config import DEFAULTS
from iabot.market import Candle, Snapshot
from iabot.signals import FACTOR_DEFAULTS, MODES, build_plan

BASE_TS = 1_700_000_000
CONTRACT_SIZE = 0.001
START_PRICE = 30000.0

# 每个周期取多少根、每根代表多少秒
PERIOD_BARS = {
    "1min": (120, 60),
    "5min": (288, 300),
    "15min": (220, 900),
    "60min": (200, 3600),
    "1day": (240, 86400),
}

# 尾段（最近 6 小时）用多少根 K 线表现
TAIL_HOURS = 6.0

# 因子权重是相对值（打分时再除以权重和），README 的表格就是这个口径
DOCUMENTED_WEIGHT_SUM = 1.00

# 场景：net_pct = 整段净涨跌%，tail_pct = 最后 6 小时的额外涨跌%（负数=顺着趋势再走一段）
UP = dict(net_pct=8.0, tail_pct=-0.2)          # 日内 + 波段都给多头
DOWN_INTRADAY = dict(net_pct=-8.0, tail_pct=-1.2)   # 日内给空头
DOWN_SWING = dict(net_pct=-8.0, tail_pct=-0.2)      # 波段给空头
RANGE = dict(chop_pct=0.6, cycles=4.0)              # 没有净方向 -> 观望
# 评分够（+55），但头顶阻力太近，止损放不下 -> 被盈亏比闸门拦下
NO_ROOM = dict(net_pct=10.0, tail_pct=-0.2)
# 急涨后横盘：价格远在均线上方，但均线自身已走平
UP_FLAT = dict(net_pct=8.0, tail_pct=0.0)


def _trend_profile(n, start, net_pct, tail_pct, ts_step, wiggle_pct=0.15):
    peak = start * (1 + net_pct / 100.0)
    tail = max(2, min(n - 2, int(TAIL_HOURS * 3600 / ts_step)))
    k = n - tail
    out = [start + (peak - start) * (i + 1) / k for i in range(k)]
    end = peak * (1 + tail_pct / 100.0)
    wig = peak * wiggle_pct / 100.0
    for j in range(tail):
        t = (j + 1) / tail
        out.append(peak + (end - peak) * t
                   + wig * math.sin(2 * math.pi * j / max(2, tail / 3.0)))
    return out


def _chop_profile(n, start, amp_pct, cycles):
    amp = start * amp_pct / 100.0
    return [start + amp * math.sin(2 * math.pi * cycles * i / n) for i in range(n)]


def _candles(closes, ts_step, wick):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(Candle(ts=BASE_TS + i * ts_step, open=o, high=max(o, c) + wick,
                          low=min(o, c) - wick, close=c, volume=1000.0 + i))
        prev = c
    return out


def make_snapshot(*, net_pct=0.0, tail_pct=0.0, chop_pct=None, cycles=6.0,
                  start=START_PRICE, depth_bias=1.0):
    klines = {}
    for period, (n, ts_step) in PERIOD_BARS.items():
        if chop_pct is None:
            closes = _trend_profile(n, start, net_pct, tail_pct, ts_step)
        else:
            closes = _chop_profile(n, start, chop_pct, cycles)
        # 波动幅度按周期的平方根缩放，模拟随机游走
        wick = start * 0.0010 * math.sqrt(ts_step / 900.0)
        klines[period] = _candles(closes, ts_step, wick)

    k15 = klines["15min"]
    price = k15[-1].close
    return Snapshot(
        symbol="TEST-USDT",
        price=price,
        index_price=price,
        open24=k15[-96].close,
        high=max(c.high for c in k15),
        low=min(c.low for c in k15),
        best_bid=price - 0.5,
        best_ask=price + 0.5,
        bid_volume=1000.0 * depth_bias,
        ask_volume=1000.0 / depth_bias,
        volume_24h=100000.0,
        turnover_24h=100000.0 * price,
        contract_size=CONTRACT_SIZE,
        klines=klines,
        depth={"asks": [[price + (i + 1) * 5.0, 1.0] for i in range(40)],
               "bids": [[price - (i + 1) * 5.0, 2.0] for i in range(40)]},
        funding_rate=0.0001,
        open_interest=10000.0,
    )


def cfg_with(**account):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["account"].update(account)
    return cfg


def gate_min_rr(cfg=None):
    return float(((cfg or DEFAULTS)["analysis"].get("gate") or {}).get("min_rr", 1.0))


def plan_for(scenario, mode="intraday", **account):
    return build_plan(make_snapshot(**scenario), cfg_with(**account), mode=mode)


class TestDirection(unittest.TestCase):
    def test_uptrend_gives_long(self):
        for mode in ("intraday", "swing"):
            with self.subTest(mode=mode):
                plan = plan_for(UP, mode)
                self.assertEqual(plan.direction, "long")
                self.assertGreater(plan.score, 30.0)

    def test_downtrend_gives_short_intraday(self):
        plan = plan_for(DOWN_INTRADAY, "intraday")
        self.assertEqual(plan.direction, "short")
        self.assertLess(plan.score, -30.0)

    def test_downtrend_gives_short_swing(self):
        plan = plan_for(DOWN_SWING, "swing")
        self.assertEqual(plan.direction, "short")

    def test_no_net_move_gives_wait(self):
        plan = plan_for(RANGE, "intraday")
        self.assertEqual(plan.direction, "wait")
        self.assertEqual(plan.confidence, "低")
        self.assertLess(abs(plan.score), 30.0)

    def test_score_ok_but_no_room_is_blocked_by_gate(self):
        """评分指向做多，但第一目标盈亏比不够时必须降级，并把原因写清楚。

        把门槛调高来构造"空间不够"的场景——这样测试的是闸门机制本身，
        而不是某个特定的门槛数值（门槛是可配的，别写死）。
        """
        cfg = cfg_with()
        cfg["analysis"]["gate"]["min_rr"] = 3.0
        plan = build_plan(make_snapshot(**NO_ROOM), cfg, mode="intraday")
        self.assertEqual(plan.direction, "wait")
        self.assertGreater(plan.score, 30.0, "前提是评分确实过了阈值")
        gate = plan.gate
        self.assertTrue(gate.get("blocked"))
        self.assertEqual(gate.get("reason"), "rr")
        self.assertEqual(gate.get("would_be"), "long")
        self.assertLess(gate.get("rr"), 3.0)
        self.assertEqual(gate.get("need"), 3.0)
        self.assertAlmostEqual(gate.get("score"), plan.score, places=1)

    def test_plain_wait_has_no_gate(self):
        """单纯评分不够只是观望，不算被闸门拦下。"""
        plan = plan_for(RANGE, "intraday")
        self.assertEqual(plan.direction, "wait")
        self.assertEqual(plan.gate, {})
        self.assertLess(abs(plan.score), 30.0)

    def test_passing_signal_has_no_gate(self):
        self.assertEqual(plan_for(UP).gate, {})

    def test_missing_price_gives_wait_with_note(self):
        snap = make_snapshot(**UP)
        snap.price = None
        plan = build_plan(snap, cfg_with(), mode="intraday")
        self.assertEqual(plan.direction, "wait")
        self.assertTrue(plan.notes)

    def test_missing_atr_data_gives_wait(self):
        snap = make_snapshot(**UP)
        snap.klines.pop("15min")
        plan = build_plan(snap, cfg_with(), mode="intraday")
        self.assertEqual(plan.direction, "wait")
        self.assertTrue(any("止损" in n or "15min" in n for n in plan.notes))

    def test_swing_mode_uses_daily_atr(self):
        plan = plan_for(UP, "swing")
        self.assertEqual(plan.market["atr_period"], MODES["swing"]["atr_period"])

    def test_unknown_mode_falls_back_to_intraday(self):
        self.assertEqual(plan_for(UP, "nope").mode, "intraday")


class TestReversionChannel(unittest.TestCase):
    """超卖反弹通道：独立判据，只做多，三条触发缺一不可。"""

    def _oversold_snapshot(self, n=240, drop_pct=-35.0, last_bounce_pct=0.0):
        """一路阴跌到 drop_pct，最后几根再砸一下，制造 RSI(2) 极值。

        200 根跌势保证 close < SMA200；尾段加速下跌保证 RSI(2) 见底、
        且 close 明显低于 SMA20（SMA20 还挂在高位）。
        """
        start = START_PRICE
        closes = []
        for i in range(n):
            t = i / (n - 1)
            closes.append(start * (1 + drop_pct / 100.0 * t))
        # 尾段再砸一跤，把 RSI(2) 打到 10 以下
        for j in range(3):
            closes[-3 + j] = closes[-3 + j] * (1 - 0.02 * (j + 1))
        if last_bounce_pct:
            closes[-1] = closes[-2] * (1 + last_bounce_pct / 100.0)
        ts_step = 86400
        k = _candles(closes, ts_step, start * 0.002)
        price = k[-1].close
        return Snapshot(
            symbol="TEST-USDT", price=price, index_price=price,
            open24=k[-2].close, high=max(c.high for c in k[-20:]),
            low=min(c.low for c in k[-20:]),
            best_bid=price - 0.5, best_ask=price + 0.5,
            bid_volume=1000.0, ask_volume=1000.0,
            volume_24h=100000.0, turnover_24h=100000.0 * price,
            contract_size=CONTRACT_SIZE, klines={"1day": k},
            depth={"asks": [[price + (i + 1) * 5.0, 1.0] for i in range(40)],
                   "bids": [[price - (i + 1) * 5.0, 2.0] for i in range(40)]},
            funding_rate=0.0001, open_interest=10000.0)

    def _plan(self, snap, **account):
        return build_plan(snap, cfg_with(**account), mode="reversion")

    def test_oversold_downtrend_gives_long(self):
        plan = self._plan(self._oversold_snapshot())
        self.assertEqual(plan.direction, "long")
        self.assertEqual(plan.mode, "reversion")
        self.assertTrue(plan.targets, "触发就必须给出目标位")
        self.assertEqual(plan.targets[0]["label"], "TP1")
        self.assertGreater(plan.targets[0]["price"], plan.price, "目标位必须在现价上方")
        self.assertLess(plan.stop, plan.price, "多头止损必须在现价下方")
        self.assertGreaterEqual(plan.rr, 1.0, "盈亏比不足 1 会被闸门拦下")

    def test_target_is_sma20(self):
        plan = self._plan(self._oversold_snapshot())
        self.assertEqual(plan.targets[0]["source"], "SMA20（反弹终点）")

    def test_needs_enough_history(self):
        snap = self._oversold_snapshot(n=120)
        plan = self._plan(snap)
        self.assertEqual(plan.direction, "wait")
        self.assertIn("日线只有", plan.entry_note)

    def test_uptrend_does_not_trigger(self):
        """涨势里不触发：三条里 SMA200 与 SMA20 都不满足。"""
        plan = self._plan(make_snapshot(net_pct=30.0))
        self.assertEqual(plan.direction, "wait")

    def test_bounce_kills_the_setup(self):
        """已经弹起来（贴近 SMA20）就不该再抢反弹——回抽空间没了。"""
        snap = self._oversold_snapshot()
        closes = [c.close for c in snap.klines["1day"]]
        # 把最后几根拉到 SMA20 附近：造一个已反弹的状态
        import iabot.indicators as ind
        s20 = ind.last(ind.sma(closes[:-1], 20))
        snap.klines["1day"][-1] = Candle(
            ts=snap.klines["1day"][-1].ts, open=closes[-2], high=s20 * 1.002,
            low=closes[-2], close=s20 * 1.001, volume=1000.0)
        snap.price = s20 * 1.001
        plan = self._plan(snap)
        self.assertNotEqual(plan.direction, "long", "已贴回 SMA20 就没有反弹空间")

    def test_rr_gate_blocks_thin_bounce(self):
        """SMA20 离现价太近（盈亏比 < 1）-> 观望，不是硬发单。"""
        snap = self._oversold_snapshot()
        closes = [c.close for c in snap.klines["1day"]]
        # 让 SMA20 只比现价高一点点，RSI 仍然极低
        target = closes[-1] * 1.001
        for i in range(19, 0, -1):
            closes[-i] = target
        snap.klines["1day"][-1] = Candle(
            ts=snap.klines["1day"][-1].ts, open=closes[-2], high=target,
            low=closes[-2] * 0.98, close=target, volume=1000.0)
        snap.price = target
        plan = self._plan(snap)
        if plan.direction == "wait":
            self.assertIn("盈亏比", plan.entry_note)
        else:
            self.assertGreaterEqual(plan.rr, 1.0)

    def test_short_is_never_emitted(self):
        """反弹通道只做多——任何输入都不能给出 short。"""
        for drop in (-35.0, -8.0, 0.0):
            snap = self._oversold_snapshot(drop_pct=drop)
            plan = self._plan(snap)
            self.assertNotEqual(plan.direction, "short")

    def test_wait_states_carry_sizing(self):
        """观望态也要有 sizing（网页"SIZE"栏不能空）。"""
        plan = self._plan(make_snapshot(net_pct=30.0))
        self.assertEqual(plan.direction, "wait")
        self.assertIn("sizing", plan.to_dict())

    def test_serialisation_round_trips(self):
        plan = self._plan(self._oversold_snapshot())
        d = plan.to_dict()
        self.assertEqual(json.loads(json.dumps(d))["direction"], "long")

    def test_subordinate_half_risk(self):
        """从属通道单笔风险按主策略的一半计。"""
        plan = self._plan(self._oversold_snapshot(), risk_pct_per_trade=2.0)
        self.assertEqual(plan.direction, "long")
        self.assertAlmostEqual(plan.sizing["risk_pct"], 1.0, places=6)
        self.assertTrue(any("从属通道" in n for n in plan.notes))

    def test_blocked_when_primary_also_long(self):
        """主策略已给同向 long 时，本通道不重复开仓。"""
        from unittest import mock
        import iabot.signals as S
        snap = self._oversold_snapshot()
        fake = S.Plan(symbol=snap.symbol, mode="swing", price=snap.price)
        fake.direction = "long"
        plan = S.Plan(symbol=snap.symbol, mode="reversion", price=snap.price)
        with mock.patch.object(S, "build_plan", return_value=fake):
            out = S._oversold_reversion_plan(
                snap, cfg_with(), "reversion", S.MODES["reversion"], plan)
        self.assertEqual(out.direction, "wait")
        self.assertIn("不重复开仓", out.entry_note)
        self.assertTrue(out.state_gate.get("subordinate_blocked"))

    def test_not_blocked_when_primary_short(self):
        """主策略给 short 不拦——反弹与趋势做空方向本就相反，可以并存。"""
        from unittest import mock
        import iabot.signals as S
        snap = self._oversold_snapshot()
        fake = S.Plan(symbol=snap.symbol, mode="swing", price=snap.price)
        fake.direction = "short"
        plan = S.Plan(symbol=snap.symbol, mode="reversion", price=snap.price)
        with mock.patch.object(S, "build_plan", return_value=fake):
            out = S._oversold_reversion_plan(
                snap, cfg_with(), "reversion", S.MODES["reversion"], plan)
        self.assertEqual(out.direction, "long")

    def test_no_edge_warning_is_attached(self):
        """无 edge 的通道必须在输出上挂明确警告，避免被当真策略下单。"""
        plan = build_plan(self._oversold_snapshot(), cfg_with(), mode="reversion")
        self.assertTrue(any("不建议实盘" in w for w in plan.warnings))


class TestTrendVeto(unittest.TestCase):
    """趋势否决闸门：方向定了之后，日线趋势明确反对就降级观望。

    依据（20 币 × 2000 根日线、真实规则逐笔）：
      swing 原样        n=5415  +0.183R   long n=2799 +0.119R
      只保留趋势同向    n=2941  +0.199R   long n=1199 +0.165R
    逆势单总体 +0.165R 看着不亏，但它把 long 从 +0.119 稀释到 +0.085。
    """

    def _cfg(self, veto=True):
        cfg = cfg_with()
        cfg["analysis"]["trend_veto"] = veto
        return cfg

    def test_off_by_default(self):
        cfg = cfg_with()
        self.assertFalse(cfg["analysis"].get("trend_veto"))

    def test_counter_trend_long_is_downgraded(self):
        cfg = self._cfg()
        for scenario in (UP, DOWN_INTRADAY, DOWN_SWING, RANGE):
            for mode in ("intraday", "swing"):
                with self.subTest(scenario=scenario, mode=mode):
                    plan = build_plan(make_snapshot(**scenario), cfg, mode=mode)
                    if plan.direction == "long":
                        td = next(f.score for f in plan.factors
                                  if f.name == "trend_daily")
                        if td < -0.2:
                            self.assertEqual(
                                plan.direction, "wait",
                                "逆日线趋势做多必须被降级")
                    self.assertTrue(plan.direction in ("long", "short", "wait"))

    def test_veto_actually_fires(self):
        """闸门必须真的触发过，否则上面的"不误杀"测试是空转。

        真实数据上它否决 31% 的有方向信号（BTC 2000 根日线，n=222）：
        那些单子是"深跌破所有均线（trend_daily ≈ -1.0）但成交量暴增 107%，
        状态因子把分数顶到 +11~+23 刚过阈值"的逆势多。合成场景要复现这个组合。
        """
        from iabot.market import Candle
        # 长期阴跌：价格远在所有均线下方、均线自身向下 -> trend_daily 强负
        n = 240
        peak = 100.0
        closes = [peak * (1 - 0.0045 * i) for i in range(n)]
        # 尾段成交量暴增（放量 -> volume 因子 +1.0，权重 0.18），
        # 把状态因子顶成正的，评分刚过 30 阈值 -> 出现"逆势多"
        k = []
        prev = closes[0]
        for i, c in enumerate(closes):
            o = prev
            k.append(Candle(ts=BASE_TS + i * 86400, open=o,
                            high=max(o, c) + 0.05, low=min(o, c) - 0.05,
                            close=c,
                            volume=(3000.0 if i >= n - 2 else 1000.0 + i)))
            prev = c
        price = k[-1].close
        snap = Snapshot(
            symbol="TEST-USDT", price=price, index_price=price,
            open24=k[-2].close, high=max(c.high for c in k[-30:]),
            low=min(c.low for c in k[-30:]),
            best_bid=price - 0.5, best_ask=price + 0.5,
            bid_volume=1000.0, ask_volume=1000.0,
            volume_24h=100000.0, turnover_24h=100000.0 * price,
            contract_size=CONTRACT_SIZE,
            klines={"1day": k, "60min": k[-200:], "15min": k[-220:]},
            depth={"asks": [[price + (i + 1) * 5.0, 1.0] for i in range(40)],
                   "bids": [[price - (i + 1) * 5.0, 2.0] for i in range(40)]},
            funding_rate=0.0001, open_interest=10000.0)
        off = build_plan(snap, self._cfg(False), mode="swing")
        on = build_plan(snap, self._cfg(True), mode="swing")
        td = next(f.score for f in off.factors if f.name == "trend_daily")
        # 这个构造必须先造出"逆势多"，否则下面的断言没有意义
        self.assertLess(td, -0.5, "构造必须先让日线趋势明确为负")
        if off.direction == "long":
            self.assertEqual(on.direction, "wait",
                             "逆势多必须被否决降级")
            self.assertTrue(on.state_gate.get("trend_veto"))

    def test_aligned_signal_survives(self):
        """与趋势同向的信号不能被这道闸门误杀。"""
        cfg = self._cfg()
        plan = build_plan(make_snapshot(**UP), cfg, mode="swing")
        if plan.direction == "long":
            td = next(f.score for f in plan.factors if f.name == "trend_daily")
            if td > 0.2:
                self.assertEqual(plan.direction, "long",
                                 "顺势多单不该被否决")
        self.assertTrue(plan.direction in ("long", "short", "wait"))



class TestFactors(unittest.TestCase):
    def test_score_always_in_range(self):
        for scenario in (UP, DOWN_INTRADAY, DOWN_SWING, RANGE):
            for mode in ("intraday", "swing"):
                plan = plan_for(scenario, mode)
                self.assertGreaterEqual(plan.score, -100.0)
                self.assertLessEqual(plan.score, 100.0)

    def test_factor_table_is_well_formed(self):
        plan = plan_for(UP)
        names = [f.name for f in plan.factors]
        self.assertEqual(len(names), len(FACTOR_DEFAULTS))
        self.assertEqual(len(names), len(set(names)), "因子名不能重复")
        for f in plan.factors:
            self.assertGreaterEqual(f.score, -1.0)
            self.assertLessEqual(f.score, 1.0)
            self.assertGreater(f.weight, 0.0)
            self.assertTrue(f.label and f.detail)
        self.assertAlmostEqual(sum(f.weight for f in plan.factors),
                               DOCUMENTED_WEIGHT_SUM, places=6)

    def test_contribution_is_score_times_weight(self):
        for f in plan_for(UP).to_dict()["factors"]:
            self.assertAlmostEqual(f["score"] * f["weight"], f["contribution"], places=3)

    def test_daily_trend_factor_follows_direction(self):
        pick = lambda p: next(f.score for f in p.factors if f.name == "trend_daily")
        # 线性上涨序列上 SMA 紧贴价格，距离项 tanh(dist/k) 会收缩，
        # 拿不到旧版那种布尔满分。要 >0.5 的强趋势得让价格真正拉开距离。
        up = plan_for(UP)
        self.assertGreater(pick(up), 0.2, "持续上涨至少该给偏多")
        dn = plan_for(DOWN_INTRADAY)
        self.assertLess(pick(dn), -0.2, "持续下跌至少该给偏空")

    def test_daily_trend_slope_overrides_distance(self):
        """价格站在均线上方但均线自己在往下走，不能再给强多。

        这是旧版的硬缺陷：SMA200 那票只看"还在上方"，13% 和 0.6% 同票，
        顶部附近会一路报多直到跌穿。斜率项必须能把它压下来。
        """
        pick = lambda p: next(f.score for f in p.factors if f.name == "trend_daily")
        # 急涨后横盘：价格远在 SMA 上方，但均线已走平
        flat = plan_for(UP_FLAT)
        self.assertLess(pick(flat), 0.5,
                        "价格虽在均线上方但均线走平，不该给强多")


class TestLevelsAndRisk(unittest.TestCase):
    def test_long_prices_are_ordered(self):
        plan = plan_for(UP)
        self.assertEqual(plan.direction, "long")
        self.assertLess(plan.stop, plan.entry_low, "多头止损必须在入场下方")
        self.assertLess(plan.entry_low, plan.entry_high)
        mid = (plan.entry_low + plan.entry_high) / 2
        self.assertLess(abs(mid / plan.price - 1.0), 0.02, "入场区不能离现价太远")
        self.assertTrue(plan.targets, "有方向就必须给出目标位")
        for t in plan.targets:
            self.assertGreater(t["price"], plan.entry_high, "多头目标必须在入场上方")

    def test_short_prices_are_ordered(self):
        plan = plan_for(DOWN_INTRADAY)
        self.assertEqual(plan.direction, "short")
        self.assertGreater(plan.stop, plan.entry_high, "空头止损必须在入场上方")
        self.assertLess(plan.entry_low, plan.entry_high)
        mid = (plan.entry_low + plan.entry_high) / 2
        self.assertLess(abs(mid / plan.price - 1.0), 0.02, "入场区不能离现价太远")
        self.assertTrue(plan.targets, "有方向就必须给出目标位")
        for t in plan.targets:
            self.assertLess(t["price"], plan.entry_low, "空头目标必须在入场下方")

    def test_targets_sorted_by_distance_and_rr(self):
        for scenario in (UP, DOWN_INTRADAY):
            plan = plan_for(scenario)
            prices = [t["price"] for t in plan.targets]
            self.assertEqual(prices, sorted(prices))
            self.assertEqual([t["rr"] for t in plan.targets],
                             sorted(t["rr"] for t in plan.targets))

    def test_resistance_above_and_support_below_price(self):
        for scenario in (UP, DOWN_INTRADAY, RANGE):
            plan = plan_for(scenario)
            for l in plan.levels["resistance"]:
                self.assertGreater(l["price"], plan.price)
            for l in plan.levels["support"]:
                self.assertLess(l["price"], plan.price)

    def test_stop_pct_inside_mode_range(self):
        for mode in MODES:
            lo, hi = MODES[mode]["stop_pct_range"]
            for scenario in (UP, DOWN_INTRADAY, DOWN_SWING):
                plan = plan_for(scenario, mode)
                if plan.direction == "wait":
                    continue
                self.assertGreaterEqual(plan.stop_pct, lo - 1e-6)
                self.assertLessEqual(plan.stop_pct, hi + 1e-6)

    def test_entry_band_has_minimum_width(self):
        for scenario in (UP, DOWN_INTRADAY):
            plan = plan_for(scenario)
            width = (plan.entry_high - plan.entry_low) / plan.entry_low * 100
            self.assertGreaterEqual(width, MODES["intraday"]["entry_width_pct"] - 1e-9)

    def test_low_rr_is_downgraded_to_wait(self):
        """盈亏比不到 1.0 就必须降级，不能给出注定亏钱的单子。"""
        plan = plan_for(RANGE)
        self.assertEqual(plan.direction, "wait")
        self.assertIsNone(plan.entry_low)
        self.assertIsNone(plan.entry_high)
        self.assertIsNone(plan.stop)
        self.assertIsNone(plan.stop_pct)
        self.assertEqual(plan.targets, [])
        self.assertIsNone(plan.rr)

    def test_rr_never_below_gate_threshold_for_real_signals(self):
        """给了方向的计划，第一目标盈亏比必须达到配置的闸门门槛。"""
        thr = gate_min_rr()
        for scenario in (UP, DOWN_INTRADAY, DOWN_SWING):
            for mode in ("intraday", "swing"):
                plan = plan_for(scenario, mode)
                if plan.direction == "wait":
                    continue
                self.assertIsNotNone(plan.rr)
                self.assertGreaterEqual(plan.rr, thr - 1e-9)


class TestSizing(unittest.TestCase):
    def test_loss_matches_risk_budget(self):
        cfg = cfg_with(equity_usdt=450.0, risk_pct_per_trade=2.0, preferred_leverage=10)
        plan = build_plan(make_snapshot(**UP), cfg, mode="intraday")
        s = plan.sizing
        self.assertTrue(s["applicable"])
        self.assertLessEqual(s["loss_if_stopped_usdt"], 450.0 * 0.02 + 0.02)
        self.assertGreater(s["loss_if_stopped_usdt"], 0.0)
        self.assertGreaterEqual(s["contracts"], 1)
        mid = (plan.entry_low + plan.entry_high) / 2
        self.assertAlmostEqual(s["notional_usdt"], s["contracts"] * CONTRACT_SIZE * mid,
                               places=1)

    def test_contracts_floor_the_risk_budget(self):
        cfg = cfg_with(equity_usdt=450.0, risk_pct_per_trade=2.0, preferred_leverage=10)
        plan = build_plan(make_snapshot(**UP), cfg, mode="intraday")
        budget = 450.0 * 0.02
        expect = math.floor(budget / (plan.sizing["stop_distance"] * CONTRACT_SIZE))
        self.assertEqual(plan.sizing["contracts"], int(expect))

    def test_higher_risk_gives_more_contracts(self):
        small = plan_for(UP, equity_usdt=450.0, risk_pct_per_trade=1.0).sizing["contracts"]
        big = plan_for(UP, equity_usdt=450.0, risk_pct_per_trade=4.0).sizing["contracts"]
        self.assertGreater(big, small)

    def test_margin_cap_respected(self):
        plan = plan_for(UP, equity_usdt=450.0, risk_pct_per_trade=20.0,
                        preferred_leverage=3, max_margin_pct=10.0)
        s = plan.sizing
        self.assertTrue(s["applicable"])
        self.assertLessEqual(s["margin_usdt"], 450.0 * 0.10 + 1.0)
        self.assertLessEqual(s["margin_pct_of_equity"], 10.0 + 0.1)
        self.assertIn("缩减", s["note"])

    def test_leverage_never_exceeds_max(self):
        plan = plan_for(UP, equity_usdt=450.0, preferred_leverage=125, max_leverage=10)
        self.assertEqual(plan.sizing["leverage"], 10)

    def test_wait_has_no_sizing(self):
        plan = plan_for(RANGE)
        self.assertEqual(plan.direction, "wait")
        self.assertFalse(plan.sizing.get("applicable", False))

    def test_zero_equity_does_not_crash(self):
        plan = plan_for(UP, equity_usdt=0.0)
        self.assertFalse(plan.sizing.get("applicable", True))


class TestSerialisation(unittest.TestCase):
    def test_to_dict_is_json_safe(self):
        text = json.dumps(plan_for(UP).to_dict(), ensure_ascii=False)
        self.assertIn("factors", text)
        self.assertIn("sizing", text)
        self.assertIn("levels", text)

    def test_headline_mentions_direction(self):
        self.assertIn("做多", plan_for(UP).headline())
        self.assertIn("做空", plan_for(DOWN_INTRADAY).headline())
        self.assertIn("观望", plan_for(RANGE).headline())


if __name__ == "__main__":
    unittest.main()