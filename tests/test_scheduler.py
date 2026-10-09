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
        client = FakeClient()
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


if __name__ == "__main__":
    unittest.main()
