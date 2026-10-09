"""因子权重覆盖与多周期共振的单元测试。"""

import copy
import unittest

from iabot.config import DEFAULTS
from iabot.signals import (FACTOR_DEFAULTS, RESONANCE_DEFAULT_WEIGHT, apply_factor_weights,
                           build_plan, compute_factors, _bias_daily, _bias_intraday,
                           _f_resonance)

try:
    from .test_signals import UP, DOWN_INTRADAY, make_snapshot
except ImportError:
    from test_signals import UP, DOWN_INTRADAY, make_snapshot


class TestApplyWeights(unittest.TestCase):
    def test_defaults_factored_in(self):
        snap = make_snapshot(**UP)
        factors = compute_factors(snap)
        by = {f.name: f.weight for f in factors}
        for name, w in FACTOR_DEFAULTS.items():
            self.assertAlmostEqual(by[name], w)

    def test_override_changes_weight(self):
        snap = make_snapshot(**UP)
        factors = apply_factor_weights(compute_factors(snap),
                                       {"trend_daily": 0.5, "momentum": 0.0})
        by = {f.name: f.weight for f in factors}
        self.assertEqual(by["trend_daily"], 0.5)
        # 0 不生效（保持默认 0.15），避免把某因子从加权分母里剔除
        self.assertEqual(by["momentum"], FACTOR_DEFAULTS["momentum"])

    def test_unknown_name_ignored(self):
        snap = make_snapshot(**UP)
        factors = apply_factor_weights(compute_factors(snap), {"not_a_factor": 0.9})
        self.assertEqual(len(factors), 10)

    def test_build_plan_reads_config_weights(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["analysis"]["factor_weights"] = {"trend_daily": 0.5}
        plan = build_plan(make_snapshot(**UP), cfg, mode="intraday")
        by = {f.name: f.weight for f in plan.factors}
        self.assertEqual(by["trend_daily"], 0.5)


class TestResonance(unittest.TestCase):
    def test_disabled_by_default(self):
        # 默认 10 因子，resonance 不在列表里（保持文档口径）
        factors = compute_factors(make_snapshot(**UP))
        self.assertNotIn("resonance", {f.name for f in factors})
        self.assertEqual(len(factors), 10)

    def test_enabled_adds_factor(self):
        factors = compute_factors(make_snapshot(**UP), resonance=True)
        names = [f.name for f in factors]
        self.assertIn("resonance", names)
        self.assertEqual(len(factors), 11)

    def test_build_plan_respects_resonance_flag(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["analysis"]["resonance"] = {"enabled": True, "weight": 0.06}
        plan = build_plan(make_snapshot(**UP), cfg, mode="intraday")
        by = {f.name: f.weight for f in plan.factors}
        self.assertIn("resonance", by)
        self.assertAlmostEqual(by["resonance"], 0.06)

    def test_resonance_score_in_range(self):
        f = _f_resonance(make_snapshot(**UP), RESONANCE_DEFAULT_WEIGHT)
        self.assertGreaterEqual(f.score, -1.0)
        self.assertLessEqual(f.score, 1.0)
        self.assertTrue(f.detail)

    def test_resonance_positive_in_clean_uptrend(self):
        # 无回撤的干净上涨：日线与 1h 同向偏置 -> 共振为正
        f = _f_resonance(make_snapshot(net_pct=10.0, tail_pct=1.0), 0.06)
        self.assertGreater(f.score, 0.0)

    def test_resonance_positive_in_clean_downtrend(self):
        # 无反抽的干净下跌：两者都负，乘积为正（下跌也是"同向共振"）
        f = _f_resonance(make_snapshot(net_pct=-10.0, tail_pct=-1.0), 0.06)
        self.assertGreater(f.score, 0.0)

    def test_resonance_negative_when_diverging(self):
        # UP 场景末尾有回撤：日线仍多、1h 转空 -> 背离 -> 共振为负
        snap = make_snapshot(**UP)
        hi, lo = _bias_daily(snap), _bias_intraday(snap)
        f = _f_resonance(snap, 0.06)
        self.assertAlmostEqual(f.score, max(-1.0, min(1.0, hi * lo)), places=6)
        if hi * lo < 0:
            self.assertLess(f.score, 0.0)

    def test_bias_helpers_bounded(self):
        snap = make_snapshot(**UP)
        self.assertGreaterEqual(_bias_daily(snap), -1.0)
        self.assertLessEqual(_bias_daily(snap), 1.0)
        self.assertGreaterEqual(_bias_intraday(snap), -1.0)
        self.assertLessEqual(_bias_intraday(snap), 1.0)


if __name__ == "__main__":
    unittest.main()
