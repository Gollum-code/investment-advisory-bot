"""信号结局判定的单元测试（用合成 K 线，不联网）。"""

import unittest

from iabot.market import Candle
from iabot.outcomes import evaluate_outcome

BASE_TS = 1_700_000_000


def _candle(ts, o, h, l, c, v=1000.0):
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def _plan(direction="long", generated_at=BASE_TS + 100, **extra):
    """一份可判定的做多计划：入场 100-101，止损 98，TP1=104 / TP2=106。"""
    p = {
        "symbol": "TEST-USDT", "mode": "intraday", "direction": direction,
        "generated_at": generated_at, "entry_low": 100.0, "entry_high": 101.0,
        "stop": 98.0, "score": 55.0,
        "targets": [{"price": 104.0, "rr": 2.0, "label": "TP1"},
                    {"price": 106.0, "rr": 3.0, "label": "TP2"}],
    }
    p.update(extra)
    return p


class TestEvaluateOutcome(unittest.TestCase):
    def test_hits_tp1_first(self):
        candles = [
            _candle(BASE_TS + 1000, 101, 102, 100.5, 101.5),
            _candle(BASE_TS + 1900, 101.5, 105, 101, 104.5),  # high 破 TP1=104
        ]
        res = evaluate_outcome(_plan(), candles)
        self.assertEqual(res["outcome"], "tp1")
        self.assertEqual(res["rr"], 2.0)

    def test_stop_wins_over_target(self):
        candles = [
            # low=97 破止损 98，high=105 破 TP1=104，同一根都碰到 -> 按止损（保守）
            _candle(BASE_TS + 1000, 101, 105, 97, 101),
        ]
        res = evaluate_outcome(_plan(), candles)
        self.assertEqual(res["outcome"], "stopped")
        self.assertEqual(res["rr"], -1.0)

    def test_picks_closest_target_first(self):
        # 一根大阳线同时越过 TP1 和 TP2，应记最近的那档
        candles = [_candle(BASE_TS + 1000, 101, 107, 100.5, 106.5)]
        res = evaluate_outcome(_plan(), candles)
        self.assertEqual(res["outcome"], "tp1")

    def test_timeout_when_nothing_hit(self):
        candles = [_candle(BASE_TS + 1000, 100.5, 101, 100, 100.5)]
        res = evaluate_outcome(_plan(), candles)
        self.assertEqual(res["outcome"], "timeout")
        self.assertIsNone(res["rr"])

    def test_short_stop_and_target(self):
        short_plan = _plan(direction="short")
        short_plan.update({"entry_low": 100.0, "entry_high": 101.0, "stop": 103.0,
                           "targets": [{"price": 96.0, "rr": 2.0, "label": "TP1"}]})
        candles = [_candle(BASE_TS + 1000, 101, 101.5, 95, 95.5)]  # low 破 TP1
        res = evaluate_outcome(short_plan, candles)
        self.assertEqual(res["outcome"], "tp1")

    def test_skips_candles_before_signal(self):
        # 信号生成之前/生成后 5 分钟内的 K 线不该计入
        candles = [
            _candle(BASE_TS, 100, 120, 90, 95),  # 早于 generated_at，忽略
            _candle(BASE_TS + 1000, 101, 101.5, 100.5, 101),  # 中性
        ]
        res = evaluate_outcome(_plan(), candles)
        self.assertEqual(res["outcome"], "timeout")

    def test_tracks_mfe_and_mae(self):
        candles = [
            _candle(BASE_TS + 1000, 101, 103, 99, 102),  # 有利 +2%, 不利 -1%
            _candle(BASE_TS + 1900, 102, 102.5, 97, 97.5),  # 跌破止损
        ]
        res = evaluate_outcome(_plan(), candles)
        self.assertEqual(res["outcome"], "stopped")
        self.assertGreater(res["mfe_pct"], 0)
        self.assertLess(res["mae_pct"], 0)

    def test_wait_has_no_outcome(self):
        self.assertIsNone(evaluate_outcome(_plan(direction="wait"), [
            _candle(BASE_TS + 1000, 100, 110, 90, 95)]))

    def test_no_following_candles_returns_none(self):
        # 还没有任何后续 K 线 -> 交给下一轮再判，不记 timeout
        candles = [_candle(BASE_TS, 100, 101, 99, 100)]  # 全在信号之前
        self.assertIsNone(evaluate_outcome(_plan(), candles))


if __name__ == "__main__":
    unittest.main()
