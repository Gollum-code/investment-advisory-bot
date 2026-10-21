"""定时任务的单元测试：去重落库 + 结果回填（不联网）。"""

import copy
import tempfile
import time
import unittest
from pathlib import Path

from iabot.advisor import Advisor
from iabot.config import DEFAULTS
from iabot.market import Candle
from iabot.scheduler import Scheduler
from iabot.store import SignalStore

try:
    from .test_signals import UP, make_snapshot
except ImportError:
    from test_signals import UP, make_snapshot

BASE_TS = 1_700_000_000


class FakeClient:
    """假行情源：snapshot 出可分析的行情，kline 出可控的后续 K 线。"""

    def __init__(self, candles=None):
        self.calls = 0
        self.candles = candles or []

    def snapshot(self, symbol, *, periods, kline_size):
        self.calls += 1
        return make_snapshot(**UP)

    def kline(self, symbol, period, size=300):
        return self.candles

    def health(self):
        return []


def _candle(ts, o, h, l, c):
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=1000.0)


class SchedulerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SignalStore(str(Path(self.tmp.name) / "signals.db"), keep_days=30)
        cfg = copy.deepcopy(DEFAULTS)
        cfg["schedule"].update({"enabled": False, "symbols": ["BTC-USDT"],
                                "interval_sec": 300, "mode": "intraday"})
        self.cfg = cfg

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _scheduler(self, client=None, **sched):
        cfg = copy.deepcopy(self.cfg)
        cfg["schedule"].update(sched)
        adv = Advisor(cfg)
        adv.client = client or FakeClient()
        return Scheduler(adv, self.store, cfg)


class TestDedupeOnRun(SchedulerTestBase):
    def test_repeated_run_records_once(self):
        sch = self._scheduler()
        sch.run_now()
        n1 = self.store._count()
        sch.run_now()  # 方向没变、评分没变、冷却期内 -> 不再落库
        self.assertEqual(self.store._count(), n1)

    def test_dedupe_respects_only_on_signal(self):
        # 观望信号 + only_on_signal -> 不落库
        adv = Advisor(self.cfg)
        sch = Scheduler(adv, self.store, self.cfg)
        sch.only_on_signal = True
        # 直接构造一个 wait 计划测 store 层过滤
        wait = {"symbol": "BTC-USDT", "mode": "intraday", "direction": "wait",
                "generated_at": time.time()}
        self.assertIsNone(self.store.save(wait, only_on_signal=True))


class TestVerify(SchedulerTestBase):
    def _long_signal_in_store(self, generated_at):
        plan = {"symbol": "BTC-USDT", "mode": "intraday", "direction": "long",
                "generated_at": generated_at, "price": 100.0,
                "entry_low": 100.0, "entry_high": 101.0, "stop": 98.0, "score": 55.0,
                "targets": [{"price": 104.0, "rr": 2.0, "label": "TP1"}]}
        rid = self.store.save(plan)
        return plan, rid

    def test_verify_fills_tp_outcome(self):
        gen = time.time() - 7200  # 两小时前
        plan, rid = self._long_signal_in_store(gen)
        candles = [_candle(int(gen) + 3600, 101, 105, 100, 104)]  # 破 TP1
        sch = self._scheduler(client=FakeClient(candles),
                              verify={"enabled": True, "min_age_sec": 600})
        sch._verify_pending()
        counts = self.store.outcome_counts()
        self.assertEqual(counts.get("tp1"), 1)

    def test_verify_skips_young_signals(self):
        gen = time.time()  # 刚生成
        self._long_signal_in_store(gen)
        sch = self._scheduler(client=FakeClient([_candle(int(gen) + 3600, 101, 105, 100, 104)]),
                              verify={"enabled": True, "min_age_sec": 900})
        sch._verify_pending()  # 太新，不该回填
        self.assertEqual(self.store.outcome_counts(), {})

    def test_verify_disabled(self):
        gen = time.time() - 7200
        self._long_signal_in_store(gen)
        sch = self._scheduler(client=FakeClient([_candle(int(gen) + 3600, 101, 105, 100, 104)]),
                              verify={"enabled": False})
        sch._verify_pending()
        self.assertEqual(self.store.outcome_counts(), {})


