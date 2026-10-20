from __future__ import annotations

import unittest

from iabot.market import Candle
from iabot.outcomes import evaluate_outcome

T0 = 1_700_000_000.0


def _c(ts_off: float, o: float, h: float, l: float, cl: float) -> Candle:
    return Candle(ts=T0 + ts_off, open=o, high=h, low=l, close=cl, volume=1.0)


def _plan(direction="long", entry=100.0, stop=95.0, tp=110.0):
    return {
        "direction": direction,
        "entry_low": entry - 0.5, "entry_high": entry + 0.5,
        "stop": stop,
        "targets": [{"label": "TP1", "price": tp, "rr": 2.0}],
        "generated_at": T0,
        "mode": "swing",
    }


class TestBaselineStop(unittest.TestCase):
    def test_plain_stop_is_minus_one_r(self):
        # 入场后直接击穿止损：-1R
        candles = [_c(600, 100, 100.2, 94.5, 95.0)]
        r = evaluate_outcome(_plan(), candles)
        self.assertEqual(r["outcome"], "stopped")
        self.assertAlmostEqual(r["rr"], -1.0, places=4)

    def test_tp_hit(self):
        candles = [_c(600, 100, 111, 99.5, 110.5)]
        r = evaluate_outcome(_plan(), candles)
        self.assertEqual(r["outcome"], "tp1")


class TestBreakeven(unittest.TestCase):
    def test_breakeven_turns_loss_into_flat(self):
        # 先冲到 +1R（105 以上），再回落击穿原始止损 -> 保本 0R，而非 -1R
        candles = [
            _c(600, 100, 106.0, 99.5, 105.5),   # 浮盈 1.2R
            _c(1200, 105.5, 105.5, 94.0, 94.5),  # 跌穿原始止损 95
        ]
        base = evaluate_outcome(_plan(), candles)
        be = evaluate_outcome(_plan(), candles, breakeven_at_r=1.0)
        self.assertAlmostEqual(base["rr"], -1.0, places=4)
        self.assertAlmostEqual(be["rr"], 0.0, places=4)

    def test_breakeven_not_triggered_below_threshold(self):
        # 只到 +0.5R 就回落：保本不该生效，仍是 -1R
        candles = [
            _c(600, 100, 102.4, 99.5, 102.0),
            _c(1200, 102.0, 102.0, 94.0, 94.5),
        ]
        r = evaluate_outcome(_plan(), candles, breakeven_at_r=1.0)
        self.assertAlmostEqual(r["rr"], -1.0, places=4)

    def test_breakeven_does_not_use_same_bar_high(self):
        # 同一根 K 线里先冲高再砸穿：不能用"看到高点"救这一笔
        candles = [_c(600, 100, 106.0, 94.0, 94.5)]
        r = evaluate_outcome(_plan(), candles, breakeven_at_r=1.0)
        self.assertAlmostEqual(r["rr"], -1.0, places=4)


class TestTrailing(unittest.TestCase):
    def test_trailing_locks_some_profit(self):
        # 涨到 108（+1.6R），ATR=2、倍数 1 -> 止损抬到 106；随后回落到 105
        candles = [
            _c(600, 100, 108.0, 99.5, 107.5),
            _c(1200, 107.5, 107.5, 105.0, 105.5),
        ]
        r = evaluate_outcome(_plan(), candles, trail_atr_mult=1.0, atr=2.0)
        self.assertEqual(r["outcome"], "stopped")
        # 出场在 106 -> (106-100)/5 = +1.2R
        self.assertAlmostEqual(r["rr"], 1.2, places=4)

    def test_trailing_ignored_without_atr(self):
        candles = [
            _c(600, 100, 108.0, 99.5, 107.5),
            _c(1200, 107.5, 107.5, 94.0, 94.5),
        ]
        r = evaluate_outcome(_plan(), candles, trail_atr_mult=1.0, atr=None)
        self.assertAlmostEqual(r["rr"], -1.0, places=4)

    def test_trailing_never_loosens_stop(self):
        # 价格没怎么涨，移动止损不该把止损往外推（多头不能更低）
        candles = [_c(600, 100, 100.5, 94.0, 94.5)]
        r = evaluate_outcome(_plan(), candles, trail_atr_mult=2.0, atr=1.0)
        self.assertAlmostEqual(r["rr"], -1.0, places=4)

    def test_short_side_breakeven(self):
        plan = _plan(direction="short", entry=100.0, stop=105.0, tp=90.0)
        candles = [
            _c(600, 100, 100.5, 94.0, 94.5),    # 浮盈 1.2R
            _c(1200, 94.5, 106.0, 94.0, 105.5),  # 涨穿原始止损 105
        ]
        base = evaluate_outcome(plan, candles)
        be = evaluate_outcome(plan, candles, breakeven_at_r=1.0)
        self.assertAlmostEqual(base["rr"], -1.0, places=4)
        self.assertAlmostEqual(be["rr"], 0.0, places=4)


class TestConfig(unittest.TestCase):
    def test_defaults_off(self):
        from iabot.config import DEFAULTS
        self.assertEqual(DEFAULTS["analysis"]["exit"],
                         {"breakeven_at_r": None, "trail_atr_mult": None})

    def test_coercion(self):
        from iabot.config import DEFAULTS, _merge, validate_config
        cfg = validate_config(_merge(DEFAULTS, {"analysis": {"exit": {
            "breakeven_at_r": "bad", "trail_atr_mult": 99}}}))
        ex = cfg["analysis"]["exit"]
        self.assertAlmostEqual(ex["breakeven_at_r"], 1.0)   # 脏值 -> 默认
        self.assertAlmostEqual(ex["trail_atr_mult"], 10.0)  # 夹到上限

    def test_null_stays_off(self):
        from iabot.config import DEFAULTS, _merge, validate_config
        cfg = validate_config(_merge(DEFAULTS, {"analysis": {"exit": {
            "breakeven_at_r": None, "trail_atr_mult": None}}}))
        self.assertIsNone(cfg["analysis"]["exit"]["breakeven_at_r"])
        self.assertIsNone(cfg["analysis"]["exit"]["trail_atr_mult"])


if __name__ == "__main__":
    unittest.main()
