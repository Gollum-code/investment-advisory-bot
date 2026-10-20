from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from iabot.config import DEFAULTS, _merge, validate_config
from iabot.market import Candle
from iabot.shadow import load, record, report, summarize

from tests.test_backtest import _trading_snapshot


def _fake_plan(direction="long", symbol="X"):
    from iabot.signals import Plan
    return Plan(symbol=symbol, mode="intraday", direction=direction,
                price=100.0, generated_at=0.0)

T0 = 1_700_000_000.0


def _cfg():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["analysis"]["score_threshold"] = 20   # 夹具能出方向
    return validate_config(cfg)


def _c(ts_off, o, h, l, cl):
    return Candle(ts=T0 + ts_off, open=o, high=h, low=l, close=cl, volume=1.0)


class TestRecord(unittest.TestCase):
    def test_writes_one_line_per_variant_and_symbol(self):
        snap = _trading_snapshot()
        with mock.patch("iabot.shadow.build_plan",
                        side_effect=lambda s, c, mode: _fake_plan("long", s.symbol)):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "shadow.jsonl")
                out = record({"A-USDT": snap, "B-USDT": copy.deepcopy(snap)},
                             _cfg(), variants=["base", "trend_veto"],
                             mode="intraday", path=p, now=T0)
                self.assertEqual(out["written"], 4)   # 2 标的 × 2 方案
                rows = load(p)
                self.assertEqual(len(rows), 4)
                self.assertEqual({r["variant"] for r in rows},
                                 {"base", "trend_veto"})
                for r in rows:
                    self.assertIn(r["plan"]["direction"], ("long", "short"))
                    self.assertEqual(r["ts"], T0)

    def test_wait_plans_are_skipped(self):
        snap = _trading_snapshot()
        with mock.patch("iabot.shadow.build_plan",
                        side_effect=lambda s, c, mode: _fake_plan("wait", s.symbol)):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "shadow.jsonl")
                out = record({"A-USDT": snap}, _cfg(), variants=["base"],
                             mode="intraday", path=p, now=T0)
                self.assertEqual(out["written"], 0)
                self.assertEqual(out["skipped_wait"], 1)

    def test_unknown_variant_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jsonl")
            with self.assertRaises(ValueError):
                record({"A-USDT": _trading_snapshot()}, _cfg(),
                       variants=["nope"], path=p)

    def test_load_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load(os.path.join(d, "none.jsonl")), [])

    def test_jsonl_lines_are_valid_json(self):
        with mock.patch("iabot.shadow.build_plan",
                        side_effect=lambda s, c, mode: _fake_plan("short", s.symbol)):
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "s.jsonl")
                record({"A-USDT": _trading_snapshot()}, _cfg(), path=p, now=T0)
                with open(p, encoding="utf-8") as fh:
                    for line in fh:
                        json.loads(line)

    def test_recorded_plan_keeps_variant_config_effect(self):
        # 反空转：不同方案必须用各自的配置生成计划，不能共用一份
        seen = []

        def spy(s, c, mode):
            seen.append(bool(c["analysis"].get("trend_veto")))
            return _fake_plan("long", s.symbol)

        with mock.patch("iabot.shadow.build_plan", side_effect=spy):
            with tempfile.TemporaryDirectory() as d:
                record({"A-USDT": _trading_snapshot()}, _cfg(),
                       variants=["base", "trend_veto"],
                       path=os.path.join(d, "s.jsonl"), now=T0)
        self.assertIn(False, seen)
        self.assertIn(True, seen)


class TestReport(unittest.TestCase):
    def _entry(self, variant, direction="long", entry=100.0, stop=95.0,
               tp=110.0):
        return {
            "ts": T0, "symbol": "A-USDT", "variant": variant, "mode": "swing",
            "plan": {
                "direction": direction,
                "entry_low": entry - 0.5, "entry_high": entry + 0.5,
                "stop": stop, "targets": [{"label": "TP1", "price": tp,
                                           "rr": 2.0}],
                "generated_at": T0, "mode": "swing", "market": {"atr": None},
            },
        }

    def test_tp_and_stop_counted(self):
        entries = [self._entry("base")]
        candles = {"A-USDT": [_c(600, 100, 111, 99.5, 110.5)]}
        out = report(entries, candles)
        v = out["variants"]["base"]
        self.assertEqual(v["decided"], 1)
        self.assertEqual(v["wins"], 1)
        self.assertAlmostEqual(v["expectancy_r"], 2.0, places=4)

    def test_loss_counts_as_minus_one(self):
        entries = [self._entry("base")]
        candles = {"A-USDT": [_c(600, 100, 100.2, 94.0, 94.5)]}
        v = report(entries, candles)["variants"]["base"]
        self.assertEqual(v["wins"], 0)
        self.assertAlmostEqual(v["expectancy_r"], -1.0, places=4)

    def test_missing_candles_are_pending(self):
        entries = [self._entry("base")]
        v = report(entries, {})["variants"]["base"]
        self.assertEqual(v["decided"], 0)
        self.assertEqual(v["pending"], 1)

    def test_no_fill_is_pending_not_loss(self):
        entries = [self._entry("base")]
        # 价格一路涨走，从没进过入场区
        candles = {"A-USDT": [_c(600, 101, 111, 100.6, 110.5)]}
        v = report(entries, candles)["variants"]["base"]
        self.assertEqual(v["pending"], 1)
        self.assertEqual(v["decided"], 0)

    def test_variants_compared_separately(self):
        entries = [self._entry("base"), self._entry("trend_veto")]
        candles = {"A-USDT": [_c(600, 100, 111, 99.5, 110.5)]}
        out = report(entries, candles)["variants"]
        self.assertEqual(set(out), {"base", "trend_veto"})
        self.assertAlmostEqual(out["base"]["expectancy_r"],
                               out["trend_veto"]["expectancy_r"], places=4)

    def test_exit_override_changes_outcome(self):
        # 同一批 K 线：保本让第二笔从 -1R 变成 0R
        entries = [self._entry("be")]
        candles = {"A-USDT": [
            _c(600, 100, 106.0, 99.5, 105.5),
            _c(1200, 105.5, 105.5, 94.0, 94.5),
        ]}
        base = report(entries, candles)["variants"]["be"]
        be = report(entries, candles,
                    exit_by_variant={"be": {"breakeven_at_r": 1.0}}
                    )["variants"]["be"]
        self.assertAlmostEqual(base["expectancy_r"], -1.0, places=4)
        self.assertAlmostEqual(be["expectancy_r"], 0.0, places=4)


class TestSummarize(unittest.TestCase):
    def test_empty(self):
        out = summarize([])
        self.assertEqual(out["variants"], {})

    def test_ci_present_with_enough_samples(self):
        rows = [{"variant": "v", "outcome": "tp1", "r": 1.0}] * 40
        rows += [{"variant": "v", "outcome": "stopped", "r": -1.0}] * 40
        v = summarize(rows)["variants"]["v"]
        self.assertEqual(v["decided"], 80)
        self.assertIsNotNone(v["expectancy_ci"])
        self.assertLess(v["expectancy_ci"][0], v["expectancy_r"])
        self.assertGreater(v["expectancy_ci"][1], v["expectancy_r"])


if __name__ == "__main__":
    unittest.main()