class TestNearStopWarn(SchedulerTestBase):
    def _open_long_stop95(self, opened_ts):
        pid = self.store.open_position(symbol="BTC-USDT", direction="long",
                                       entry_price=100, size_coin=1, stop=95)
        with self.store._lock:
            self.store._conn.execute("UPDATE positions SET opened_at=?", (opened_ts,))
            self.store._conn.commit()
        return pid

    def test_warns_when_mark_near_stop(self):
        # 现价 95.5，止损 95 -> 距离 0.52% < 1.5% -> 应预警
        pid = self._open_long_stop95(time.time() - 3600)
        sch = self._scheduler(client=FakeClient([_candle(int(time.time()) - 60, 95.5, 96, 95.2, 95.5)]),
                              warn_near_stop={"enabled": True, "pct": 1.5, "cooldown_sec": 1800})
        sch._check_positions()
        # 未被平仓（95.5 > 95），但应进 _warned 去重表
        self.assertEqual(len(self.store.positions(status="open")), 1)
        self.assertIn(pid, sch._warned)

    def test_no_warn_when_far_from_stop(self):
        pid = self._open_long_stop95(time.time() - 3600)
        sch = self._scheduler(client=FakeClient([_candle(int(time.time()) - 60, 97, 97.5, 96.5, 97)]),
                              warn_near_stop={"enabled": True, "pct": 1.5})
        sch._check_positions()
        self.assertNotIn(pid, sch._warned)

    def test_warn_cooldown(self):
        pid = self._open_long_stop95(time.time() - 3600)
        candle = _candle(int(time.time()) - 60, 95.5, 96, 95.2, 95.5)
        sch = self._scheduler(client=FakeClient([candle]),
                              warn_near_stop={"enabled": True, "pct": 1.5, "cooldown_sec": 3600})
        sch._check_positions()
        sch._check_positions()  # 冷却期内不应重复预警 -> _warned 次数仍为 1
        self.assertEqual(len([x for x in sch._warned if x == pid]), 1)

    def test_warn_disabled(self):
        pid = self._open_long_stop95(time.time() - 3600)
        sch = self._scheduler(client=FakeClient([_candle(int(time.time()) - 60, 95.5, 96, 95.2, 95.5)]),
                              warn_near_stop={"enabled": False, "pct": 1.5})
        sch._check_positions()
        self.assertNotIn(pid, sch._warned)


class UniverseFakeClient(FakeClient):
    """在假行情源上再补全市场接口，用于测试定时任务的全市场轮询。"""

    def __init__(self, symbols, candles=None):
        super().__init__(candles)
        self._symbols = symbols

    def contract_list(self):
        return [{"contract_code": s, "contract_size": 0.001, "labels": ["hot"],
                 "contract_status": 1} for s in self._symbols]

    def all_tickers(self):
        return [{"symbol": s, "price": 100.0, "change_pct": 1.0, "turnover": 1e8,
                 "volume": 1.0, "count": 10} for s in self._symbols]


class TestUniverseRoundRobin(SchedulerTestBase):
    """全市场轮询：每轮只深挖一批，游标跨轮推进，转完一圈回头。"""

    def _universe_scheduler(self, symbols, batch):
        client = UniverseFakeClient(symbols)
        return self._scheduler(client=client, universe={
            "enabled": True, "batch_size": batch, "workers": 2,
            "filters": {"min_turnover_usdt": 1000}})

    def test_disabled_uses_explicit_symbols(self):
        # 显式关掉全市场时才回落到写死的列表（默认已改为开启）
        sch = self._scheduler(universe={"enabled": False})
        self.assertEqual(sch._round_symbols(), ["BTC-USDT"])

    def test_defaults_scan_universe(self):
        # 默认就该扫全市场：只盯一个币等于自我减单，而这套策略靠手数。
        sch = self._scheduler()
        self.assertTrue(sch.universe_enabled)
        self.assertLessEqual(sch.universe_cfg.get("max_symbols", 0), 30)

    def test_batches_advance_cursor(self):
        syms = ["A-USDT", "B-USDT", "C-USDT", "D-USDT", "E-USDT"]
        sch = self._universe_scheduler(syms, 2)
        self.assertEqual(sch._round_symbols(), ["A-USDT", "B-USDT"])
        self.assertEqual(sch._round_symbols(), ["C-USDT", "D-USDT"])
        self.assertEqual(sch._round_symbols(), ["E-USDT", "A-USDT"])   # 绕回
        self.assertEqual(sch._universe_total, 5)

    def test_batch_larger_than_universe(self):
        sch = self._universe_scheduler(["A-USDT", "B-USDT"], 10)
        self.assertEqual(sch._round_symbols(), ["A-USDT", "B-USDT"])

    def test_filters_can_empty_the_universe(self):
        sch = self._universe_scheduler(["A-USDT"], 5)
        sch.universe_cfg = {"asset_class": "all", "min_turnover_usdt": 1e12}
        self.assertEqual(sch._round_symbols(), [])
        self.assertIn("为空", sch.last_error)

    def test_run_once_scans_only_the_batch(self):
        syms = [f"C{i}-USDT" for i in range(6)]
        sch = self._universe_scheduler(syms, 2)
        plans = sch.run_now()
        self.assertEqual(len(plans), 2)
        self.assertEqual(sch._universe_total, 6)
        self.assertEqual(sch._universe_cursor, 2)

    def test_status_reports_universe(self):
        sch = self._universe_scheduler(["A-USDT", "B-USDT"], 1)
        sch.run_now()
        st = sch.status()
        self.assertTrue(st["universe"]["enabled"])
        self.assertEqual(st["universe"]["total"], 2)
        self.assertEqual(st["universe"]["cursor"], 1)
        self.assertEqual(st["universe"]["batch_size"], 1)


