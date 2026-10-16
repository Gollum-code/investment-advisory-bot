"""因子校准与权重单一来源的回归测试。"""

import copy
import unittest

from iabot.config import DEFAULTS
from iabot.market import Snapshot
from iabot.signals import (FACTOR_DEFAULTS, compute_factors, apply_factor_weights,
                           _f_elite, _f_funding)

try:
    from .test_signals import make_snapshot, UP
except ImportError:
    from test_signals import make_snapshot, UP


def _elite(buy, sell, hist_buy=0.47, hist_sell=0.53, n=20):
    e = {"symbol": "X-USDT", "price": 100.0, "funding_rate": 0.0001,
         "elite_position": [{"buy_ratio": hist_buy, "sell_ratio": hist_sell,
                             "ts": 1_700_000_000 + i * 86400}
                            for i in range(n)]}
    e["elite_position"][-1] = {"buy_ratio": buy, "sell_ratio": sell,
                               "ts": 1_700_000_000 + (n - 1) * 86400}
    return _f_elite(Snapshot(**e))


class TestWeightSingleSource(unittest.TestCase):
    """权重只能有一个来源。曾经因子函数里各写死一个数、FACTOR_DEFAULTS 又写
    另一个数，实际计算用前者、网页展示用后者 —— 面板会骗人。"""

    def test_compute_factors_weights_match_defaults(self):
        snap = make_snapshot(**UP)
        for f in compute_factors(snap):
            if f.name in FACTOR_DEFAULTS:
                self.assertAlmostEqual(
                    f.weight, FACTOR_DEFAULTS[f.name], places=9,
                    msg=f"{f.name} 的计算权重与 FACTOR_DEFAULTS 不一致")

    def test_no_hardcoded_literal_can_disagree(self):
        """把 FACTOR_DEFAULTS 改掉后，compute_factors 必须立刻跟着变。"""
        import iabot.signals as S
        old = dict(S.FACTOR_DEFAULTS)
        try:
            S.FACTOR_DEFAULTS["momentum"] = 0.42
            snap = make_snapshot(**UP)
            by = {f.name: f.weight for f in compute_factors(snap)}
            self.assertAlmostEqual(by["momentum"], 0.42)
        finally:
            S.FACTOR_DEFAULTS.clear()
            S.FACTOR_DEFAULTS.update(old)

    def test_total_weight_is_documented_sum(self):
        self.assertAlmostEqual(sum(FACTOR_DEFAULTS.values()), 1.06, places=9)

    def test_short_term_factors_downgraded(self):
        """短周期因子权重必须明显低于日线趋势，否则 1h 饱和项会主导评分。"""
        self.assertGreater(FACTOR_DEFAULTS["trend_daily"],
                           FACTOR_DEFAULTS["trend_intraday"] * 3)
        self.assertGreater(FACTOR_DEFAULTS["trend_daily"],
                           FACTOR_DEFAULTS["structure"] * 3)


class TestEliteCentered(unittest.TestCase):
    """大户净比必须打"相对自身基线"的偏离。

    实测 40 个合约里 39 个净比 <= 0（大户拿永续空单对冲现货是常态），
    用绝对值打分会让这个 0.12 权重的因子永远只输出负分。
    """

    def test_typical_negative_net_is_not_automatically_bearish(self):
        # 净比 -0.06 但历史上一直是 -0.06 -> 等于自身常态 -> 不应看空
        f = _elite(0.47, 0.53, hist_buy=0.47, hist_sell=0.53)
        self.assertAlmostEqual(f.score, 0.0, places=6)

    def test_below_own_baseline_is_bullish(self):
        # 净比从常态 -0.06 抬到 -0.02 -> 相对转多
        f = _elite(0.49, 0.51, hist_buy=0.47, hist_sell=0.53)
        self.assertGreater(f.score, 0.0)

    def test_above_own_baseline_is_bearish(self):
        f = _elite(0.44, 0.56, hist_buy=0.47, hist_sell=0.53)
        self.assertLess(f.score, 0.0)

    def test_saturated_constants_do_not_explode(self):
        # 历史完全不变时尺度为 0，不能除零放大成 ±1
        f = _elite(0.47, 0.53, hist_buy=0.47, hist_sell=0.53)
        self.assertTrue(-1.0 <= f.score <= 1.0)
        self.assertEqual(f.score, 0.0)

    def test_always_positive_series_gives_zero(self):
        f = _elite(0.55, 0.45, hist_buy=0.55, hist_sell=0.45)
        self.assertAlmostEqual(f.score, 0.0, places=6)

    def test_produces_both_signs_across_coins(self):
        """同一天的不同币，因子必须能给出正负两种结论（有横截面区分度）。"""
        scores = [_elite(0.49, 0.51, 0.47, 0.53).score,
                  _elite(0.44, 0.56, 0.47, 0.53).score,
                  _elite(0.47, 0.53, 0.47, 0.53).score]
        self.assertTrue(any(s > 0 for s in scores))
        self.assertTrue(any(s < 0 for s in scores))


class TestFundingRelative(unittest.TestCase):
    """资金费同理：费率有强结构性偏向，必须相对自身基线。"""

    def _f(self, rate, hist):
        return _f_funding(Snapshot(
            symbol="X-USDT", price=100.0, funding_rate=rate,
            funding_history=[{"funding_time": i * 8 * 3600_000, "funding_rate": v}
                             for i, v in enumerate(hist)]))

    def test_at_own_baseline_is_neutral(self):
        self.assertAlmostEqual(self._f(0.0003, [0.0003] * 30).score, 0.0)

    def test_below_baseline_is_bullish(self):
        self.assertGreater(self._f(0.0001, [0.0003] * 30).score, 0.0)

    def test_above_baseline_is_bearish(self):
        self.assertLess(self._f(0.0006, [0.0003] * 30).score, 0.0)

    def test_no_history_falls_back_to_absolute(self):
        s = Snapshot(symbol="X-USDT", price=100.0, funding_rate=0.0006)
        self.assertLess(_f_funding(s).score, 0.0)      # 高费率 -> 多头拥挤

    def test_missing_rate_is_zero(self):
        self.assertEqual(_f_funding(Snapshot(symbol="X", price=1.0)).score, 0.0)


class TestApplyWeightsStillOverrides(unittest.TestCase):
    def test_custom_weight_wins(self):
        snap = make_snapshot(**UP)
        fs = apply_factor_weights(compute_factors(snap), {"trend_daily": 0.5})
        by = {f.name: f.weight for f in fs}
        self.assertEqual(by["trend_daily"], 0.5)

    def test_zero_override_keeps_default(self):
        """0 不生效（否则会把因子从加权分母里剔除）。"""
        snap = make_snapshot(**UP)
        fs = apply_factor_weights(compute_factors(snap), {"momentum": 0.0})
        by = {f.name: f.weight for f in fs}
        self.assertEqual(by["momentum"], FACTOR_DEFAULTS["momentum"])

    def test_default_cfg_uses_factor_defaults(self):
        cfg = copy.deepcopy(DEFAULTS)
        snap = make_snapshot(**UP)
        fs = apply_factor_weights(compute_factors(snap),
                                  (cfg["analysis"] or {}).get("factor_weights"))
        by = {f.name: f.weight for f in fs}
        for k, v in FACTOR_DEFAULTS.items():
            self.assertAlmostEqual(by[k], v, places=9)


if __name__ == "__main__":
    unittest.main()