from __future__ import annotations

import unittest

from iabot.costs import impact_bps, participation, trade_cost_bps


class TestParticipation(unittest.TestCase):
    def test_basic_ratio(self):
        self.assertAlmostEqual(participation(100.0, 10_000.0), 0.01)

    def test_clamped_above_one(self):
        self.assertAlmostEqual(participation(20_000.0, 10_000.0), 1.0)

    def test_zero_inputs(self):
        self.assertEqual(participation(0.0, 10_000.0), 0.0)
        self.assertEqual(participation(100.0, 0.0), 0.0)
        self.assertEqual(participation(-100.0, 10_000.0), 0.0)


class TestImpactBps(unittest.TestCase):
    def test_small_order_is_negligible(self):
        # 450U 账户在 5000 万 U 成交额的 BTC 上：冲击 < 0.2bps
        bps = impact_bps(450.0, 50_000_000.0, 30.0)
        self.assertLess(bps, 0.2)

    def test_small_order_on_thin_contract_is_bigger(self):
        thin = impact_bps(450.0, 1_000_000.0, 30.0)
        deep = impact_bps(450.0, 50_000_000.0, 30.0)
        self.assertGreater(thin, deep)
        self.assertLess(thin, 1.0)

    def test_large_order_scales_with_sqrt(self):
        # 两倍资金 -> 冲击 × sqrt(2)
        a = impact_bps(100_000.0, 10_000_000.0, 30.0)
        b = impact_bps(200_000.0, 10_000_000.0, 30.0)
        self.assertAlmostEqual(b / a, 2 ** 0.5, places=6)

    def test_no_adv_means_no_estimate(self):
        self.assertEqual(impact_bps(450.0, 0.0, 30.0), 0.0)

    def test_zero_coeff_disables(self):
        self.assertEqual(impact_bps(1e9, 1e6, 0.0), 0.0)


class TestTradeCostBps(unittest.TestCase):
    def test_taker_round_trip_math(self):
        out = trade_cost_bps(450.0, 50_000_000.0, taker_fee_bps=5.0,
                             maker_fee_bps=2.0, slippage_bps=2.0,
                             impact_coeff_bps=30.0)
        # 手续费 5×2 = 10bps；滑点 (2 + 极小冲击) × 2 ≈ 4bps
        self.assertAlmostEqual(out["fee_bps"], 10.0, places=4)
        self.assertLess(out["slippage_bps"], 4.3)
        self.assertLess(out["total_bps"], 14.3)

    def test_maker_is_cheaper_on_fees(self):
        taker = trade_cost_bps(450.0, 50_000_000.0, taker_fee_bps=5.0,
                               maker_fee_bps=2.0, slippage_bps=2.0)
        maker = trade_cost_bps(450.0, 50_000_000.0, taker_fee_bps=5.0,
                               maker_fee_bps=2.0, slippage_bps=2.0,
                               maker=True)
        self.assertLess(maker["fee_bps"], taker["fee_bps"])
        # 挂单免固定滑点，但仍吃价格冲击
        self.assertLess(maker["slippage_bps"], taker["slippage_bps"])
        self.assertGreater(maker["slippage_bps"], 0.0)

    def test_bigger_order_costs_more(self):
        small = trade_cost_bps(450.0, 5_000_000.0)
        big = trade_cost_bps(4500.0, 5_000_000.0)
        self.assertGreater(big["impact_bps"], small["impact_bps"])
        self.assertGreater(big["total_bps"], small["total_bps"])


class TestImpactInBacktest(unittest.TestCase):
    def test_defaults_exist(self):
        from iabot.config import DEFAULTS
        bc = DEFAULTS["analysis"]["backtest"]
        self.assertAlmostEqual(bc["impact_coeff_bps"], 30.0)
        self.assertAlmostEqual(bc["maker_fee_bps"], 2.0)

    def test_thin_contract_raises_slippage(self):
        # 反空转：同一个账户，成交额越低，回测里的滑点必须越高
        from tests.test_backtest import _bt, _trading_snapshot
        deep = _trading_snapshot()
        deep.turnover_24h = 500_000_000.0
        thin = _trading_snapshot()
        thin.turnover_24h = 2_000_000.0
        rd, rt = _bt(deep), _bt(thin)
        self.assertGreater(rt.avg_slippage_r, rd.avg_slippage_r)
        self.assertGreater(rt.avg_impact_r, rd.avg_impact_r)

    def test_zero_coeff_reverts_to_flat_slippage(self):
        from tests.test_backtest import _bt, _trading_snapshot
        thin = _trading_snapshot()
        thin.turnover_24h = 2_000_000.0
        r = _bt(thin, costs={"impact_coeff_bps": 0.0})
        self.assertAlmostEqual(r.avg_impact_r, 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
