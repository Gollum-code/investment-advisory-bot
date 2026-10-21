from __future__ import annotations

import copy
import unittest

from iabot.config import DEFAULTS, _merge, validate_config
from iabot.signals import _dampen_trend


class TestDampenTrend(unittest.TestCase):
    def _cfg(self, **kw):
        return {"analysis": {"trend_dampen": dict(kw)}}

    def test_disabled_is_identity(self):
        for s in (-1.0, -0.3, 0.0, 0.55, 1.0):
            self.assertAlmostEqual(_dampen_trend(s, self._cfg(enabled=False)), s)
        self.assertAlmostEqual(_dampen_trend(1.0, None), 1.0)

    def test_below_threshold_untouched(self):
        cfg = self._cfg(enabled=True, dampen_at=0.6, k=1.0)
        self.assertAlmostEqual(_dampen_trend(0.3, cfg), 0.3)
        self.assertAlmostEqual(_dampen_trend(-0.6, cfg), -0.6)

    def test_extreme_is_reduced_but_sign_kept(self):
        cfg = self._cfg(enabled=True, dampen_at=0.6, k=1.0)
        out = _dampen_trend(1.0, cfg)
        # 0.6 + 0.4/1.4 = 0.8857
        self.assertAlmostEqual(out, 0.6 + 0.4 / 1.4, places=6)
        self.assertLess(out, 1.0)
        self.assertGreater(out, 0.6)
        self.assertAlmostEqual(_dampen_trend(-1.0, cfg), -out, places=6)

    def test_larger_k_reduces_less(self):
        weak = _dampen_trend(0.95, self._cfg(enabled=True, dampen_at=0.6, k=5.0))
        strong = _dampen_trend(0.95, self._cfg(enabled=True, dampen_at=0.6, k=0.3))
        self.assertGreater(weak, strong)

    def test_monotone_in_score(self):
        cfg = self._cfg(enabled=True, dampen_at=0.6, k=1.0)
        xs = [i / 20 for i in range(21)]
        ys = [_dampen_trend(x, cfg) for x in xs]
        for a, b in zip(ys, ys[1:]):
            self.assertLessEqual(a, b + 1e-9)


class TestTrendDampenConfig(unittest.TestCase):
    def test_default_off(self):
        td = DEFAULTS["analysis"]["trend_dampen"]
        self.assertFalse(td["enabled"])
        self.assertAlmostEqual(td["dampen_at"], 0.6)

    def test_coercion(self):
        cfg = validate_config(_merge(copy.deepcopy(DEFAULTS), {
            "analysis": {"trend_dampen": {"enabled": True,
                                          "dampen_at": -1, "k": 0}}}))
        td = cfg["analysis"]["trend_dampen"]
        self.assertTrue(td["enabled"])
        self.assertAlmostEqual(td["dampen_at"], 0.1)   # 夹到下限
        self.assertAlmostEqual(td["k"], 0.05)

    def test_garbage_falls_back(self):
        cfg = validate_config(_merge(copy.deepcopy(DEFAULTS), {
            "analysis": {"trend_dampen": {"enabled": True,
                                          "dampen_at": "x", "k": "y"}}}))
        td = cfg["analysis"]["trend_dampen"]
        self.assertAlmostEqual(td["dampen_at"], 0.6)
        self.assertAlmostEqual(td["k"], 1.0)


if __name__ == "__main__":
    unittest.main()
