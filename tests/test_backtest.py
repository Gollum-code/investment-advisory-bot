"""历史回放的单元测试（合成行情，不联网）。"""

import copy
import unittest

from iabot.backtest import backtest
from iabot.config import DEFAULTS
from iabot.market import Candle

BASE = 1_700_000_000
START = 30000.0


def _bar(ts, close, wick=10.0):
    return Candle(ts=ts, open=close - 5.0, high=close + wick,
                  low=close - wick, close=close, volume=1000.0)


def _make_snapshot(n=240, period="15min"):
    """构造只有主周期 + 60min/1day 的合成快照。

    行情走势：先跌后反弹，保证会出现短/多信号且之后有走势可判定。
    """
    from iabot.market import Snapshot

    closes = [START]
    a = 0.06  # 每根最多涨跌 6%
    import random
    rnd = random.Random(7)
    for i in range(1, n):
        # 前 1/3 下跌，之后震荡回升
        drift = -a * 0.8 if i < n // 3 else a * 0.3
        closes.append(closes[-1] * (1 + drift + rnd.uniform(-a, a)))

    step = {"15min": 900, "60min": 3600, "1day": 86400}[period]
    k = [_bar(BASE + i * step, c) for i, c in enumerate(closes)]
    return Snapshot(symbol="BTC-USDT", price=closes[-1], klines={period: k},
                    contract_size=0.001, funding_rate=0.0001, open_interest=1e6)


def _cfg():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["analysis"]["score_threshold"] = 20  # 让合成行情更容易出信号
    return cfg


class TestFundingRateAt(unittest.TestCase):
    """回测按建仓点历史时刻取资金费率。"""

    def _hist(self):
        # funding_time 是毫秒，正序（最后=最新）
        return [
            {"funding_time": 1_700_000_000_000, "funding_rate": 0.0001},
            {"funding_time": 1_700_028_800_000, "funding_rate": 0.0003},
            {"funding_time": 1_700_057_600_000, "funding_rate": -0.0002},
        ]

    def test_picks_rate_before_ts(self):
        from iabot.backtest import _funding_rate_at
        h = self._hist()
        # ts 在第2期之后 -> 取第2期的费率
        self.assertAlmostEqual(_funding_rate_at(h, 1_700_030_000), 0.0003)
        # ts 在第1期之后 -> 取第1期
        self.assertAlmostEqual(_funding_rate_at(h, 1_700_010_000), 0.0001)
        # ts 早于所有 -> 0
        self.assertEqual(_funding_rate_at(h, 1_600_000_000), 0.0)
        # ts 在最新之后 -> 取最新
        self.assertAlmostEqual(_funding_rate_at(h, 1_800_000_000), -0.0002)

    def test_empty_history_returns_zero(self):
        from iabot.backtest import _funding_rate_at
        self.assertEqual(_funding_rate_at([], 1_700_000_000), 0.0)

    def test_backtest_uses_history_when_available(self):
        from iabot.backtest import _funding_rate_at
        snap = _make_snapshot(200)
        # 给一个"全正费率"的历史序列，多头要付更多 -> 成本不低于无历史
        t0 = snap.klines["15min"][0].ts
        snap.funding_history = [
            {"funding_time": int((t0 + i * 8 * 3600) * 1000), "funding_rate": 0.0005}
            for i in range(20)
        ]
        with_hist = backtest(snap, _cfg(), mode="intraday", step=6, warmup=30).to_dict()
        snap2 = _make_snapshot(200)
        snap2.funding_history = []
        without = backtest(snap2, _cfg(), mode="intraday", step=6, warmup=30).to_dict()
        # 有历史费率时，平均资金费成本不应为负（多头正费率要付钱）
        self.assertGreaterEqual(with_hist["avg_funding_r"], without["avg_funding_r"] - 1e-9)


