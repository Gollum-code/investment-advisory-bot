"""结局判定与回测口径的回归测试。

两处都曾让统计数字失真：
1. evaluate_outcome 不检查挂单是否成交 -> 没做上的单被算成胜利，胜率虚高。
2. 回测"数据不足"早退时把 timeouts 填成 K 线根数 -> 幻影超时污染分母。
"""

import unittest

from iabot.backtest import BacktestResult, backtest
from iabot.config import DEFAULTS
from iabot.market import Candle
from iabot.outcomes import evaluate_outcome

import copy

BASE = 1_700_000_000


def _c(ts, o, h, l, close):
    return Candle(ts=ts, open=o, high=h, low=l, close=close, volume=1.0)


def _plan(direction="long", entry_low=100.0, entry_high=101.0, stop=97.0,
          tps=((108.0, 2.0),), ts=BASE):
    return {"direction": direction, "mode": "swing", "generated_at": ts,
            "entry_low": entry_low, "entry_high": entry_high, "stop": stop,
            "targets": [{"price": p, "rr": r, "label": f"TP{i}"}
                        for i, (p, r) in enumerate(tps, 1)]}


class TestFillRequired(unittest.TestCase):
    """多单挂在现价下方，价格直接涨走就不该算成交。"""

    def test_never_touched_entry_is_no_fill(self):
        plan = _plan("long", entry_low=100.0, entry_high=101.0, stop=97.0,
                     tps=((110.0, 2.0),))
        # 价格从 105 一路涨到 112：从没回到 100-101 的挂单区，却涨过了 110
        candles = [_c(BASE + 3600 * (i + 1), 105 + i, 106 + i, 105 + i, 106 + i)
                   for i in range(8)]
        res = evaluate_outcome(plan, candles)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "no_fill")
        self.assertIsNone(res["rr"])

    def test_touched_entry_then_target_is_a_win(self):
        plan = _plan("long", entry_low=100.0, entry_high=101.0, stop=97.0,
                     tps=((110.0, 2.0),))
        candles = [_c(BASE + 3600, 104, 105, 100.5, 101.5),      # 触及入场区
                   _c(BASE + 7200, 101.5, 105, 101, 105),
                   _c(BASE + 10800, 105, 110.5, 105, 110)]       # 到目标
        res = evaluate_outcome(plan, candles)
        self.assertEqual(res["outcome"], "tp1")

    def test_require_fill_false_keeps_old_behaviour(self):
        plan = _plan("long", entry_low=100.0, entry_high=101.0, stop=97.0,
                     tps=((110.0, 2.0),))
        candles = [_c(BASE + 3600 * (i + 1), 105 + i, 106 + i, 105 + i, 106 + i)
                   for i in range(8)]
        res = evaluate_outcome(plan, candles, require_fill=False)
        self.assertEqual(res["outcome"], "tp1")

    def test_short_side_symmetric(self):
        # 空单挂在现价上方，价格直接跌走 -> 未成交
        plan = _plan("short", entry_low=99.0, entry_high=100.0, stop=103.0,
                     tps=((90.0, 2.0),))
        candles = [_c(BASE + 3600 * (i + 1), 95 - i, 95 - i, 93 - i, 93 - i)
                   for i in range(8)]
        self.assertEqual(evaluate_outcome(plan, candles)["outcome"], "no_fill")

    def test_fill_then_stop_in_same_candle_counts_as_loss(self):
        """同一根既触及挂单又打穿止损 -> 按止损算（保守）。"""
        plan = _plan("long", entry_low=100.0, entry_high=101.0, stop=97.0,
                     tps=((110.0, 2.0),))
        candles = [_c(BASE + 3600, 101, 101, 96, 96.5)]
        self.assertEqual(evaluate_outcome(plan, candles)["outcome"], "stopped")

    def test_horizon_without_fill_is_no_fill_not_timeout(self):
        plan = _plan("long", entry_low=100.0, entry_high=101.0, stop=97.0,
                     tps=((101.5, 0.5),))
        plan["mode"] = "intraday"
        # 一直在 102 上方横着，没回挂单区，也没到 101.5
        candles = [_c(BASE + 900 * (i + 1), 102.5, 103, 102, 102.5)
                   for i in range(6)]
        self.assertEqual(evaluate_outcome(plan, candles)["outcome"], "no_fill")


class TestInsufficientDataNotPhantomTimeouts(unittest.TestCase):
    """数据不足时不能报出一堆"超时"——一笔信号都没有。"""

    def test_insufficient_data_flag_and_zero_timeouts(self):
        from iabot.market import Snapshot
        # 主周期只有 20 根，远小于 warmup+step+30
        ks = [_c(BASE + i * 86400, 100, 101, 99, 100) for i in range(20)]
        snap = Snapshot(symbol="X-USDT", price=100.0, klines={"1day": ks},
                        contract_size=0.001)
        cfg = copy.deepcopy(DEFAULTS)
        r = backtest(snap, cfg, mode="swing", step=2, warmup=60)
        self.assertTrue(r.insufficient_data)
        self.assertEqual(r.timeouts, 0)
        self.assertEqual(r.trades, 0)
        self.assertEqual(r.no_fills, 0)
        d = r.to_dict()
        self.assertTrue(d["insufficient_data"])
        self.assertEqual(d["timeouts"], 0)

    def test_win_rate_denominator_is_self_consistent(self):
        """wins/(wins+losses) 必须等于 wins/(trades-timeouts)。"""
        from iabot.market import Snapshot
        import random
        rnd = random.Random(9)
        closes = [100.0]
        for _ in range(400):
            closes.append(closes[-1] * (1 + rnd.uniform(-0.02, 0.02)))
        series = {}
        for period, secs in (("15min", 900), ("60min", 3600), ("1day", 86400)):
            series[period] = [_c(BASE + i * secs, c, c * 1.01, c * 0.99, c)
                              for i, c in enumerate(closes)]
        snap = Snapshot(symbol="X-USDT", price=closes[-1], klines=series,
                        contract_size=0.001, funding_rate=0.0001)
        cfg = copy.deepcopy(DEFAULTS)
        r = backtest(snap, cfg, mode="intraday", step=5, warmup=60)
        self.assertEqual(r.trades, r.wins + r.losses + r.timeouts)
        if r.trades and r.timeouts < r.trades:
            a = r.wins / (r.wins + r.losses) if (r.wins + r.losses) else 0.0
            b = r.wins / (r.trades - r.timeouts)
            self.assertAlmostEqual(a, b, places=9)


class TestBacktestResultShape(unittest.TestCase):
    def test_new_fields_present(self):
        d = BacktestResult(
            symbol="X", mode="swing", bars=1, trades=0, wins=0, losses=0,
            timeouts=0, no_fills=0, total_r=0.0, avg_win_r=0.0, avg_loss_r=0.0,
            win_rate=0.0, expectancy_r=0.0, avg_cost_r=0.0, avg_fee_r=0.0,
            avg_slippage_r=0.0, avg_funding_r=0.0, avg_holding_h=0.0,
            max_mfe=0.0, max_mae=0.0, equity_curve=[], equity_usdt=100.0,
            risk_pct=2.0, risk_amount_usdt=2.0, equity_curve_usdt=[]).to_dict()
        self.assertIn("no_fills", d)
        self.assertIn("insufficient_data", d)


if __name__ == "__main__":
    unittest.main()