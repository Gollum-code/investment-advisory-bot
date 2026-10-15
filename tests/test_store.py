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

    def test_dedupe_skips_repeated_signal(self):
        """同一个方向反复落库 -> 只存第一条，直到冷却期过或评分大变。"""
        plan = self._plan()
        dedupe = {"cooldown_sec": 3600, "score_delta": 5.0}
        self.assertIsNotNone(self.store.save(plan, dedupe=dedupe))
        # 一字不差再存一次 -> 跳过
        self.assertIsNone(self.store.save(plan, dedupe=dedupe))
        # 方向变了 -> 存
        flipped = dict(plan)
        flipped["direction"] = "short" if plan["direction"] == "long" else "long"
        self.assertIsNotNone(self.store.save(flipped, dedupe=dedupe))
        # 评分大变 -> 存
        moved = dict(plan)
        moved["score"] = (plan.get("score") or 0) + 50.0
        self.assertIsNotNone(self.store.save(moved, dedupe=dedupe))

    def test_no_dedupe_by_default(self):
        plan = self._plan()
        self.assertIsNotNone(self.store.save(plan))
        self.assertIsNotNone(self.store.save(plan))  # 默认不去重，保持老行为

    def test_pending_and_set_outcome(self):
        plan = self._plan()
        rid = self.store.save(plan)
        pending = self.store.pending(before=time.time() + 1, limit=10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][0], rid)
        self.store.set_outcome(rid, "tp1", ts=plan["generated_at"] + 60,
                               price=104.0, rr=2.0, mfe=2.1, mae=-0.5)
        self.assertEqual(self.store.pending(before=time.time() + 1, limit=10), [])
        self.assertEqual(self.store.outcome_counts().get("tp1"), 1)

    def test_query_with_meta_includes_outcome(self):
        plan = self._plan()
        rid = self.store.save(plan)
        self.store.set_outcome(rid, "tp1", ts=plan["generated_at"] + 60, rr=2.0)
        rows = self.store.query(limit=10, with_meta=True)
        self.assertEqual(rows[0]["outcome"], "tp1")
        self.assertEqual(rows[0]["outcome_rr"], 2.0)
        # 不带 with_meta 时保持旧行为（payload 纯净）
        plain = self.store.query(limit=10)
        self.assertNotIn("outcome", plain[0])

    def test_purge_old_uses_keep_days(self):
        plan = self._plan()
        self.store.save(plan)
        with self.store._lock:
            self.store._conn.execute("UPDATE signals SET ts = ?",
                                     (time.time() - 365 * 86400,))
            self.store._conn.commit()
        self.assertEqual(self.store.purge_old(), 1)

    def test_backup_creates_consistent_snapshot(self):
        """备份必须是一份能独立打开、内容一致的库（VACUUM INTO）。"""
        self.store.save(self._plan())
        dest = Path(self.tmp.name) / "snap.db"
        path = self.store.backup_to(str(dest))
        self.assertTrue(Path(path).exists())
        # 用一个独立的 store 打开备份，确认内容在
        snap_store = SignalStore(path, keep_days=30)
        try:
            self.assertEqual(snap_store._count(), self.store._count())
        finally:
            snap_store.close()

    def test_backup_overwrites_existing_file(self):
        self.store.save(self._plan())
        dest = Path(self.tmp.name) / "snap2.db"
        self.store.backup_to(str(dest))
        # 再次备份到同一路径不应报错（内部先删旧文件）
        self.store.backup_to(str(dest))
        self.assertTrue(dest.exists())

    def test_stats_overview(self):
        self.store.save(self._plan())
        ov = self.store.stats_overview()
        self.assertEqual(ov["signals"], 1)
        self.assertEqual(ov["positions"], 0)

    def test_win_stats_aggregates(self):
        good = self._plan()
        good["score"] = 70.0
        bad = self._plan(RANGE)
        bad["direction"] = "short"
        bad["score"] = -30.0
        r1 = self.store.save(good)
        r2 = self.store.save(bad)
        self.store.set_outcome(r1, "tp1", rr=2.0, mfe=2.0, mae=-0.5)
        self.store.set_outcome(r2, "stopped", rr=-1.0, mfe=0.5, mae=-1.5)
        st = self.store.win_stats()
        self.assertEqual(st["resolved"], 2)
        self.assertEqual(st["wins"], 1)
        self.assertEqual(st["losses"], 1)
        self.assertAlmostEqual(st["win_rate"], 0.5)
        self.assertIn("intraday", st["by_mode"])
        self.assertIn("TEST-USDT", st["by_symbol"])
        self.assertIn("60-80", st["by_score"])   # score=70 落在 60-80 档
        self.assertIn("<40", st["by_score"])      # score=-30 落在 <40 档

    def test_win_stats_groups_by_profile(self):
        good = self._plan()
        good["profile"] = "进取"
        bad = self._plan(RANGE)
        bad["direction"] = "short"
        bad["profile"] = "稳健"
        r1 = self.store.save(good)
        r2 = self.store.save(bad)
        self.store.set_outcome(r1, "tp1", rr=2.0)
        self.store.set_outcome(r2, "stopped", rr=-1.0)
        st = self.store.win_stats()
        self.assertIn("进取", st["by_profile"])
        self.assertIn("稳健", st["by_profile"])
        self.assertEqual(st["by_profile"]["进取"]["n"], 1)
        # 没有 profile 字段的老数据归到"默认"
        r3 = self.store.save({**self._plan(), "symbol": "XRP-USDT"})
        self.store.set_outcome(r3, "timeout")
        st = self.store.win_stats()
        self.assertIn("默认", st["by_profile"])


    def test_win_stats_excludes_timeout_from_grouped_win_rate(self):
        """timeout 不是亏损：分组胜率必须和总胜率用同一套口径。

        曾经 by_mode/by_score 直接拿含 timeout 的 rows 去算，而 _agg 用
        l = n - w，把 timeout 当成了亏损，于是分组胜率比总胜率低一大截，
        页面上会同时出现两个互相矛盾的胜率。
        """
        tp = self.store.save({**self._plan(), "symbol": "AAA-USDT"})
        stopped = self.store.save({**self._plan(RANGE), "symbol": "AAA-USDT"})
        timeout = self.store.save({**self._plan(), "symbol": "AAA-USDT"})
        self.store.set_outcome(tp, "tp1", rr=2.0)
        self.store.set_outcome(stopped, "stopped", rr=-1.0)
        self.store.set_outcome(timeout, "timeout")
        st = self.store.win_stats()
        self.assertEqual(st["resolved"], 2)
        self.assertAlmostEqual(st["win_rate"], 0.5)
        g = st["by_symbol"]["AAA-USDT"]
        self.assertEqual(g["n"], 2)          # timeout 不进分母
        self.assertEqual(g["wins"], 1)
        self.assertEqual(g["losses"], 1)     # 也不会被算成亏损
        self.assertAlmostEqual(g["win_rate"], st["win_rate"])

    def test_win_stats_group_with_only_timeouts_still_listed(self):
        """只有 timeout 的分组仍要出现在结果里（只是计数为 0），
        否则用户会以为那个币/方案从没出过信号。"""
        only = self.store.save({**self._plan(), "symbol": "ZZZ-USDT"})
        self.store.set_outcome(only, "timeout")
        st = self.store.win_stats()
        self.assertIn("ZZZ-USDT", st["by_symbol"])
        self.assertEqual(st["by_symbol"]["ZZZ-USDT"]["n"], 0)
        self.assertIsNone(st["by_symbol"]["ZZZ-USDT"]["win_rate"])

    def test_open_count_not_capped_by_positions_page_limit(self):
        """持仓数不能用 positions() 的默认分页（200）去数，否则超了就报少。"""
        n = 5
        for i in range(n):
            self.store.open_position(symbol=f"S{i}-USDT", direction="long",
                                     entry_price=100.0, size_coin=1.0,
                                     leverage=5, stop=95.0)
        self.assertEqual(self.store._count_where(status="open"), n)
        self.assertEqual(len(self.store.positions(status="open")), n)
        self.assertEqual(self.store.pnl_summary()["open_count"], n)

    def test_backup_refuses_same_path_as_source(self):
        """备份目标与源库同路径时必须拒绝，不能先把源库 unlink 掉。"""
        same = str(self.store.path)
        with self.assertRaises(ValueError):
            self.store.backup_to(same)
        self.assertTrue(self.store.path.exists(), "源库被删了")
        # 拒绝之后仍然能正常读写
        self.assertIsNotNone(self.store.save(self._plan()))


class TestLegacyDbMigration(unittest.TestCase):
    """老库（缺 outcome 等列）升级时，必须先补列再建索引，否则启动直接崩。"""

    def test_old_schema_is_upgraded_in_place(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "legacy.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE signals (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
              symbol TEXT NOT NULL, mode TEXT NOT NULL, direction TEXT NOT NULL,
              score REAL, confidence TEXT, price REAL, entry_low REAL,
              entry_high REAL, stop REAL, stop_pct REAL, rr REAL,
              payload TEXT NOT NULL);
            """)
        conn.execute("INSERT INTO signals (ts, symbol, mode, direction, payload)"
                     " VALUES (1, 'BTC-USDT', 'intraday', 'long', '{}')")
        conn.commit()
        conn.close()

        store = SignalStore(str(db))          # 不该再抛 no such column: outcome
        self.addCleanup(store.close)
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(signals)")}
        self.assertIn("outcome", cols)
        self.assertIn("checked_at", cols)
        self.assertEqual(
            store._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        names = {r[0] for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_signals_pending", names)


if __name__ == "__main__":
    unittest.main()
