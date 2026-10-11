"""强平价估算与止损有效性校验的单元测试。"""

import unittest

from iabot.risk import (DEFAULT_MMR, liquidation_price, safe_max_leverage,
                        stop_is_effective)


class TestLiquidationPrice(unittest.TestCase):
    def test_long_10x(self):
        # 多仓 10x：100 × (1 - 0.1 + 0.005) = 90.5
        self.assertAlmostEqual(liquidation_price(100, 10, "long"), 90.5, places=6)

    def test_short_10x(self):
        # 空仓 10x：100 × (1 + 0.1 - 0.005) = 109.5
        self.assertAlmostEqual(liquidation_price(100, 10, "short"), 109.5, places=6)

    def test_no_leverage_no_liquidation(self):
        self.assertIsNone(liquidation_price(100, 1, "long"))
        self.assertIsNone(liquidation_price(100, 0, "long"))

    def test_higher_leverage_closer(self):
        near = liquidation_price(100, 50, "long")
        far = liquidation_price(100, 10, "long")
        self.assertGreater(near, far)      # 杠杆越高强平越近

    def test_unknown_direction(self):
        self.assertIsNone(liquidation_price(100, 10, "flat"))


class TestStopEffectiveness(unittest.TestCase):
    def test_normal_10x_long_stop_is_effective(self):
        # 10x 强平 90.5，止损 98（更近）-> 有效
        ok, liq = stop_is_effective(100, 98, 10, "long")
        self.assertTrue(ok)
        self.assertAlmostEqual(liq, 90.5, places=6)

    def test_extreme_leverage_long_stop_is_ineffective(self):
        # 100x 强平 99.5，止损 98（在强平价下方）-> 价格先到 99.5 就被强平，止损失效
        ok, liq = stop_is_effective(100, 98, 100, "long")
        self.assertFalse(ok)

    def test_short_symmetric(self):
        ok, _ = stop_is_effective(100, 102, 10, "short")
        self.assertTrue(ok)                # 强平 109.5，止损 102 更近
        ok2, _ = stop_is_effective(100, 102, 100, "short")
        self.assertFalse(ok2)              # 100x 强平 100.5 < 止损 102，止损失效


class TestSafeMaxLeverage(unittest.TestCase):
    def test_wide_stop_allows_high_leverage(self):
        # 止损 5%：1/(0.05+0.005) ≈ 18.18，向下取整 18
        self.assertAlmostEqual(safe_max_leverage(5.0, "long"), 18.0, places=6)

    def test_tight_stop_caps_leverage(self):
        # 止损 0.5%：1/0.01 = 100 恰在边界，取 99（严格小于才安全）
        self.assertAlmostEqual(safe_max_leverage(0.5, "long"), 99.0, places=6)

    def test_returns_cap_at_125(self):
        # 止损 0.2%：1/0.007 ≈ 142.8，超过 125 上限，应被截断
        self.assertEqual(safe_max_leverage(0.2, "long"), 125.0)

    def test_zero_stop(self):
        self.assertEqual(safe_max_leverage(0, "long"), 1.0)

    def test_safe_leverage_actually_works(self):
        """反推出的杠杆上限必须真的让止损先生效（这是该函数存在的意义）。"""
        for stop_pct in (0.5, 1.0, 2.0, 5.0):
            lev = safe_max_leverage(stop_pct, "long")
            entry = 100.0
            stop = entry * (1 - stop_pct / 100.0)
            ok, _ = stop_is_effective(entry, stop, lev, "long")
            self.assertTrue(ok, f"止损 {stop_pct}% @ {lev:.1f}x 应当有效")


class TestMmrSane(unittest.TestCase):
    def test_default_mmr(self):
        self.assertEqual(DEFAULT_MMR, 0.005)


if __name__ == "__main__":
    unittest.main()
