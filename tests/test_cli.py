"""CLI 渲染、参数解析与缓存保鲜的单元测试。"""

import copy
import unittest

from iabot import __main__ as cli
from iabot.advisor import Advisor
from iabot.config import DEFAULTS
from iabot.signals import build_plan

try:  # `discover -s tests` 和 `discover -t .` 两种跑法都要能用
    from .test_signals import DOWN_INTRADAY, RANGE, UP, cfg_with, make_snapshot
except ImportError:
    from test_signals import DOWN_INTRADAY, NO_ROOM, RANGE, UP, cfg_with, make_snapshot


class TestHeadline(unittest.TestCase):
    def test_long(self):
        plan = build_plan(make_snapshot(**UP), cfg_with(), mode="intraday")
        self.assertIn("做多", cli._headline(plan.to_dict()))

    def test_short(self):
        plan = build_plan(make_snapshot(**DOWN_INTRADAY), cfg_with(), mode="intraday")
        self.assertIn("做空", cli._headline(plan.to_dict()))

    def test_wait(self):
        plan = build_plan(make_snapshot(**RANGE), cfg_with(), mode="intraday")
        self.assertIn("观望", cli._headline(plan.to_dict()))


class TestRenderPlan(unittest.TestCase):
    def setUp(self):
        self.plan = build_plan(make_snapshot(**UP), cfg_with(), mode="intraday")
        self.text = cli.render_plan(self.plan.to_dict())

    def test_shows_direction_entry_stop(self):
        self.assertIn("做多 LONG", self.text)
        self.assertIn("入场区间", self.text)
        self.assertIn("止损", self.text)

    def test_shows_actionable_sizing(self):
        self.assertIn("张", self.text)
        self.assertIn("保证金", self.text)
        self.assertIn("止损亏损", self.text)

    def test_shows_factor_breakdown(self):
        self.assertIn("因子明细", self.text)
        self.assertIn("日线趋势", self.text)

    def test_brief_hides_factors(self):
        brief = cli.render_plan(self.plan.to_dict(), verbose=False)
        self.assertNotIn("因子明细", brief)

    def test_wait_plan_has_no_sizing_numbers(self):
        wait = cli.render_plan(build_plan(make_snapshot(**RANGE), cfg_with(),
                                          mode="intraday").to_dict())
        self.assertIn("不给", wait)

    def test_explains_why_a_high_score_still_waits(self):
        plan = build_plan(make_snapshot(**NO_ROOM), cfg_with(), mode="intraday")
        text = cli.render_plan(plan.to_dict())
        self.assertIn("其实指向做多", text)
        self.assertIn("盈亏比", text)
        self.assertIn("转为观望", text)

    def test_missing_price_does_not_crash(self):
        snap = make_snapshot(**UP)
        snap.price = None
        text = cli.render_plan(build_plan(snap, cfg_with(), mode="intraday").to_dict())
        self.assertIn("观望", text)


class TestArgParsing(unittest.TestCase):
    def test_analyze_defaults(self):
        args = cli.build_parser().parse_args(["analyze", "BTC-USDT"])
        self.assertEqual(args.mode, "intraday")
        self.assertFalse(args.json)

    def test_swing_mode(self):
        args = cli.build_parser().parse_args(["analyze", "eth", "--mode", "swing"])
        self.assertEqual(args.mode, "swing")

    def test_scan_parses_list(self):
        args = cli.build_parser().parse_args(["scan", "BTC-USDT,ETH-USDT"])
        self.assertEqual(args.symbols, "BTC-USDT,ETH-USDT")

    def test_bad_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["analyze", "BTC-USDT", "--mode", "scalp"])

    def test_bare_symbol_is_treated_as_analyze(self):
        # main() 会把裸合约代码改写成 analyze 子命令
        argv = ["btc"]
        if argv[0] not in cli.COMMANDS and not argv[0].startswith("-"):
            argv = ["analyze"] + argv
        args = cli.build_parser().parse_args(argv)
        self.assertEqual(args.cmd, "analyze")
        self.assertEqual(args.symbol, "btc")


class FakeClient:
    """假行情源：每次调用都返回一份新的快照，用来验证缓存行为。"""

    def __init__(self, scenario):
        self.scenario = scenario
        self.calls = 0

    def snapshot(self, symbol, *, periods, kline_size):
        self.calls += 1
        snap = make_snapshot(**self.scenario)
        snap.fetched_at = float(self.calls)
        return snap

    def health(self):
        return [{"base": "fake", "ok": True, "ms": 1, "error": "", "current": True}]


class TestAdvisorCache(unittest.TestCase):
    def _advisor(self, **analysis):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["analysis"].update(analysis)
        adv = Advisor(cfg)
        adv.client = FakeClient(UP)
        return adv

    def test_second_fetch_within_ttl_uses_cache(self):
        adv = self._advisor(cache_ttl_sec=60, snapshot_ttl_sec=60)
        adv.fetch("BTC-USDT")
        adv.fetch("BTC-USDT")
        self.assertEqual(adv.client.calls, 1)

    def test_forced_fetch_bypasses_cache(self):
        adv = self._advisor(cache_ttl_sec=60, snapshot_ttl_sec=60)
        adv.fetch("BTC-USDT")
        adv.fetch("BTC-USDT", force=True)
        self.assertEqual(adv.client.calls, 2)

    def test_stale_snapshot_is_refreshed(self):
        """快照过期后必须重新拉，否则定时任务会一直分析同一份行情。"""
        adv = self._advisor(cache_ttl_sec=0.001, snapshot_ttl_sec=0.001)
        first = adv.fetch("BTC-USDT")
        import time
        time.sleep(0.02)
        second = adv.fetch("BTC-USDT")
        self.assertEqual(adv.client.calls, 2)
        self.assertNotEqual(first.fetched_at, second.fetched_at)

    def test_force_analyze_refetches_market_data(self):
        adv = self._advisor(cache_ttl_sec=60, snapshot_ttl_sec=60)
        adv.analyze("BTC-USDT", force=True)
        adv.analyze("BTC-USDT", force=True)
        self.assertEqual(adv.client.calls, 2)

    def test_plan_cache_returns_same_object_without_force(self):
        adv = self._advisor(cache_ttl_sec=60, snapshot_ttl_sec=60)
        a = adv.analyze("BTC-USDT")
        b = adv.analyze("BTC-USDT")
        self.assertIs(a, b)
        self.assertEqual(adv.client.calls, 1)

    def test_invalidate_clears_caches(self):
        adv = self._advisor(cache_ttl_sec=60, snapshot_ttl_sec=60)
        adv.analyze("BTC-USDT")
        adv.invalidate("BTC-USDT")
        adv.analyze("BTC-USDT")
        self.assertEqual(adv.client.calls, 2)


if __name__ == "__main__":
    unittest.main()