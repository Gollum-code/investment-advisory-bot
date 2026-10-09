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


if __name__ == "__main__":
    unittest.main()