"""因子权重覆盖的单元测试。"""

import copy
import unittest

from iabot.config import DEFAULTS
from iabot.signals import FACTOR_DEFAULTS, apply_factor_weights, build_plan, compute_factors

try:
    from .test_signals import UP, make_snapshot
except ImportError:
    from test_signals import UP, make_snapshot


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


if __name__ == "__main__":
    unittest.main()
