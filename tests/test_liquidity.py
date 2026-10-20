from __future__ import annotations

import copy
import unittest

from iabot.config import DEFAULTS, _merge, validate_config
from iabot.liquidity import assess, spread_bps
from iabot.market import Snapshot
from iabot.signals import build_plan

from tests.test_backtest import _trading_snapshot


def _cfg_with(analysis: dict) -> dict:
    return validate_config(_merge(copy.deepcopy(DEFAULTS),
                                  {"analysis": analysis}))


def _plan(snap, cfg=None):
    return build_plan(snap, cfg or DEFAULTS, mode="intraday")


class TestSpreadBps(unittest.TestCase):
    def test_normal(self):
        s = Snapshot(symbol="A-USDT", price=100.0, best_bid=99.9, best_ask=100.1)
        self.assertAlmostEqual(spread_bps(s), 20.0, places=6)

    def test_tight(self):
        s = Snapshot(symbol="A-USDT", price=100.0, best_bid=99.99,
                     best_ask=100.01)
        self.assertAlmostEqual(spread_bps(s), 2.0, places=6)

    def test_missing_orderbook(self):
        self.assertIsNone(spread_bps(Snapshot(symbol="A-USDT", price=100.0)))


class TestLiquidityAssess(unittest.TestCase):
    def test_defaults_exist(self):
        lc = DEFAULTS["analysis"]["liquidity"]
        self.assertAlmostEqual(lc["min_turnover_24h"], 5_000_000.0)
        self.assertAlmostEqual(lc["max_spread_bps"], 20.0)
        self.assertFalse(lc["enforce"])

    def test_healthy_contract(self):
        s = Snapshot(symbol="A-USDT", price=100.0, turnover_24h=50_000_000.0,
                     best_bid=99.99, best_ask=100.01)
        out = assess(s, DEFAULTS)
        self.assertTrue(out["ok"])
        self.assertEqual(out["reasons"], [])

    def test_low_turnover_flagged(self):
        s = Snapshot(symbol="A-USDT", price=100.0, turnover_24h=800_000.0)
        out = assess(s, DEFAULTS)
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["reasons"]), 1)
        self.assertIn("成交额", out["reasons"][0])

    def test_wide_spread_flagged(self):
        s = Snapshot(symbol="A-USDT", price=100.0, turnover_24h=99_000_000.0,
                     best_bid=98.0, best_ask=102.0)
        out = assess(s, DEFAULTS)
        self.assertFalse(out["ok"])
        self.assertIn("价差", out["reasons"][0])

    def test_both_flagged(self):
        s = Snapshot(symbol="A-USDT", price=100.0, turnover_24h=1.0,
                     best_bid=95.0, best_ask=105.0)
        out = assess(s, DEFAULTS)
        self.assertEqual(len(out["reasons"]), 2)

    def test_missing_turnover_is_not_illiquid(self):
        # 没数据 != 没流动性：字段缺失时不该误报
        s = Snapshot(symbol="A-USDT", price=100.0)
        self.assertTrue(assess(s, DEFAULTS)["ok"])

    def test_config_override(self):
        cfg = _cfg_with({"liquidity": {"min_turnover_24h": 100.0,
                                       "max_spread_bps": 100.0}})
        s = Snapshot(symbol="A-USDT", price=100.0, turnover_24h=500.0,
                     best_bid=99.995, best_ask=100.005)
        self.assertTrue(assess(s, cfg)["ok"])

    def test_coercion_falls_back_on_garbage(self):
        cfg = _cfg_with({"liquidity": {"min_turnover_24h": "abc",
                                       "max_spread_bps": "xyz"}})
        lc = cfg["analysis"]["liquidity"]
        self.assertAlmostEqual(lc["min_turnover_24h"], 5_000_000.0)
        self.assertAlmostEqual(lc["max_spread_bps"], 20.0)

    def test_zero_disables_check(self):
        cfg = _cfg_with({"liquidity": {"min_turnover_24h": 0,
                                       "max_spread_bps": 0}})
        s = Snapshot(symbol="A-USDT", price=100.0, turnover_24h=1.0,
                     best_bid=95.0, best_ask=105.0)
        self.assertTrue(assess(s, cfg)["ok"])


class TestLiquidityInPlan(unittest.TestCase):
    def test_warning_only_by_default(self):
        # 反空转：默认只警告，方向不能被悄悄改掉
        snap = _trading_snapshot()
        snap.turnover_24h = 1.0
        snap.best_bid, snap.best_ask = 95.0, 105.0
        r = _plan(snap)
        base = _plan(_trading_snapshot())
        self.assertEqual(r.direction, base.direction)
        self.assertFalse(r.market["liquidity"]["ok"])
        self.assertIn("成交额", "；".join(r.warnings))

    def test_enforce_downgrades_to_wait(self):
        snap = _trading_snapshot()
        snap.turnover_24h = 1.0
        snap.best_bid, snap.best_ask = 95.0, 105.0
        cfg = _cfg_with({"liquidity": {"enforce": True}})
        r = _plan(snap, cfg)
        self.assertEqual(r.direction, "wait")
        self.assertIn("liquidity", (r.state_gate or {}))

    def test_healthy_still_passes(self):
        snap = _trading_snapshot()
        snap.turnover_24h = 99_000_000.0
        snap.best_bid, snap.best_ask = 99.99, 100.01
        cfg = _cfg_with({"liquidity": {"enforce": True}})
        r = _plan(snap, cfg)
        self.assertNotIn("liquidity", (r.state_gate or {}))
        self.assertFalse(any("成交额" in w or "价差" in w for w in r.warnings))


if __name__ == "__main__":
    unittest.main()