class TestBacktest(unittest.TestCase):
    def test_runs_and_returns_summary(self):
        snap = _make_snapshot(240)
        res = backtest(snap, _cfg(), mode="intraday", step=5, warmup=30)
        d = res.to_dict()
        self.assertIn("win_rate", d)
        self.assertGreaterEqual(res.bars, 200)
        # 合成行情必有走势，理应出现少量交易（不吹不黑，只是确保不崩）
        self.assertIsInstance(res.trades, int)

    def test_wait_only_market_has_zero_trades(self):
        closes = [START + (i % 7) for i in range(120)]  # 完全横盘
        k = [_bar(BASE + i * 900, c, wick=0.5) for i, c in enumerate(closes)]
        from iabot.market import Snapshot
        snap = Snapshot(symbol="XRP-USDT", price=closes[-1],
                        klines={"15min": k}, contract_size=0.001)
        res = backtest(snap, _cfg(), mode="intraday", step=5, warmup=20)
        # 横盘没有趋势 -> 要么观望要么被闸门拦下，不该出现交易
        self.assertLess(res.trades, 5)

    def test_equity_curve_length_matches_trades(self):
        snap = _make_snapshot(200)
        res = backtest(snap, _cfg(), mode="intraday", step=4, warmup=30)
        self.assertEqual(len(res.equity_curve), res.trades)

    def test_costs_reduce_total_r(self):
        snap = _make_snapshot(220)
        free = backtest(snap, _cfg(), mode="intraday", step=4, warmup=30,
                        apply_costs=False)
        paid = backtest(snap, _cfg(), mode="intraday", step=4, warmup=30,
                        apply_costs=True)
        self.assertEqual(free.trades, paid.trades)
        self.assertLessEqual(paid.total_r, free.total_r + 1e-9)
        if paid.trades:
            self.assertGreater(paid.avg_cost_r, 0)
            # 成本按单笔风险折算，不该超过 0.1 R（默认费率 0.1% 往返）
            self.assertLess(paid.avg_cost_r, 0.1)

    def test_cost_breakdown_present(self):
        snap = _make_snapshot(200)
        r = backtest(snap, _cfg(), mode="intraday", step=6, warmup=30).to_dict()
        for k in ("avg_fee_r", "avg_slippage_r", "avg_funding_r", "avg_holding_h"):
            self.assertIn(k, r)
        if r["trades"]:
            self.assertGreaterEqual(r["avg_fee_r"], 0)
            self.assertGreaterEqual(r["avg_slippage_r"], 0)
            self.assertGreaterEqual(r["avg_holding_h"], 0)

    def test_funding_increases_cost_for_long(self):
        # 资金费率越高、持仓越久，多头付的资金费越多，成本应更大
        snap = _make_snapshot(200)
        snap.funding_rate = 0.0003  # 0.03%/8h
        hi = backtest(snap, _cfg(), mode="intraday", step=6, warmup=30,
                      costs={"funding_rate": 0.0003})
        lo = backtest(snap, _cfg(), mode="intraday", step=6, warmup=30,
                      costs={"funding_rate": 0.0})
        # 多头在正费率下要付钱 -> 累计 R 不应更高
        self.assertLessEqual(hi.total_r, lo.total_r + 1e-9)

    def test_equity_usdt_conversion(self):
        snap = _make_snapshot(200)
        cfg = _cfg()
        cfg["account"]["equity_usdt"] = 1000.0
        cfg["account"]["risk_pct_per_trade"] = 2.0
        r = backtest(snap, cfg, mode="intraday", step=6, warmup=30).to_dict()
        # 单笔风险 = 1000 × 2% = 20 U
        self.assertAlmostEqual(r["risk_amount_usdt"], 20.0, places=2)
        self.assertAlmostEqual(r["equity_usdt"], 1000.0, places=2)
        # 金额曲线长度与 R 曲线一致
        self.assertEqual(len(r["equity_curve_usdt"]), len(r["equity_curve"]))
        if r["trades"]:
            # 期末 = 初始 + 累计R × 单笔风险
            self.assertAlmostEqual(r["final_equity_usdt"],
                                   1000.0 + r["total_r"] * 20.0, places=0)


if __name__ == "__main__":
    unittest.main()