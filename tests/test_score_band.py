from __future__ import annotations

import copy
import unittest

from iabot.config import DEFAULTS, _merge, validate_config
from iabot.signals import build_plan

from tests.test_signals import UP, make_snapshot


def _cfg_with(analysis: dict) -> dict:
    return validate_config(_merge(copy.deepcopy(DEFAULTS),
                                  {"analysis": analysis}))


class TestScoreBandGate(unittest.TestCase):
    def _plan(self, cfg):
        return build_plan(make_snapshot(**UP), cfg, mode="intraday")

    def test_default_off(self):
        self.assertFalse(DEFAULTS["analysis"]["score_band"]["enabled"])
        base = self._plan(copy.deepcopy(DEFAULTS))
        self.assertEqual(base.direction, "long")      # 夹具本身是 long
        self.assertNotIn("score_band", (base.state_gate or {}))

    def test_inside_band_passes(self):
        base = self._plan(copy.deepcopy(DEFAULTS))
        mag = abs(base.score)
        cfg = _cfg_with({"score_band": {"enabled": True,
                                        "min": mag - 1, "max": mag + 1}})
        p = self._plan(cfg)
        self.assertEqual(p.direction, base.direction)
        self.assertNotIn("score_band", (p.state_gate or {}))

    def test_above_max_is_wait(self):
        base = self._plan(copy.deepcopy(DEFAULTS))
        mag = abs(base.score)
        cfg = _cfg_with({"score_band": {"enabled": True,
                                        "min": 0.0, "max": mag - 1}})
        p = self._plan(cfg)
        self.assertEqual(p.direction, "wait")
        self.assertTrue((p.state_gate or {}).get("score_band"))
        self.assertIn("走过头", (p.state_gate or {}).get("reason", ""))

    def test_below_min_is_wait(self):
        base = self._plan(copy.deepcopy(DEFAULTS))
        mag = abs(base.score)
        cfg = _cfg_with({"score_band": {"enabled": True,
                                        "min": mag + 1, "max": mag + 10}})
        p = self._plan(cfg)
        self.assertEqual(p.direction, "wait")
        self.assertTrue((p.state_gate or {}).get("score_band"))
        self.assertIn("不够强", (p.state_gate or {}).get("reason", ""))

    def test_band_is_symmetric(self):
        # 负分同样按绝对值判定：short 方向的高分也该被拦
        base = self._plan(copy.deepcopy(DEFAULTS))
        mag = abs(base.score)
        cfg = _cfg_with({"score_band": {"enabled": True,
                                        "min": 0.0, "max": mag - 1}})
        p = self._plan(cfg)
        self.assertEqual(p.direction, "wait")

    def test_coercion_clamps_and_swaps(self):
        cfg = _cfg_with({"score_band": {"enabled": True, "min": 90, "max": 10,
                                        }})
        sb = cfg["analysis"]["score_band"]
        self.assertAlmostEqual(sb["min"], 10.0)
        self.assertAlmostEqual(sb["max"], 90.0)

    def test_coercion_clamps_range(self):
        cfg = _cfg_with({"score_band": {"enabled": True, "min": -50,
                                        "max": 500}})
        sb = cfg["analysis"]["score_band"]
        self.assertAlmostEqual(sb["min"], 0.0)
        self.assertAlmostEqual(sb["max"], 100.0)

    def test_wait_stays_wait(self):
        # 本来就是观望的（阈值调到不可能达到），不该被闸门再改一次
        cfg = _cfg_with({"score_threshold": 500,
                         "score_band": {"enabled": True, "min": 0, "max": 1}})
        p = self._plan(cfg)
        self.assertEqual(p.direction, "wait")
        self.assertFalse((p.state_gate or {}).get("score_band"))


if __name__ == "__main__":
    unittest.main()
