"""信号历史存储（SQLite）的单元测试。"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from iabot.signals import build_plan
from iabot.store import SignalStore

try:  # `discover -s tests` 和 `discover -t .` 两种跑法都要能用
    from .test_signals import RANGE, UP, cfg_with, make_snapshot
except ImportError:
    from test_signals import RANGE, UP, cfg_with, make_snapshot


class TestSignalStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SignalStore(str(Path(self.tmp.name) / "signals.db"), keep_days=30)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _plan(self, scenario=UP):
        return build_plan(make_snapshot(**scenario), cfg_with(), mode="intraday").to_dict()

    def test_save_and_query_roundtrip(self):
        plan = self._plan()
        self.assertIsNotNone(self.store.save(plan))
        rows = self.store.query(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], plan["symbol"])
        self.assertEqual(rows[0]["direction"], plan["direction"])

    def test_only_on_signal_skips_wait(self):
        plan = self._plan(RANGE)
        self.assertIsNone(self.store.save(plan, only_on_signal=True))

    def test_dump_jsonl_serializes_saved_rows(self):
        plan = self._plan()
        self.store.save(plan)
        text = self.store.dump_jsonl(limit=10)
        lines = [l for l in text.splitlines() if l]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["symbol"], plan["symbol"])

    def test_purge_removes_old_rows(self):
        plan = self._plan()
        self.store.save(plan)
        with self.store._lock:
            self.store._conn.execute("UPDATE signals SET ts = ?",
                                     (time.time() - 365 * 86400,))
            self.store._conn.commit()
        self.assertEqual(self.store.purge(days=30), 1)
        self.assertEqual(self.store._count(), 0)


if __name__ == "__main__":
    unittest.main()
