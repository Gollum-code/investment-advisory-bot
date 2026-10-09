"""指标计算的单元测试（纯标准库，不联网）。"""

import math
import unittest

from iabot import indicators as ind


class TestSma(unittest.TestCase):
    def test_last_value(self):
        self.assertAlmostEqual(ind.last(ind.sma([1, 2, 3, 4, 5], 3)), 4.0)

    def test_padding_is_none(self):
        out = ind.sma([1, 2, 3, 4], 3)
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)

    def test_length_preserved_and_zero_window(self):
        self.assertEqual(len(ind.sma([1, 2, 3], 2)), 3)
        self.assertTrue(all(v is None for v in ind.sma([1, 2, 3], 0)))

    def test_window_longer_than_series(self):
        self.assertTrue(all(v is None for v in ind.sma([1, 2], 5)))


class TestEma(unittest.TestCase):
    def test_seed_is_sma_then_reacts(self):
        vals = [10.0] * 10 + [20.0] * 5
        out = ind.ema(vals, 5)
        self.assertAlmostEqual(out[4], 10.0)
        self.assertGreater(ind.last(out), 10.0)
        self.assertLess(ind.last(out), 20.0)

    def test_flat_series_stays_flat(self):
        out = ind.ema([7.0] * 20, 5)
        self.assertAlmostEqual(ind.last(out), 7.0)


class TestRsi(unittest.TestCase):
    def test_monotonic_up_is_100(self):
        self.assertAlmostEqual(ind.last(ind.rsi(list(range(1, 40)), 14)), 100.0)

    def test_monotonic_down_is_0(self):
        self.assertAlmostEqual(ind.last(ind.rsi(list(range(40, 1, -1)), 14)), 0.0)

    def test_flat_series_is_in_range(self):
        v = ind.last(ind.rsi([5.0] * 30, 14))
        self.assertTrue(v is None or 0.0 <= v <= 100.0)

    def test_too_short_returns_all_none(self):
        self.assertTrue(all(v is None for v in ind.rsi([1.0] * 5, 14)))


class TestAtrMacdBollinger(unittest.TestCase):
    def test_atr_on_constant_range(self):
        n = 40
        high = [10.0 + 0.5] * n
        low = [10.0 - 0.5] * n
        close = [10.0] * n
        self.assertAlmostEqual(ind.last(ind.atr(high, low, close, 14)), 1.0, places=6)

    def test_atr_shrinks_with_narrower_bars(self):
        wide = ind.last(ind.atr([11.0] * 40, [9.0] * 40, [10.0] * 40, 14))
        narrow = ind.last(ind.atr([10.1] * 40, [9.9] * 40, [10.0] * 40, 14))
        self.assertGreater(wide, narrow)

    def test_macd_hist_positive_when_gains_accelerate(self):
        vals = [100.0 * (1.02 ** i) for i in range(90)]
        _macd, _sig, hist = ind.macd(vals)
        self.assertGreater(ind.last(hist), 0)
        self.assertGreater(ind.last(hist), 0.5)

    def test_macd_line_negative_in_downtrend(self):
        vals = [100.0 * (0.98 ** i) for i in range(90)]
        macd_line, _sig, _hist = ind.macd(vals)
        self.assertLess(ind.last(macd_line), 0)

    def test_bollinger_ordering(self):
        vals = [100.0 + math.sin(i / 3.0) * 5 for i in range(60)]
        up, mid, dn = ind.bollinger(vals, 20, 2.0)
        self.assertGreater(ind.last(up), ind.last(mid))
        self.assertGreater(ind.last(mid), ind.last(dn))
        self.assertIsNone(up[5])


class TestVwapVolAdx(unittest.TestCase):
    def test_vwap_is_volume_weighted(self):
        high = [10.0, 10.0]
        low = [10.0, 10.0]
        close = [10.0, 20.0]
        volume = [9.0, 1.0]
        # VWAP 用典型价 (h+l+c)/3：第 1 根 10，第 2 根 40/3
        expect = (10.0 * 9.0 + (40.0 / 3.0) * 1.0) / 10.0
        self.assertAlmostEqual(ind.last(ind.vwap(high, low, close, volume)), expect)

    def test_vwap_respects_start(self):
        out = ind.vwap([1.0, 1.0], [1.0, 1.0], [1.0, 9.0], [1.0, 1.0], start=1)
        self.assertIsNone(out[0])
        self.assertAlmostEqual(out[1], (1.0 + 1.0 + 9.0) / 3.0)

    def test_realized_vol_needs_enough_data(self):
        self.assertIsNone(ind.realized_vol_pct([1.0] * 5, 30, 365))
        self.assertGreater(ind.realized_vol_pct([1.0 + i * 0.01 for i in range(40)], 30, 365), 0)

    def test_adx_high_in_trend_low_in_chop(self):
        n = 80
        h = [100.0 + i for i in range(n)]
        l = [99.0 + i for i in range(n)]
        c = [99.5 + i for i in range(n)]
        self.assertGreater(ind.last(ind.adx(h, l, c, 14)), 50)

        s = [100.0 + (1.0 if i % 2 else -1.0) for i in range(n)]
        flat = ind.last(ind.adx([v + 1 for v in s], [v - 1 for v in s], s, 14))
        self.assertLess(flat, 25)

    def test_pct_change(self):
        self.assertAlmostEqual(ind.pct_change([100.0, 110.0], 1), 10.0)
        self.assertIsNone(ind.pct_change([100.0], 1))
        self.assertAlmostEqual(ind.pct_change([100.0, 0.0, 121.0], 2), 21.0)

    def test_stdev(self):
        self.assertAlmostEqual(ind.stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.13809, places=4)
        self.assertEqual(ind.stdev([1.0]), 0.0)


if __name__ == "__main__":
    unittest.main()