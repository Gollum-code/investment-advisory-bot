from __future__ import annotations

import copy
import unittest

from iabot.config import DEFAULTS, _merge, validate_config
from iabot.signals import build_plan

from tests.test_backtest import _trading_snapshot


def _cfg_with(analysis: dict) -> dict:
    return validate_config(_merge(copy.deepcopy(DEFAULTS),
                                  {"analysis": analysis}))


class TestRiskMultiplier(unittest.TestCase):
    def test_defaults_are_neutral(self):
        self.assertAlmostEqual(DEFAULTS["analysis"]["risk_mult_long"], 1.0)
        self.assertAlmostEqual(DEFAULTS["analysis"]["risk_mult_short"], 1.0)

    def test_default_does_not_change_sizing(self):
        # 反空转：默认配置下仓位必须和以前一样
        snap = _trading_snapshot()
        p = build_plan(snap, copy.deepcopy(DEFAULTS), mode="intraday")
        s = p.sizing
        if s.get("applicable"):
            self.assertAlmostEqual(s["risk_mult"], 1.0)
            self.assertAlmostEqual(s["risk_pct_effective"], s["risk_pct"])

    def test_long_multiplier_scales_risk(self):
        snap = _trading_snapshot()
        base = build_plan(snap, copy.deepcopy(DEFAULTS), mode="intraday")
        cfg = _cfg_with({"risk_mult_long": 2.0})
        p = build_plan(snap, cfg, mode="intraday")
        if base.direction == "long" and base.sizing.get("applicable"):
            self.assertAlmostEqual(p.sizing["risk_mult"], 2.0)
            self.assertGreater(p.sizing["risk_amount_usdt"],
                               base.sizing["risk_amount_usdt"])
        # 多空倍率各自独立：空单倍率不该被 long 的改动影响
        self.assertAlmostEqual(p.risk_mult_short, 1.0)

    def test_short_multiplier_scales_risk(self):
        snap = _trading_snapshot()
        cfg = _cfg_with({"risk_mult_short": 1.5})
        p = build_plan(snap, cfg, mode="intraday")
        if p.direction == "short" and p.sizing.get("applicable"):
            self.assertAlmostEqual(p.sizing["risk_mult"], 1.5)
            self.assertAlmostEqual(p.sizing["risk_pct_effective"],
                                   p.sizing["risk_pct"] * 1.5)

    def test_multiplier_only_applies_to_its_side(self):
        snap = _trading_snapshot()
        base = build_plan(snap, copy.deepcopy(DEFAULTS), mode="intraday")
        if base.direction == "long":
            cfg = _cfg_with({"risk_mult_short": 3.0})
            p = build_plan(snap, cfg, mode="intraday")
            if p.sizing.get("applicable"):
                self.assertAlmostEqual(
                    p.sizing["risk_amount_usdt"],
                    base.sizing["risk_amount_usdt"])

    def test_wait_has_no_sizing(self):
        snap = _trading_snapshot()
        cfg = _cfg_with({"risk_mult_long": 2.0, "risk_mult_short": 2.0})
        p = build_plan(snap, cfg, mode="intraday")
        if p.direction == "wait":
            self.assertFalse(p.sizing.get("applicable"))

    def test_coercion_clamps(self):
        cfg = _cfg_with({"risk_mult_long": "bad", "risk_mult_short": 99})
        self.assertAlmostEqual(cfg["analysis"]["risk_mult_long"], 1.0)
        self.assertAlmostEqual(cfg["analysis"]["risk_mult_short"], 3.0)


if __name__ == "__main__":
    unittest.main()
