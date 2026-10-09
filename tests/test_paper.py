"""模拟盘持仓追踪的单元测试（不联网）。"""

import tempfile
import time
import unittest
from pathlib import Path

from iabot.market import Candle
from iabot.paper import check_exit, pnl_of
from iabot.store import SignalStore

BASE = 1_700_000_000


def _bar(ts, h, l):
    return Candle(ts=ts, open=(h + l) / 2, high=h, low=l, close=(h + l) / 2, volume=1.0)


class TestPnl(unittest.TestCase):
    def test_long_profit(self):
        pos = {"entry_price": 100, "size_coin": 2, "leverage": 10, "direction": "long",
               "stop": 95, "tp1": 110, "status": "open"}
        p = pnl_of(pos, 105)
        self.assertAlmostEqual(p["unrealized_pnl_usdt"], 10.0)   # (105-100)*2
        self.assertAlmostEqual(p["notional_usdt"], 200.0)
        self.assertAlmostEqual(p["margin_usdt"], 20.0)
        self.assertGreater(p["pnl_pct"], 0)
        self.assertLess(p["distance_to_stop_pct"], 0)  # 现价高于止损

    def test_short_profit_when_price_drops(self):
        pos = {"entry_price": 100, "size_coin": 2, "leverage": 10, "direction": "short",
               "stop": 105, "tp1": 90, "status": "open"}
        p = pnl_of(pos, 95)
        self.assertAlmostEqual(p["unrealized_pnl_usdt"], 10.0)   # (100-95)*2

    def test_long_loss(self):
        pos = {"entry_price": 100, "size_coin": 2, "leverage": 10, "direction": "long"}
        p = pnl_of(pos, 98)
        self.assertAlmostEqual(p["unrealized_pnl_usdt"], -4.0)
        self.assertLess(p["pnl_pct"], 0)

    def test_no_price_returns_zero(self):
        pos = {"entry_price": 100, "size_coin": 2, "leverage": 10, "direction": "long"}
        p = pnl_of(pos, None)
        self.assertEqual(p["unrealized_pnl_usdt"], 0.0)


class TestCheckExit(unittest.TestCase):
    def test_long_stop_hit(self):
        pos = {"direction": "long", "entry_price": 100, "stop": 95, "tp1": 110,
               "opened_at": BASE, "tp2": None, "tp3": None}
        candles = [_bar(BASE + 3600, 102, 94)]  # low=94 破止损 95
        hit = check_exit(pos, candles)
        self.assertEqual(hit["reason"], "stop")

    def test_long_tp1_hit(self):
        pos = {"direction": "long", "entry_price": 100, "stop": 95, "tp1": 110,
               "opened_at": BASE, "tp2": 120, "tp3": None}
        candles = [_bar(BASE + 3600, 111, 99)]
        hit = check_exit(pos, candles)
        self.assertEqual(hit["reason"], "tp")
        self.assertEqual(hit["tp_index"], 1)

    def test_stop_wins_same_bar(self):
        pos = {"direction": "long", "entry_price": 100, "stop": 95, "tp1": 110,
               "opened_at": BASE, "tp2": None, "tp3": None}
        candles = [_bar(BASE + 3600, 111, 94)]  # 同一根同时破止损和 tp
        hit = check_exit(pos, candles)
        self.assertEqual(hit["reason"], "stop")  # 保守：止损优先

    def test_short_stop_hit(self):
        pos = {"direction": "short", "entry_price": 100, "stop": 105, "tp1": 90,
               "opened_at": BASE, "tp2": None, "tp3": None}
        candles = [_bar(BASE + 3600, 106, 95)]  # high=106 破止损
        hit = check_exit(pos, candles)
        self.assertEqual(hit["reason"], "stop")

    def test_closest_tp_first(self):
        pos = {"direction": "long", "entry_price": 100, "stop": 95, "tp1": 105,
               "tp2": 110, "tp3": 120, "opened_at": BASE}
        candles = [_bar(BASE + 3600, 106, 99)]  # 只破 tp1
        hit = check_exit(pos, candles)
        self.assertEqual(hit["tp_index"], 1)

    def test_no_hit_returns_none(self):
        pos = {"direction": "long", "entry_price": 100, "stop": 95, "tp1": 110,
               "opened_at": BASE, "tp2": None, "tp3": None}
        candles = [_bar(BASE + 3600, 103, 98)]
        self.assertIsNone(check_exit(pos, candles))

    def test_skips_candles_before_open(self):
        pos = {"direction": "long", "entry_price": 100, "stop": 95, "tp1": 110,
               "opened_at": BASE, "tp2": None, "tp3": None}
        candles = [_bar(BASE - 100, 200, 80)]  # 开仓前，不该判定
        self.assertIsNone(check_exit(pos, candles))


class TestPositionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SignalStore(str(Path(self.tmp.name) / "s.db"), keep_days=30)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_open_close_position(self):
        pid = self.store.open_position(symbol="BTC-USDT", direction="long",
                                      entry_price=100, size_coin=1, leverage=10,
                                      stop=95, tp1=110)
        self.assertEqual(len(self.store.positions(status="open")), 1)
        self.store.close_position(pid, price=110, reason="tp1")
        self.assertEqual(len(self.store.positions(status="open")), 0)
        pos = self.store.position(pid)
        self.assertEqual(pos["status"], "closed")
        self.assertEqual(pos["close_reason"], "tp1")
        self.assertAlmostEqual(pos["close_price"], 110)

    def test_delete_position(self):
        pid = self.store.open_position(symbol="ETH-USDT", direction="short",
                                      entry_price=200, size_coin=1)
        self.assertTrue(self.store.delete_position(pid))
        self.assertIsNone(self.store.position(pid))


if __name__ == "__main__":
    unittest.main()
