"""参数方案（ProfileStore）的单元测试。"""

import copy
import unittest

from iabot.config import DEFAULTS
from iabot.profiles import ProfileStore, cfg_with_profile, backtest_all
from iabot.signals import FACTOR_DEFAULTS


class TestProfiles(unittest.TestCase):
    def _store(self):
        cfg = copy.deepcopy(DEFAULTS)
        return cfg, ProfileStore(cfg)

    def test_empty_by_default(self):
        cfg, ps = self._store()
        self.assertEqual(ps.list(), [])
        self.assertIsNone(ps.active)

    def test_save_normalizes_weights(self):
        cfg, ps = self._store()
        p = ps.save("稳健", factor_weights={"trend_daily": 0.5, "bad_name": 0.9},
                    score_threshold=40, mode="swing", note="测试")
        self.assertEqual(p["factor_weights"]["trend_daily"], 0.5)
        # 未知名被丢掉补默认
        self.assertNotIn("bad_name", p["factor_weights"])
        self.assertEqual(len(p["factor_weights"]), len(FACTOR_DEFAULTS))
        self.assertEqual(p["score_threshold"], 40.0)
        self.assertEqual(p["mode"], "swing")
        self.assertEqual(p["note"], "测试")

    def test_save_empty_name_rejected(self):
        cfg, ps = self._store()
        with self.assertRaises(ValueError):
            ps.save("  ", factor_weights={}, score_threshold=30, mode="intraday")

    def test_delete_clears_active(self):
        cfg, ps = self._store()
        ps.save("a", factor_weights={}, score_threshold=30, mode="intraday")
        ps.apply("a")
        self.assertEqual(ps.active, "a")
        self.assertTrue(ps.delete("a"))
        self.assertIsNone(ps.active)
        self.assertFalse(ps.delete("a"))

    def test_apply_writes_runtime_config(self):
        cfg, ps = self._store()
        ps.save("进取", factor_weights={"momentum": 0.4}, score_threshold=45,
                mode="swing")
        ps.apply("进取")
        self.assertEqual(cfg["analysis"]["factor_weights"]["momentum"], 0.4)
        self.assertEqual(cfg["analysis"]["score_threshold"], 45.0)
        self.assertEqual(cfg["schedule"]["mode"], "swing")
        self.assertEqual(cfg["active_profile"], "进取")

    def test_apply_missing_raises(self):
        cfg, ps = self._store()
        with self.assertRaises(ValueError):
            ps.apply("不存在")

    def test_cfg_with_profile_does_not_mutate(self):
        cfg = copy.deepcopy(DEFAULTS)
        prof = {"factor_weights": {"trend_daily": 0.5}, "score_threshold": 50,
                "mode": "swing"}
        out = cfg_with_profile(cfg, prof)
        self.assertEqual(out["analysis"]["score_threshold"], 50.0)
        self.assertEqual(out["analysis"]["factor_weights"]["trend_daily"], 0.5)
        # 原 cfg 不变
        self.assertEqual(cfg["analysis"]["score_threshold"], 30)

    def test_backtest_all_compares_sorted(self):
        from iabot.backtest import backtest
        from iabot.market import Snapshot
        import random

        # 构造一份小历史快照（只要能跑 backtest 即可）
        cfg = copy.deepcopy(DEFAULTS)
        cfg["analysis"]["score_threshold"] = 15
        ps = ProfileStore(cfg)
        ps.save("进取", factor_weights={"momentum": 0.4}, score_threshold=20,
                mode="intraday")

        rnd = random.Random(3)
        closes = [30000.0]
        for i in range(120):
            closes.append(closes[-1] * (1 + rnd.uniform(-0.02, 0.02)))
        k = [__import__("iabot.market", fromlist=["Candle"]).Candle(
            ts=1_700_000_000 + i * 900, open=c, high=c * 1.01, low=c * 0.99,
            close=c, volume=1000.0) for i, c in enumerate(closes)]
        snap = Snapshot(symbol="BTC-USDT", price=closes[-1],
                        klines={"15min": k, "60min": k, "1day": k},
                        contract_size=0.001, funding_rate=0.0001)

        rows = backtest_all(cfg, snapshot=snap, symbol="BTC-USDT",
                            bars=300, step=8)
        names = [r["name"] for r in rows]
        self.assertIn("当前参数", names)
        self.assertIn("进取", names)
        # 按期望 R 降序
        exps = [r["result"]["expectancy_r"] for r in rows]
        self.assertEqual(exps, sorted(exps, reverse=True))


if __name__ == "__main__":
    unittest.main()
