"""币种代码解析与关键价位合并的单元测试。"""

import unittest

from iabot import symbols as sym
from iabot.levels import Level, merge_levels, price_ladder, swing_points
from iabot.market import Candle


class TestToContract(unittest.TestCase):
    def test_plain_code(self):
        self.assertEqual(sym.to_contract("BTC"), "BTC-USDT")

    def test_already_contract(self):
        self.assertEqual(sym.to_contract("eth-usdt"), "ETH-USDT")

    def test_no_dash(self):
        self.assertEqual(sym.to_contract("SOLUSDT"), "SOL-USDT")

    def test_chinese_alias(self):
        self.assertEqual(sym.to_contract("大饼"), "BTC-USDT")
        self.assertEqual(sym.to_contract("狗狗币"), "DOGE-USDT")

    def test_whitespace_and_empty(self):
        self.assertEqual(sym.to_contract("  btc  "), "BTC-USDT")
        self.assertEqual(sym.to_contract(""), "")

    def test_base_of(self):
        self.assertEqual(sym.base_of("BTC-USDT"), "BTC")
        self.assertEqual(sym.base_of(""), "")

    def test_catalog_is_unique_and_well_formed(self):
        codes = [s["code"] for s in sym.catalog_payload()]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertIn("BTC-USDT", codes)
        for row in sym.catalog_payload():
            self.assertTrue(row["code"].endswith("-USDT"))
            self.assertTrue(row["name"])


class TestSwingPoints(unittest.TestCase):
    def _series(self, highs, lows):
        return [Candle(ts=i * 60, open=l, high=h, low=l, close=l, volume=1.0)
                for i, (h, l) in enumerate(zip(highs, lows))]

    def test_finds_peak(self):
        highs = [1, 2, 3, 4, 5, 4, 3, 2, 1]
        lows = [0] * 9
        ph, pl = swing_points(self._series(highs, lows), width=2)
        self.assertIn((4, 5), ph)

    def test_finds_trough(self):
        highs = [5] * 9
        lows = [4, 3, 2, 1, 0, 1, 2, 3, 4]
        ph, pl = swing_points(self._series(highs, lows), width=2)
        self.assertIn((4, 0), pl)

    def test_too_short_for_width(self):
        ph, pl = swing_points(self._series([1] * 4, [0] * 4), width=3)
        self.assertEqual((ph, pl), ([], []))


class TestLevelMerge(unittest.TestCase):
    def test_nearby_levels_merge_and_weights_add(self):
        lv = [Level(100.0, "resistance", 0.3, "A"), Level(100.05, "resistance", 0.4, "B")]
        merged = merge_levels(100.0, lv, cluster_pct=0.1)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0].weight, 0.7, places=6)

    def test_far_levels_stay_separate(self):
        lv = [Level(100.0, "resistance", 0.3, "A"), Level(110.0, "resistance", 0.4, "B")]
        self.assertEqual(len(merge_levels(100.0, lv, cluster_pct=0.1)), 2)

    def test_weight_is_capped_at_one(self):
        lv = [Level(100.0, "resistance", 0.8, "A"), Level(100.01, "resistance", 0.8, "B")]
        self.assertLessEqual(merge_levels(100.0, lv)[0].weight, 1.0)


class TestPriceLadder(unittest.TestCase):
    def _levels(self):
        return [Level(90.0, "support", 0.5, "s90"),
                Level(95.0, "support", 0.5, "s95"),
                Level(105.0, "resistance", 0.6, "r105"),
                Level(120.0, "resistance", 0.7, "r120")]

    def test_up_side_only_returns_resistance(self):
        up = price_ladder(100.0, self._levels(), min_dist=1.0, max_dist=30.0, direction="up")
        self.assertTrue(up)
        for row in up:
            self.assertGreater(row["price"], 100.0)

    def test_down_side_only_returns_support(self):
        dn = price_ladder(100.0, self._levels(), min_dist=1.0, max_dist=30.0, direction="down")
        self.assertTrue(dn)
        for row in dn:
            self.assertLess(row["price"], 100.0)

    def test_max_dist_filters_far_levels(self):
        up = price_ladder(100.0, self._levels(), min_dist=1.0, max_dist=10.0, direction="up")
        self.assertEqual([r["price"] for r in up], [105.0])

    def test_min_dist_filters_noise(self):
        dn = price_ladder(100.0, self._levels(), min_dist=8.0, max_dist=30.0, direction="down")
        self.assertEqual([r["price"] for r in dn], [90.0])

    def test_no_price_is_empty(self):
        self.assertEqual(price_ladder(0.0, self._levels(), min_dist=1.0,
                                      max_dist=30.0, direction="up"), [])

    def test_max_each_is_respected(self):
        many = [Level(100.0 + i, "resistance", 1.0 - i * 0.01, f"r{i}") for i in range(1, 20)]
        self.assertLessEqual(len(price_ladder(100.0, many, min_dist=0.5,
                                              max_dist=50.0, max_each=3)), 3)


if __name__ == "__main__":
    unittest.main()