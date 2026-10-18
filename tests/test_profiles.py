"""参数方案（ProfileStore）的单元测试。"""

import copy
import random
import unittest

from iabot.config import DEFAULTS
from iabot.market import Candle, Snapshot
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
        for _ in range(120):
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


class TestBacktestAllUsesProfileWeights(unittest.TestCase):
    """回归：对比回测必须真的用上各方案的权重。

    list() 返回的键是 "weights"，cfg_with_profile 读的是 "factor_weights"，
    直接用 list() 的元素会让权重静默回落成默认值 —— 几个方案回测出来完全一样，
    "参数方案对比"这个功能就废了。
    """

    def test_list_entry_loses_weights_but_get_keeps_them(self):
        cfg = copy.deepcopy(DEFAULTS)
        ps = ProfileStore(cfg)
        ps.save("动量", factor_weights={"momentum": 0.9}, score_threshold=30,
                mode="intraday")
        listed = ps.list()[0]
        self.assertIn("weights", listed)
        self.assertNotIn("factor_weights", listed)
        # 这正是曾经踩的坑：list() 的元素喂给 cfg_with_profile 拿不到自定义权重
        self.assertEqual(
            cfg_with_profile(cfg, listed)["analysis"]["factor_weights"]["momentum"],
            FACTOR_DEFAULTS["momentum"])
        # get() 才是完整方案
        self.assertEqual(
            cfg_with_profile(cfg, ps.get("动量"))["analysis"]["factor_weights"]["momentum"],
            0.9)

    def test_backtest_all_honours_weights(self):
        """两个只有权重不同的方案，回测笔数/结果不应完全相同。"""
        cfg = copy.deepcopy(DEFAULTS)
        cfg["analysis"]["score_threshold"] = 12
        ps = ProfileStore(cfg)
        # 同一个币：一个几乎只看动量，一个几乎只看日线趋势
        ps.save("动量派", factor_weights={"momentum": 0.95, "trend_daily": 0.01},
                score_threshold=12, mode="intraday")
        ps.save("趋势派", factor_weights={"momentum": 0.01, "trend_daily": 0.95},
                score_threshold=12, mode="intraday")

        # 三周期必须是同一条走势的降采样，且要带成交量，状态闸门才会开。
        # 共用同一串收盘价时 1day 只有 ~2 根 -> 日线因子永远"数据不足"，
        # 两个方案自然得出相同结果，测不出权重差异。
        rnd = random.Random(11)
        n = 6000
        closes = [100.0]
        for i in range(1, n):
            noise = 0.008 * (1.0 - 0.85 * (i / n))
            drift = 0.001 if (i / n) > 0.6 else 0.0002
            closes.append(closes[-1] * (1 + drift + rnd.uniform(-noise, noise)))

        def bars(period, factor):
            src = [closes[i] for i in range(0, n, factor)]
            secs = {"15min": 900, "60min": 3600, "1day": 86400}[period]
            return [Candle(ts=1_700_000_000 + i * secs, open=c, high=c * 1.012,
                          low=c * 0.988, close=c,
                          volume=1000.0 * (0.5 + 1.0 * i / max(1, len(src) - 1)))
                    for i, c in enumerate(src)]
        snap = Snapshot(symbol="BTC-USDT", price=closes[-1], contract_size=0.001,
                       klines={p: bars(p, f) for p, f in
                               (("15min", 1), ("60min", 4), ("1day", 96))})

        rows = backtest_all(cfg, snapshot=snap, symbol="BTC-USDT", step=10)
        by_name = {r["name"]: r["result"] for r in rows}
        a, b = by_name["动量派"], by_name["趋势派"]
        # 用上了权重 => 两套参数产生的交易不会一模一样
        self.assertNotEqual(
            (a["trades"], a["expectancy_r"]), (b["trades"], b["expectancy_r"]),
            "两个只有权重不同的方案回测结果完全相同，说明权重没生效")

    def test_zero_expectancy_sorts_below_negative(self):
        """0.0 是合法期望值，不能被 `or -999` 当成最差而排到负数后面。"""
        from iabot.profiles import _exp_key
        vals = [0.3, 0.0, -0.5, -1.2]
        self.assertEqual(sorted(vals, key=lambda v: -_exp_key(v)),
                         [0.3, 0.0, -0.5, -1.2])
        self.assertEqual(_exp_key(None), -999.0)
        self.assertEqual(_exp_key("bad"), -999.0)

    def test_threshold_zero_is_preserved(self):
        """显式把阈值设成 0（不设门槛）不能被当成缺省而变成 30。"""
        from iabot.profiles import _thr
        self.assertEqual(_thr(0), 0.0)
        self.assertEqual(_thr(0.0), 0.0)
        self.assertEqual(_thr(None), 30.0)
        self.assertEqual(_thr(""), 30.0)
        self.assertEqual(_thr("bad"), 30.0)
        self.assertEqual(_thr(45), 45.0)


if __name__ == "__main__":
    unittest.main()