class TestShadowSchedule(SchedulerTestBase):
    """影子记录挂在定时任务里：复用快照缓存、限频、失败不拖垮主流程。"""

    def _plan(self, direction="long"):
        from iabot.signals import Plan
        return Plan(symbol="BTC-USDT", mode="intraday", direction=direction,
                    price=100.0, generated_at=0.0)

    def test_disabled_writes_nothing(self):
        from unittest import mock
        sch = self._scheduler(shadow={"enabled": False})
        p = str(Path(self.tmp.name) / "shadow.jsonl")
        sch.shadow_path = p
        with mock.patch("iabot.shadow.build_plan",
                        side_effect=lambda s, c, mode: self._plan()):
            info = sch._record_shadow(["BTC-USDT"])
        self.assertFalse(info["enabled"])
        self.assertFalse(Path(p).exists())

    def test_enabled_writes_lines(self):
        from unittest import mock
        sch = self._scheduler(shadow={"enabled": True, "variants": ["base"],
                                      "every_runs": 1})
        p = str(Path(self.tmp.name) / "shadow.jsonl")
        sch.shadow_path = p
        sch.run_count = 1
        with mock.patch("iabot.shadow.build_plan",
                        side_effect=lambda s, c, mode: self._plan()):
            info = sch._record_shadow(["BTC-USDT"])
        self.assertEqual(info["written"], 1)
        self.assertTrue(Path(p).exists())
        self.assertGreater(sch.shadow_written, 0)
        self.assertEqual(sch.shadow_error, "")

    def test_every_runs_throttles(self):
        from unittest import mock
        sch = self._scheduler(shadow={"enabled": True, "variants": ["base"],
                                      "every_runs": 3})
        p = str(Path(self.tmp.name) / "shadow.jsonl")
        sch.shadow_path = p
        with mock.patch("iabot.shadow.build_plan",
                        side_effect=lambda s, c, mode: self._plan()):
            sch.run_count = 1
            self.assertIn("skipped", sch._record_shadow(["BTC-USDT"]))
            sch.run_count = 3
            self.assertEqual(sch._record_shadow(["BTC-USDT"])["written"], 1)

    def test_uses_advisor_snapshot_cache(self):
        # 反空转：影子记录应复用 Advisor.fetch 的缓存，而不是重新抓
        from unittest import mock
        client = FakeClient()
        sch = self._scheduler(client=client,
                              shadow={"enabled": True, "variants": ["base"],
                                      "every_runs": 1})
        sch.shadow_path = str(Path(self.tmp.name) / "shadow.jsonl")
        sch.run_count = 1
        sch.advisor.fetch("BTC-USDT")          # 先填缓存
        calls = client.calls
        with mock.patch("iabot.shadow.build_plan",
                        side_effect=lambda s, c, mode: self._plan()):
            sch._record_shadow(["BTC-USDT"])
        self.assertEqual(client.calls, calls)  # 没有再打接口

    def test_failure_is_contained(self):
        from unittest import mock
        sch = self._scheduler(shadow={"enabled": True, "variants": ["base"],
                                      "every_runs": 1})
        sch.run_count = 1
        with mock.patch("iabot.shadow.record", side_effect=OSError("disk full")):
            info = sch._record_shadow(["BTC-USDT"])
        self.assertIn("error", info)
        self.assertIn("disk full", sch.shadow_error)

    def test_status_exposes_shadow(self):
        sch = self._scheduler(shadow={"enabled": True, "variants": ["base"],
                                      "every_runs": 2})
        st = sch.status()
        self.assertTrue(st["shadow"]["enabled"])
        self.assertEqual(st["shadow"]["variants"], ["base"])
        self.assertEqual(st["shadow"]["every_runs"], 2)


if __name__ == "__main__":
    unittest.main()
