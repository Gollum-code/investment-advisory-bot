"""信号历史存储（SQLite，标准库自带）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import data_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    symbol        TEXT    NOT NULL,
    mode          TEXT    NOT NULL,
    direction     TEXT    NOT NULL,
    score         REAL,
    confidence    TEXT,
    price         REAL,
    entry_low     REAL,
    entry_high    REAL,
    stop          REAL,
    stop_pct      REAL,
    rr            REAL,
    payload       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_sym ON signals(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_dir ON signals(direction, ts DESC);
"""


class SignalStore:
    def __init__(self, db_file: str = "data/signals.db", keep_days: int = 30) -> None:
        self.path = data_path(db_file)
        self.keep_days = keep_days
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------
    def save(self, plan: dict, *, only_on_signal: bool = False) -> int | None:
        if only_on_signal and plan.get("direction") == "wait":
            return None
        payload = json.dumps(plan, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO signals (ts, symbol, mode, direction, score, confidence, price,
                                        entry_low, entry_high, stop, stop_pct, rr, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan.get("generated_at") or time.time(), plan.get("symbol", ""),
                 plan.get("mode", "intraday"), plan.get("direction", "wait"),
                 plan.get("score"), plan.get("confidence"), plan.get("price"),
                 plan.get("entry_low"), plan.get("entry_high"), plan.get("stop"),
                 plan.get("stop_pct"), plan.get("rr"), payload),
            )
            self._conn.commit()
            return cur.lastrowid

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        try:
            return json.loads(row["payload"])
        except Exception:
            return {"symbol": row["symbol"], "direction": row["direction"],
                    "score": row["score"], "generated_at": row["ts"]}

    def query(self, *, symbol: str | None = None, mode: str | None = None,
              direction: str | None = None, limit: int = 100,
              since: float | None = None) -> list[dict]:
        sql = "SELECT payload FROM signals WHERE 1=1"
        args: list[Any] = []
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        if direction:
            sql += " AND direction = ?"
            args.append(direction)
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def latest_per_symbol(self, mode: str | None = None) -> list[dict]:
        mode_sql = "AND mode = ?" if mode else ""
        args: list[Any] = [mode] if mode else []
        sql = f"""
            SELECT payload FROM signals s
            WHERE ts = (SELECT MAX(ts) FROM signals s2
                        WHERE s2.symbol = s.symbol {mode_sql.replace('mode', 's2.mode')})
            {mode_sql}
            ORDER BY ts DESC
        """
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def stats(self, hours: int = 24) -> dict:
        since = time.time() - hours * 3600
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) c FROM signals WHERE ts >= ?", (since,)).fetchone()["c"]
            by_dir = self._conn.execute(
                "SELECT direction, COUNT(*) c FROM signals WHERE ts >= ? GROUP BY direction",
                (since,)).fetchall()
            by_sym = self._conn.execute(
                "SELECT symbol, COUNT(*) c FROM signals WHERE ts >= ? GROUP BY symbol"
                " ORDER BY c DESC LIMIT 10", (since,)).fetchall()
            first = self._conn.execute(
                "SELECT MIN(ts) t FROM signals").fetchone()["t"]
        return {
            "since_hours": hours,
            "count": total,
            "by_direction": {r["direction"]: r["c"] for r in by_dir},
            "by_symbol": {r["symbol"]: r["c"] for r in by_sym},
            "earliest_ts": first,
            "total_rows": self._count(),
        }

    def _count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]

    def purge(self, days: int | None = None) -> int:
        days = days if days is not None else self.keep_days
        cutoff = time.time() - days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM signals WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM signals")
            self._conn.commit()

    def export_jsonl(self, path: str, limit: int = 5000) -> int:
        rows = self.query(limit=limit)
        p = data_path(path)
        with p.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
