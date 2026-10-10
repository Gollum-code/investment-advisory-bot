"""编排层：把 行情抓取 -> 指标计算 -> 信号打分 -> 交易计划 串起来，并做结果缓存。"""

from __future__ import annotations

import threading
import time
from typing import Any

from .market import MarketClient, Snapshot
from .signals import MODES, Plan, build_plan
from .symbols import to_contract

# 影响交易计划的账户字段。计划缓存键只拼这些：用户改过权益/杠杆/风险后，
# 同一合约同一模式就不能再复用旧结果，否则会拿到"按另一个账户算的张数"。
_ACCOUNT_KEY_FIELDS = ("equity_usdt", "preferred_leverage", "max_leverage",
                       "risk_pct_per_trade", "max_margin_pct")


class Advisor:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        a = cfg.get("analysis") or {}
        self.periods: list[str] = a.get("tf_periods") or ["1min", "5min", "15min", "60min", "1day"]
        self.kline_size = int(a.get("kline_size") or 300)
        self.client = MarketClient(
            cfg.get("rest_bases") or [],
            timeout=float(cfg.get("http_timeout_sec") or 12.0),
            retries=int(cfg.get("http_retries") or 2),
            verify=bool(cfg.get("tls_verify", True)),
        )
        # key -> (写入时间, 该计划基于的快照 fetched_at, Plan)
        self._plan_cache: dict[str, tuple[float, float, Plan]] = {}
        self._snap_cache: dict[str, tuple[float, Snapshot]] = {}
        self._lock = threading.Lock()
        self._ttl = float(a.get("cache_ttl_sec") or 20.0)
        # 行情快照同样要有保鲜期。没有它，常驻服务（尤其是定时任务）会一直拿
        # 进程启动时那一份行情反复分析，评分永远不变。
        self._snap_ttl = float(a.get("snapshot_ttl_sec") or self._ttl)
        # 计划键会随账户参数变多（网页上每拖一次滑块就是一个新键），
        # 给缓存一个上限，防止长跑服务里无限膨胀。
        self._plan_cache_max = int(a.get("plan_cache_max") or 128)
        self._snap_cache_max = 256

    # ------------------------------------------------------------------
    @staticmethod
    def _plan_key(symbol: str, mode: str, account: dict | None) -> str:
        base = f"{symbol}:{mode}"
        if not account:
            return base
        part = "&".join(f"{k}={account[k]}" for k in _ACCOUNT_KEY_FIELDS
                        if account.get(k) is not None)
        return f"{base}:{part}" if part else base

    def _trim_locked(self, cache: dict, limit: int) -> None:
        """超出上限时淘汰最老的一批条目（按写入时间）。"""
        if len(cache) <= limit:
            return
        for key in sorted(cache, key=lambda k: cache[k][0])[:len(cache) - limit]:
            del cache[key]

    def analyze(self, symbol: str, *, mode: str = "intraday", force: bool = False,
                account: dict | None = None, snap: Snapshot | None = None) -> Plan:
        sym = to_contract(symbol)
        mode = mode if mode in MODES else "intraday"
        key = self._plan_key(sym, mode, account)
        now = time.time()
        if not force:
            with self._lock:
                hit = self._plan_cache.get(key)
                if hit and now - hit[0] < self._ttl:
                    # 调用方显式给了快照时，计划必须和这份快照出自同一时刻，
                    # 否则前端会看到"K线已刷新、计划还是上一份行情"的错位。
                    if snap is None or hit[1] == snap.fetched_at:
                        return hit[2]

        if snap is None:
            snap = self.fetch(sym, force=force)
        cfg = self.cfg
        if account:
            cfg = {**cfg, "account": {**(cfg.get("account") or {}), **account}}
        plan = build_plan(snap, cfg, mode=mode)
        with self._lock:
            self._plan_cache[key] = (now, snap.fetched_at, plan)
            self._trim_locked(self._plan_cache, self._plan_cache_max)
        return plan

    def fetch(self, symbol: str, *, force: bool = False) -> Snapshot:
        sym = to_contract(symbol)
        now = time.time()
        if not force:
            with self._lock:
                hit = self._snap_cache.get(sym)
                if hit and now - hit[0] < self._snap_ttl:
                    return hit[1]
        snap = self.client.snapshot(sym, periods=self.periods, kline_size=self.kline_size)
        with self._lock:
            self._snap_cache[sym] = (now, snap)
            self._trim_locked(self._snap_cache, self._snap_cache_max)
        return snap

    def invalidate(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol:
                sym = to_contract(symbol)
                self._snap_cache.pop(sym, None)
                for k in [k for k in self._plan_cache if k.startswith(sym + ":")]:
                    self._plan_cache.pop(k, None)
            else:
                self._snap_cache.clear()
                self._plan_cache.clear()

    # ------------------------------------------------------------------
    def plan_payload(self, symbol: str, *, mode: str = "intraday", force: bool = False,
                     account: dict | None = None) -> dict:
        return self.analyze(symbol, mode=mode, force=force, account=account).to_dict()

    def full_payload(self, symbol: str, *, mode: str = "intraday", force: bool = False,
                     account: dict | None = None) -> dict:
        """前端一次拿全：交易计划 + 行情快照 + K线。

        先取快照、再用同一份快照算计划，保证计划与 K 线严格同源，
        不会出现"快照已刷新、计划还是上一份行情"的错位。
        """
        snap = self.fetch(symbol, force=force)
        plan = self.analyze(symbol, mode=mode, force=force, account=account, snap=snap)
        return {
            "plan": plan.to_dict(),
            "snapshot": {
                **snap.summary(),
                "klines": {p: [c.to_dict() for c in rows[-200:]]
                           for p, rows in snap.klines.items()},
                "depth": {"asks": snap.depth.get("asks", [])[:40],
                          "bids": snap.depth.get("bids", [])[:40]},
            },
        }

    def cached_plans(self) -> list[dict]:
        with self._lock:
            rows = sorted(self._plan_cache.values(), key=lambda t: -t[0])
        return [p.to_dict() for _, _, p in rows]

    def health(self) -> list[dict]:
        return self.client.health()
