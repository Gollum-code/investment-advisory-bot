"""回测实验记录（参数快照 + 存取）的单元测试。"""

import copy
import tempfile
import unittest
from pathlib import Path

from iabot.btlog import build_params, diff_params, summarize_result
from iabot.config import DEFAULTS
from iabot.signals import FACTOR_DEFAULTS
from iabot.store import SignalStore


class TestBuildParams(unittest.TestCase):
    def test_snapshot_is_complete(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["analysis"]["factor_weights"] = {"momentum": 0.4}
        p = build_params(cfg, symbol="BTC-USDT", mode="swing", bars=300,
                         step=3, warmup=60, equity_usdt=450.0, risk_pct=2.0)
        # 足以复现：模式/步长/阈值/完整权重/共振/成本/账户
        self.assertEqual(p["mode"], "swing")
        self.assertEqual(p["bars"], 300)
        self.assertEqual(p["step"], 3)
        self.assertEqual(p["score_threshold"], 30.0)
        self.assertIn("momentum", p["factor_weights"])
        self.assertIn("trend_daily", p["factor_weights"])  # 未覆盖的也要补默认
        self.assertEqual(p["factor_weights"]["trend_daily"],
                         FACTOR_DEFAULTS["trend_daily"])
        self.assertIn("resonance", p)
        self.assertIn("taker_fee_bps", p["costs"])
        self.assertEqual(p["equity_usdt"], 450.0)
        self.assertEqual(p["risk_pct"], 2.0)

    def test_snapshot_does_not_mutate_cfg(self):
        cfg = copy.deepcopy(DEFAULTS)
        build_params(cfg, symbol="BTC-USDT", mode="intraday", bars=100,
                     step=5, warmup=30, equity_usdt=450.0, risk_pct=2.0)
        self.assertEqual(cfg["analysis"]["factor_weights"], {})


class TestDiffParams(unittest.TestCase):
    def test_detects_weight_change(self):
        a = {"mode": "intraday", "score_threshold": 30,
             "factor_weights": {"momentum": 0.15},
             "resonance": {"enabled": False}, "costs": {}}
        b = {"mode": "intraday", "score_threshold": 40,
             "factor_weights": {"momentum": 0.4},
             "resonance": {"enabled": False}, "costs": {}}
        d = diff_params(a, b)
        self.assertTrue(any("阈值" in x or "score" in x or "30" in x for x in d))
        self.assertTrue(any("momentum" in x for x in d))
        # mode 没变，不该出现在差异里
        self.assertFalse(any("模式" in x for x in d))

    def test_empty_inputs(self):
        self.assertEqual(diff_params({}, {}), [])
        self.assertEqual(diff_params(None, {}), [])


class TestSummarizeResult(unittest.TestCase):
    def test_picks_key_metrics(self):
        r = {"trades": 10, "win_rate": 0.6, "expectancy_r": 0.3,
             "total_r": 3.0, "equity_curve": [1, 2, 3],
             "equity_curve_usdt": [10, 20, 30], "unrelated": "x"}
        s = summarize_result(r)
        self.assertEqual(s["trades"], 10)
        self.assertNotIn("equity_curve", s)      # 长曲线不入库
        self.assertNotIn("equity_curve_usdt", s)
        self.assertIn("total_r", s)


class TestBacktestRunsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SignalStore(str(Path(self.tmp.name) / "s.db"), keep_days=30)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_save_and_query(self):
        rid = self.store.save_backtest_run(
            symbol="BTC-USDT", mode="swing",
            params={"mode": "swing", "score_threshold": 30},
            result={"trades": 10, "win_rate": 0.6, "expectancy_r": 0.3},
            profile="进取", note="test", elapsed_ms=500)
        self.assertIsNotNone(rid)
        rows = self.store.query_backtest_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTC-USDT")
        self.assertEqual(rows[0]["profile"], "进取")
        self.assertEqual(rows[0]["params"]["score_threshold"], 30)
        self.assertEqual(rows[0]["result"]["expectancy_r"], 0.3)

    def test_filter_and_delete(self):
        params = {"mode": "intraday"}
        result = {"trades": 5}
        self.store.save_backtest_run(symbol="BTC-USDT", mode="intraday",
                                     params=params, result=result)
        self.store.save_backtest_run(symbol="ETH-USDT", mode="intraday",
                                     params=params, result=result)
        self.assertEqual(len(self.store.query_backtest_runs(symbol="BTC-USDT")), 1)
        self.assertEqual(len(self.store.query_backtest_runs()), 2)
        rid = self.store.query_backtest_runs()[0]["id"]
        self.assertTrue(self.store.delete_backtest_run(rid))
        self.assertEqual(len(self.store.query_backtest_runs()), 1)

    def test_get_single(self):
        rid = self.store.save_backtest_run(symbol="SOL-USDT", mode="swing",
                                           params={"a": 1}, result={"b": 2})
        r = self.store.backtest_run(rid)
        self.assertEqual(r["symbol"], "SOL-USDT")
        self.assertEqual(r["params"], {"a": 1})
        self.assertIsNone(self.store.backtest_run(999999))


if __name__ == "__main__":
    unittest.main()
