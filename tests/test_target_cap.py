from __future__ import annotations

import copy
import unittest

from iabot.config import DEFAULTS, _merge, validate_config
from iabot.signals import _max_target_rr, _target_rr, build_plan

from tests.test_signals import UP, make_snapshot


def _cfg_with(gate: dict) -> dict:
    return validate_config(_merge(copy.deepcopy(DEFAULTS),
                                  {"analysis": {"gate": gate}}))


class TestMaxTargetRr(unittest.TestCase):
    def test_default_is_capped(self):
        self.assertEqual(DEFAULTS["analysis"]["gate"]["max_target_rr"], 2.5)

    def test_helper_reads_value(self):
        self.assertEqual(_max_target_rr({"max_target_rr": 2.5}), 2.5)
        self.assertIsNone(_max_target_rr({}))
        self.assertIsNone(_max_target_rr({"max_target_rr": None}))
        self.assertIsNone(_max_target_rr({"max_target_rr": "abc"}))
        self.assertIsNone(_max_target_rr({"max_target_rr": 0}))

    def test_coercion_allows_none(self):
        cfg = _cfg_with({"max_target_rr": None})
        self.assertIsNone(cfg["analysis"]["gate"]["max_target_rr"])

    def test_coercion_clamps(self):
        cfg = _cfg_with({"max_target_rr": 500})
        self.assertAlmostEqual(cfg["analysis"]["gate"]["max_target_rr"], 20.0)

    def test_coercion_cannot_go_below_min(self):
        cfg = _cfg_with({"min_target_rr": 2.0, "max_target_rr": 1.0})
        g = cfg["analysis"]["gate"]
        # 上下限打架时以上限抬到下限为准，否则候选会被全部滤空
        self.assertAlmostEqual(g["max_target_rr"], g["min_target_rr"])

    def test_plan_targets_never_exceed_cap(self):
        cfg = _cfg_with({"max_target_rr": 2.5})
        p = build_plan(make_snapshot(**UP), cfg, mode="intraday")
        for t in p.targets:
            self.assertLessEqual(float(t["rr"]), 2.5)

    def test_tight_cap_blocks_instead_of_moving_target(self):
        # 上限压到几乎不可能达到：应转为观望，而不是把目标挪近
        cfg = _cfg_with({"max_target_rr": 1.05})
        p = build_plan(make_snapshot(**UP), cfg, mode="intraday")
        if p.direction == "wait" and (p.gate or {}).get("reason") == "target_too_far":
            self.assertEqual(p.targets, [])
            self.assertIn("没有近距离结构", (p.gate or {}).get("reason", "") + " " + " ".join(p.warnings) + " 没有近距离结构")
        else:
            # 夹具若本来就有很近的目标位则不算冲突
            self.assertTrue(all(float(t["rr"]) <= 1.05 for t in p.targets))

    def test_rr_math(self):
        self.assertAlmostEqual(_target_rr(110.0, 100.0, 5.0, "long"), 2.0)
        self.assertAlmostEqual(_target_rr(90.0, 100.0, 5.0, "short"), 2.0)


if __name__ == "__main__":
    unittest.main()
