from __future__ import annotations

import copy
import unittest

from iabot.config import DEFAULTS, _merge, validate_config
from iabot.signals import _dead_factor_names, build_plan, Factor

from tests.test_signals import UP, make_snapshot


def _cfg_with(analysis: dict) -> dict:
    return validate_config(_merge(copy.deepcopy(DEFAULTS),
                                  {"analysis": analysis}))


class TestDeadFactorDetection(unittest.TestCase):
    def test_missing_data_counts_as_dead(self):
        fs = {"funding": Factor("funding", "资金费", 0.0, 0.08, "接口失败"),
              "volume": Factor("volume", "成交额", -0.5, 0.18, "低于 20 日均量")}
        self.assertEqual(_dead_factor_names(fs), {"funding"})

    def test_neutral_zero_is_not_dead(self):
        # 算出来正好是 0 分（例如 RSI=50）不是"没数据"，必须参与合成
        fs = {"momentum": Factor("momentum", "动量", 0.0, 0.06, "15m RSI 50 中性")}
        self.assertEqual(_dead_factor_names(fs), set())

    def test_empty_detail_is_not_dead(self):
        fs = {"vwap": Factor("vwap", "VWAP", 0.0, 0.03, "")}
        self.assertEqual(_dead_factor_names(fs), set())


class TestFactorCoverage(unittest.TestCase):
    def test_coverage_reported(self):
        cfg = _cfg_with({})
        p = build_plan(make_snapshot(**UP), cfg, mode="swing")
        self.assertGreater(p.factor_coverage, 0.0)
        self.assertLessEqual(p.factor_coverage, 1.0)

    def test_coverage_ignores_normalize_switch(self):
        # 覆盖率描述事实，不随"是否剔除"改变——否则关掉归一化时它会永远为 1
        a = build_plan(make_snapshot(**UP), _cfg_with({}), mode="swing")
        b = build_plan(make_snapshot(**UP),
                       _cfg_with({"factor_normalize": {"enabled": True}}),
                       mode="swing")
        self.assertEqual(a.factor_coverage, b.factor_coverage)
        self.assertEqual(a.dead_factors, b.dead_factors)

    def test_default_off(self):
        self.assertFalse(DEFAULTS["analysis"]["factor_normalize"]["enabled"])

    def test_switch_does_not_change_direction_on_full_data(self):
        # 数据齐全时两种口径应当等价（没有因子会被剔除）
        a = build_plan(make_snapshot(**UP), _cfg_with({}), mode="swing")
        b = build_plan(make_snapshot(**UP),
                       _cfg_with({"factor_normalize": {"enabled": True}}),
                       mode="swing")
        self.assertEqual(a.direction, b.direction)
        self.assertAlmostEqual(a.score, b.score, places=6)

    def test_backtest_reports_coverage(self):
        from iabot.backtest import backtest
        res = backtest(make_snapshot(**UP), _cfg_with({}), mode="swing")
        self.assertGreaterEqual(res.factor_coverage, 0.0)
        self.assertLessEqual(res.factor_coverage, 1.0)
        d = res.to_dict()
        self.assertIn("factor_coverage", d)
        self.assertIn("gate_counts", d)


if __name__ == "__main__":
    unittest.main()
