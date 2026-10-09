"""导出报告（SVG K线 + HTML）的单元测试。"""

import unittest

from iabot.market import Candle
from iabot.report import digits, render_report, svg_candles

BASE = 1_700_000_000


def _candles(n=60):
    out = []
    price = 30000.0
    for i in range(n):
        o = price
        c = price + (50 if i % 2 == 0 else -30)
        out.append(Candle(ts=BASE + i * 900, open=o, high=max(o, c) + 20,
                          low=min(o, c) - 20, close=c, volume=1000.0))
        price = c
    return out


def _plan(direction="long"):
    return {
        "symbol": "BTC-USDT", "mode": "intraday", "direction": direction,
        "score": 55.5, "confidence": "高", "confidence_score": 62.0,
        "price": 30000.0, "entry_low": 29900.0, "entry_high": 29950.0,
        "stop": 29700.0, "stop_pct": 0.8, "rr": 1.6,
        "targets": [{"price": 30200.0, "rr": 1.6, "label": "TP1"}],
        "sizing": {"applicable": True, "contracts": 3, "margin_usdt": 90.0,
                   "loss_if_stopped_usdt": 7.0, "loss_pct_of_equity": 1.5},
        "market": {"regime": "趋势", "adx": 28.0, "change_24h_pct": 2.1},
        "factors": [{"label": "日线趋势", "score": 0.8, "weight": 0.2,
                     "contribution": 0.16}],
        "warnings": ["波动率偏高"],
        "entry_note": "等回踩再进",
    }


class TestSvgCandles(unittest.TestCase):
    def test_renders_svg(self):
        svg = svg_candles(_candles())
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        self.assertIn("<rect", svg)

    def test_empty_returns_message(self):
        self.assertIn("无 K 线", svg_candles([]))

    def test_plan_levels_drawn(self):
        svg = svg_candles(_candles(), plan=_plan())
        self.assertIn("止损", svg)
        self.assertIn("入场", svg)
        self.assertIn("TP1", svg)


class TestRenderReport(unittest.TestCase):
    def test_full_report(self):
        html = render_report(_plan(), {"symbol": "BTC-USDT"},
                             _candles(), symbol="BTC-USDT", mode="intraday")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("BTC-USDT", html)
        self.assertIn("做多", html)
        self.assertIn("<svg", html)          # 内联 K 线
        self.assertIn("日线趋势", html)        # 因子
        self.assertIn("波动率偏高", html)      # 风险提示
        self.assertIn("不构成投资建议", html)

    def test_wait_direction(self):
        p = _plan("wait")
        html = render_report(p, {}, _candles(), symbol="BTC-USDT", mode="intraday")
        self.assertIn("观望", html)

    def test_win_stats_block(self):
        html = render_report(_plan(), {}, _candles(), symbol="BTC-USDT",
                             mode="swing",
                             win_stats={"resolved": 10, "win_rate": 0.6, "avg_r": 0.4})
        self.assertIn("策略胜率", html)
        self.assertIn("60.0%", html)

    def test_escapes_html(self):
        p = _plan()
        p["entry_note"] = "<script>x</script>"
        html = render_report(p, {}, _candles(), symbol="BTC-USDT", mode="intraday")
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestDigits(unittest.TestCase):
    def test_digits(self):
        self.assertEqual(digits(30000), 2)   # clamp 到至少 2
        self.assertEqual(digits(100), 3)
        self.assertEqual(digits(1.5), 5)
        self.assertEqual(digits(0), 2)


if __name__ == "__main__":
    unittest.main()
