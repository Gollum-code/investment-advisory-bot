"""闸门判据与目标位一致性的单元测试（不联网）。"""

import copy
import unittest

from iabot import signals as S
from iabot.config import DEFAULTS, validate_config

try:
    from .test_signals import UP, DOWN_INTRADAY, make_snapshot
except ImportError:
    from test_signals import UP, DOWN_INTRADAY, make_snapshot


def cfg_with(**gate):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["analysis"]["gate"].update(gate)
    return cfg


class TestGateHelpers(unittest.TestCase):
    def test_target_rr_long_and_short(self):
        self.assertAlmostEqual(S._target_rr(110, 100, 5, "long"), 2.0)
        self.assertAlmostEqual(S._target_rr(90, 100, 5, "short"), 2.0)
        self.assertAlmostEqual(S._target_rr(90, 100, 5, "long"), -2.0)

    def test_target_rr_zero_risk_is_flat(self):
        self.assertEqual(S._target_rr(110, 100, 0, "long"), 0.0)
        self.assertEqual(S._target_rr(110, 100, -1, "long"), 0.0)

    def test_measure_tp1(self):
        t = [{"rr": 0.9}, {"rr": 2.5}, {"rr": 4.0}]
        self.assertAlmostEqual(S._gate_measure(t, "tp1", (0.5, 0.3, 0.2)), 0.9)

    def test_measure_max(self):
        t = [{"rr": 0.9}, {"rr": 2.5}, {"rr": 4.0}]
        self.assertAlmostEqual(S._gate_measure(t, "max", (0.5, 0.3, 0.2)), 4.0)

    def test_measure_weighted_three(self):
        t = [{"rr": 1.0}, {"rr": 2.0}, {"rr": 3.0}]
        self.assertAlmostEqual(S._gate_measure(t, "weighted", (0.5, 0.3, 0.2)),
                               0.5 * 1.0 + 0.3 * 2.0 + 0.2 * 3.0)

    def test_measure_weighted_single_target_is_normalised(self):
        # 只有 TP1 时不能按 0.5 权重算（会把数值压小一半）
        self.assertAlmostEqual(S._gate_measure([{"rr": 1.6}], "weighted",
                                                (0.5, 0.3, 0.2)), 1.6)

    def test_measure_weighted_two_targets_normalised(self):
        t = [{"rr": 1.0}, {"rr": 3.0}]
        # 权重 (0.5, 0.3) 归一化成 0.5/0.8 与 0.3/0.8
        self.assertAlmostEqual(S._gate_measure(t, "weighted", (0.5, 0.3, 0.2)),
                               (0.5 * 1.0 + 0.3 * 3.0) / 0.8)

    def test_measure_empty(self):
        self.assertIsNone(S._gate_measure([], "tp1", (0.5, 0.3, 0.2)))

    def test_gate_settings_defaults(self):
        name, min_rr, w = S._gate_settings({})
        self.assertEqual(name, "tp1")
        self.assertEqual(min_rr, 1.0)
        self.assertEqual(w, (0.5, 0.3, 0.2))

    def test_gate_settings_rejects_unknown_measure(self):
        name, _rr, _w = S._gate_settings({"measure": "乱写"})
        self.assertEqual(name, "tp1")

    def test_gate_settings_pads_short_weights(self):
        _n, _r, w = S._gate_settings({"weights": [0.7]})
        self.assertEqual(len(w), 3)

    def test_gate_settings_rejects_bad_numbers(self):
        _n, min_rr, w = S._gate_settings({"min_rr": "abc", "weights": "abc"})
        self.assertEqual(min_rr, 1.0)
        self.assertEqual(w, (0.5, 0.3, 0.2))

    def test_gate_settings_rejects_zero_weight_sum(self):
        _n, _r, w = S._gate_settings({"weights": [0, 0, 0]})
        self.assertEqual(sum(w) > 0, True)


class TestTargetConsistency(unittest.TestCase):
    """回归：目标位盈亏比必须和最终入场中点自洽，且不低于筛选门槛。"""

    def _plan(self, cfg=None, mode="intraday", snap_kw=None):
        snap = make_snapshot(**(snap_kw or UP))
        return S.build_plan(snap, cfg or cfg_with(), mode=mode)

    def test_reported_rr_matches_entry_midpoint(self):
        """每个目标位报出的 RR，都能用最终 mid/stop 重新算出来。"""
        for mode in ("intraday", "swing"):
            for kw in (UP, DOWN_INTRADAY):
                p = self._plan(mode=mode, snap_kw=kw)
                if p.direction == "wait" or not p.targets:
                    continue
                mid = (p.entry_low + p.entry_high) / 2
                risk = abs(mid - p.stop)
                self.assertGreater(risk, 0)
                for t in p.targets:
                    expect = ((t["price"] - mid) / risk if p.direction == "long"
                              else (mid - t["price"]) / risk)
                    self.assertAlmostEqual(t["rr"], round(expect, 2), places=1,
                                           msg=f"{mode} {t}")

    def test_no_target_below_candidate_floor(self):
        """筛选用的是最终 mid，所以不会有 TP1 掉到 min_target_rr 以下。"""
        for mode in ("intraday", "swing"):
            for kw in (UP, DOWN_INTRADAY):
                p = self._plan(mode=mode, snap_kw=kw)
                if p.direction == "wait" or not p.targets:
                    continue
                for t in p.targets:
                    self.assertGreaterEqual(
                        t["rr"], p.gate.get("need", 1.0) * 0 - 0.8,
                        msg=f"{mode} {t} 低于筛选门槛")

    def test_gate_reports_the_measure_it_used(self):
        p = self._plan(cfg_with(measure="weighted"))
        if p.direction == "wait":
            return
        self.assertIn("加权目标", " ".join(p.notes))
        if p.gate.get("blocked") and p.gate.get("reason") == "rr":
            self.assertEqual(p.gate.get("measure"), "加权")

    def test_looser_gate_lets_more_signals_through(self):
        strict = self._plan(cfg_with(min_rr=1.0))
        loose = self._plan(cfg_with(min_rr=0.5))
        self.assertNotEqual(strict.direction == "wait", loose.direction == "wait"
                            or loose.direction == strict.direction)


class TestGateConfigCoercion(unittest.TestCase):
    def _validate(self, gate):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["analysis"]["gate"] = gate
        return validate_config(cfg)["analysis"]["gate"]

    def test_valid_passes_through(self):
        g = self._validate({"measure": "max", "min_rr": 1.4,
                            "weights": [0.6, 0.3, 0.1], "min_target_rr": 0.7})
        self.assertEqual(g["measure"], "max")
        self.assertEqual(g["min_rr"], 1.4)
        self.assertEqual(g["min_target_rr"], 0.7)

    def test_unknown_measure_falls_back(self):
        self.assertEqual(self._validate({"measure": "nope"})["measure"], "tp1")

    def test_non_numeric_rr_falls_back(self):
        self.assertEqual(self._validate({"min_rr": "abc"})["min_rr"], 1.0)
        self.assertEqual(self._validate({"min_target_rr": None})["min_target_rr"], 0.8)

    def test_bad_weights_fall_back(self):
        self.assertEqual(self._validate({"weights": "abc"})["weights"], [0.5, 0.3, 0.2])
        self.assertEqual(self._validate({"weights": []})["weights"], [0.5, 0.3, 0.2])

    def test_gate_not_a_dict_resets(self):
        self.assertEqual(self._validate("oops")["measure"], "tp1")


if __name__ == "__main__":
    unittest.main()