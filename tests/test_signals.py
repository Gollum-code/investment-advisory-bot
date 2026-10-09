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
from iabot.signals import MODES, build_plan

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
DOCUMENTED_WEIGHT_SUM = 1.12

# 场景：net_pct = 整段净涨跌%，tail_pct = 最后 6 小时的额外涨跌%（负数=顺着趋势再走一段）
UP = dict(net_pct=8.0, tail_pct=-0.2)          # 日内 + 波段都给多头
DOWN_INTRADAY = dict(net_pct=-8.0, tail_pct=-1.2)   # 日内给空头
DOWN_SWING = dict(net_pct=-8.0, tail_pct=-0.2)      # 波段给空头
RANGE = dict(chop_pct=0.6, cycles=4.0)              # 没有净方向 -> 观望


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
        self.assertEqual(len(names), 10)
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
        self.assertGreater(pick(plan_for(UP)), 0.5)
        self.assertLess(pick(plan_for(DOWN_INTRADAY)), -0.5)


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

    def test_rr_never_below_one_for_real_signals(self):
        for scenario in (UP, DOWN_INTRADAY, DOWN_SWING):
            for mode in ("intraday", "swing"):
                plan = plan_for(scenario, mode)
                if plan.direction == "wait":
                    continue
                self.assertIsNotNone(plan.rr)
                self.assertGreaterEqual(plan.rr, 1.0)


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