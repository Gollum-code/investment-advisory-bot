"""信号历史存储（SQLite，标准库自带）。

除了落库/查询，这里还负责两件事：

1. 去重：定时任务每轮都分析，同一个信号会连续几十分钟反复出现。
   不去重的话 30 天能堆出十万条几乎重复的记录，后面算胜率会被同一个状态刷爆。
2. 结果回填：保存时并不知道后来是涨是跌。这里记下信号的方向/止损/目标位，
   由 outcomes 模块拿后续 K 线判断"先碰止损还是先到目标"，写回结果，
   胜率统计才有意义。
"""

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
    payload       TEXT    NOT NULL,
    outcome       TEXT,            -- pending 结果：tp1/tp2/tp3/stopped/timeout/空=未回填
    outcome_ts    REAL,
    outcome_price REAL,
    outcome_rr    REAL,            -- 命中那档目标的盈亏比；止损=-1
    mfe_pct       REAL,            -- 最大有利偏移（相对入场中值 %）
    mae_pct       REAL,            -- 最大不利偏移（相对入场中值 %）
    checked_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_sym ON signals(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_dir ON signals(direction, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_pending ON signals(outcome, ts) WHERE outcome IS NULL;

-- 模拟盘持仓：手动录入、程序只负责跟踪浮动盈亏与止损/止盈（不下单）
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at     REAL    NOT NULL,
    closed_at     REAL,
    symbol        TEXT    NOT NULL,
    direction     TEXT    NOT NULL,        -- long | short
    entry_price   REAL    NOT NULL,
    size_coin     REAL    NOT NULL,        -- 仓位大小（币）
    leverage      REAL    NOT NULL DEFAULT 1,
    stop          REAL,
    tp1           REAL,
    tp2           REAL,
    tp3           REAL,
    status        TEXT    NOT NULL DEFAULT 'open',  -- open | closed
    close_price   REAL,
    close_reason  TEXT,                    -- stop | tp1 | tp2 | tp3 | manual
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol, opened_at DESC);
"""

# 老库（加结果回填之前建的）没有这些列，启动时补齐
_MIGRATE: list[tuple[str, str]] = [
    ("outcome", "TEXT"),
    ("outcome_ts", "REAL"),
    ("outcome_price", "REAL"),
    ("outcome_rr", "REAL"),
    ("mfe_pct", "REAL"),
    ("mae_pct", "REAL"),
    ("checked_at", "REAL"),
]

# 按 |score| 分档，用来统计"评分越高是不是真的越准"
_SCORE_BUCKETS = (
    (0.0, 40.0, "<40"),
    (40.0, 60.0, "40-60"),
    (60.0, 80.0, "60-80"),
    (80.0, 200.0, ">=80"),
)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals)")}
    for name, ddl in _MIGRATE:
        if name not in cols:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {ddl}")


class SignalStore:
    def __init__(self, db_file: str = "data/signals.db", keep_days: int = 30) -> None:
        self.path = data_path(db_file)
        self.keep_days = keep_days
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            _ensure_columns(self._conn)
            self._conn.commit()

    # ------------------------------------------------------------------
    def save(self, plan: dict, *, only_on_signal: bool = False,
             dedupe: dict | None = None) -> int | None:
        """保存一条信号。

        dedupe: {"cooldown_sec": 3600, "score_delta": 5.0}
        同一 symbol+mode 的上一条若方向相同、|评分差| < score_delta、
        且距离上次落库不到 cooldown_sec，就跳过，避免刷屏。
        """
        if only_on_signal and plan.get("direction") == "wait":
            return None
        if dedupe and not self._should_record(plan, dedupe):
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

    def _should_record(self, plan: dict, dedupe: dict) -> bool:
        last = self.last_row(plan.get("symbol", ""), plan.get("mode", "intraday"))
        if last is None:
            return True
        if last.get("direction") != plan.get("direction"):
            return True
        score_now = plan.get("score") or 0
        score_last = last.get("score") or 0
        if abs(score_now - score_last) >= float(dedupe.get("score_delta") or 5.0):
            return True
        cooldown = float(dedupe.get("cooldown_sec") or 3600.0)
        return (time.time() - (last.get("ts") or 0)) >= cooldown

    def last_row(self, symbol: str, mode: str = "intraday") -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM signals WHERE symbol = ? AND mode = ?"
                " ORDER BY ts DESC LIMIT 1", (symbol, mode)).fetchone()
        if row is None:
            return None
        d = {k: row[k] for k in row.keys()}
        return d

    # ------------------------------------------------------------------
    def pending(self, *, before: float, limit: int = 100) -> list[tuple[int, dict]]:
        """还没回填结果的信号 [(id, payload)]。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload FROM signals"
                " WHERE outcome IS NULL AND direction != 'wait' AND ts <= ?"
                " ORDER BY ts LIMIT ?", (before, limit)).fetchall()
        return [(r["id"], json.loads(r["payload"])) for r in rows]

    def set_outcome(self, row_id: int, outcome: str, *, ts: float | None = None,
                    price: float | None = None, rr: float | None = None,
                    mfe: float | None = None, mae: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE signals SET outcome = ?, outcome_ts = ?, outcome_price = ?,"
                " outcome_rr = ?, mfe_pct = ?, mae_pct = ?, checked_at = ?"
                " WHERE id = ?",
                (outcome, ts, price, rr, mfe, mae, time.time(), row_id))
            self._conn.commit()

    def outcome_counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT outcome, COUNT(*) c FROM signals"
                " WHERE outcome IS NOT NULL AND outcome != '' GROUP BY outcome").fetchall()
        return {r["outcome"]: r["c"] for r in rows}

    # ------------------------------------------------------------------
    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        try:
            return json.loads(row["payload"])
        except Exception:
            return {"symbol": row["symbol"], "direction": row["direction"],
                    "score": row["score"], "generated_at": row["ts"]}

    def query(self, *, symbol: str | None = None, mode: str | None = None,
              direction: str | None = None, limit: int = 100,
              since: float | None = None, with_meta: bool = False) -> list[dict]:
        sql = ("SELECT payload, outcome, outcome_ts, outcome_price, outcome_rr,"
               " mfe_pct, mae_pct FROM signals WHERE 1=1")
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
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            if with_meta:
                p["outcome"] = r["outcome"]
                p["outcome_ts"] = r["outcome_ts"]
                p["outcome_rr"] = r["outcome_rr"]
                p["mfe_pct"] = r["mfe_pct"]
                p["mae_pct"] = r["mae_pct"]
            out.append(p)
        return out

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

    # ------------------------------------------------------------------
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

    def win_stats(self, *, days: int | None = None, limit: int = 20000) -> dict:
        """已回填信号的胜率统计：总体 + 按模式/币种/评分档/置信度。"""
        sql = ("SELECT symbol, mode, direction, score, confidence,"
               " outcome, outcome_rr, mfe_pct, mae_pct, ts FROM signals"
               " WHERE outcome IS NOT NULL AND outcome != ''")
        args: list[Any] = []
        if days:
            sql += " AND ts >= ?"
            args.append(time.time() - days * 86400)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()

        resolved = [r for r in rows if r["outcome"] != "timeout"]
        wins = [r for r in resolved if (r["outcome"] or "").startswith("tp")]
        losses = [r for r in resolved if r["outcome"] == "stopped"]
        timeouts = [r for r in rows if r["outcome"] == "timeout"]

        def _agg(rs) -> dict:
            n = len(rs)
            if not n:
                return {"n": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_r": None,
                        "avg_mfe_pct": None, "avg_mae_pct": None}
            w = sum(1 for r in rs if (r["outcome"] or "").startswith("tp"))
            l = n - w
            rrs = [r["outcome_rr"] for r in rs if r["outcome_rr"] is not None]
            mfes = [r["mfe_pct"] for r in rs if r["mfe_pct"] is not None]
            maes = [r["mae_pct"] for r in rs if r["mae_pct"] is not None]
            return {"n": n, "wins": w, "losses": l,
                    "win_rate": round(w / n, 4) if n else None,
                    "avg_r": round(sum(rrs) / len(rrs), 3) if rrs else None,
                    "avg_mfe_pct": round(sum(mfes) / len(mfes), 3) if mfes else None,
                    "avg_mae_pct": round(sum(maes) / len(maes), 3) if maes else None}

        def _group(key):
            out = {}
            for r in rows:
                out.setdefault(key(r), []).append(r)
            return {k: _agg(v) for k, v in sorted(out.items())}

        by_score = {}
        for r in rows:
            s = abs(r["score"] or 0)
            b = next((label for lo, hi, label in _SCORE_BUCKETS if lo <= s < hi), ">=80")
            by_score.setdefault(b, []).append(r)

        return {
            "days": days,
            "resolved": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "timeouts": len(timeouts),
            **_agg(resolved),
            "by_mode": _group(lambda r: r["mode"]),
            "by_symbol": _group(lambda r: r["symbol"]),
            "by_confidence": _group(lambda r: r["confidence"] or "低"),
            "by_score": {k: _agg(v) for k, v in sorted(by_score.items())},
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

    def purge_old(self) -> int:
        """按配置的 keep_days 清理过期记录（启动与定时任务每轮调用）。"""
        return self.purge(self.keep_days)

    # ------------------------------------------------------------------
    # 模拟盘持仓（positions 表）
    # ------------------------------------------------------------------
    def open_position(self, *, symbol: str, direction: str, entry_price: float,
                      size_coin: float, leverage: float = 1.0,
                      stop: float | None = None, tp1: float | None = None,
                      tp2: float | None = None, tp3: float | None = None,
                      note: str = "", opened_at: float | None = None) -> int:
        direction = direction if direction in ("long", "short") else "long"
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO positions (opened_at, symbol, direction, entry_price,
                       size_coin, leverage, stop, tp1, tp2, tp3, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'open',?)""",
                (opened_at or time.time(), symbol, direction, entry_price,
                 size_coin, leverage, stop, tp1, tp2, tp3, note))
            self._conn.commit()
            return int(cur.lastrowid)

    def close_position(self, pos_id: int, *, price: float, reason: str = "manual",
                       closed_at: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE positions SET status='closed', close_price=?, close_reason=?,"
                " closed_at=? WHERE id=? AND status='open'",
                (price, reason, closed_at or time.time(), pos_id))
            self._conn.commit()

    def positions(self, *, status: str | None = None, symbol: str | None = None,
                  limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM positions WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        sql += " ORDER BY opened_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def position(self, pos_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
        return {k: r[k] for k in r.keys()} if r else None

    def delete_position(self, pos_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM positions WHERE id=?", (pos_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def pnl_summary(self) -> dict:
        """模拟盘利润簿：已平仓的已实现盈亏汇总 + 权益曲线。"""
        from .paper import realized_pnl_of
        closed = self.positions(status="closed", limit=5000)
        now = time.time()
        week_ago = now - 7 * 86400
        month_ago = now - 30 * 86400

        curve: list[dict] = []          # [{ts, cum}] 权益曲线（净已实现累加）
        cum = 0.0
        total_net = week_net = month_net = 0.0
        wins = losses = 0
        win_nets: list[float] = []
        loss_nets: list[float] = []
        rows = sorted(closed, key=lambda p: (p.get("closed_at") or 0))
        for p in rows:
            r = realized_pnl_of(p)
            net = r["net_pnl_usdt"]
            total_net += net
            ct = p.get("closed_at") or 0
            if ct >= week_ago:
                week_net += net
            if ct >= month_ago:
                month_net += net
            if net > 0:
                wins += 1
                win_nets.append(net)
            else:
                losses += 1
                loss_nets.append(net)
            cum += net
            curve.append({"ts": ct, "cum": round(cum, 2)})

        total_trades = wins + losses
        return {
            "closed_count": len(closed),
            "open_count": len(self.positions(status="open")),
            "total_net_usdt": round(total_net, 2),
            "week_net_usdt": round(week_net, 2),
            "month_net_usdt": round(month_net, 2),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total_trades, 3) if total_trades else 0.0,
            "avg_win_usdt": round(sum(win_nets) / len(win_nets), 2) if win_nets else 0.0,
            "avg_loss_usdt": round(sum(loss_nets) / len(loss_nets), 2) if loss_nets else 0.0,
            "equity_curve": curve,
        }

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM signals")
            self._conn.commit()

    def dump_jsonl(self, *, symbol: str | None = None, mode: str | None = None,
                   direction: str | None = None, limit: int = 5000) -> str:
        """把信号历史序列化成 JSON Lines 文本（供 API 导出）。"""
        rows = self.query(symbol=symbol, mode=mode, direction=direction, limit=limit)
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)

    def export_jsonl(self, path: str, limit: int = 5000) -> int:
        p = data_path(path)
        text = self.dump_jsonl(limit=limit)
        with p.open("w", encoding="utf-8") as f:
            f.write(text)
        return len(text.splitlines())

    def close(self) -> None:
        with self._lock:
            self._conn.close()
