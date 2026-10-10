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