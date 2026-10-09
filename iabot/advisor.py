"""编排层：把 行情抓取 -> 指标计算 -> 信号打分 -> 交易计划 串起来，并做结果缓存。"""

from __future__ import annotations

import threading
import time
from typing import Any

from .market import MarketClient, Snapshot
from .signals import MODES, Plan, build_plan
from .symbols import to_contract


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
        self._plan_cache: dict[str, tuple[float, Plan]] = {}
        self._snap_cache: dict[str, tuple[float, Snapshot]] = {}
        self._lock = threading.Lock()
        self._ttl = float(a.get("cache_ttl_sec") or 20.0)
        # 行情快照同样要有保鲜期。没有它，常驻服务（尤其是定时任务）会一直拿
        # 进程启动时那一份行情反复分析，评分永远不变。
        self._snap_ttl = float(a.get("snapshot_ttl_sec") or self._ttl)

    # ------------------------------------------------------------------
    def analyze(self, symbol: str, *, mode: str = "intraday", force: bool = False,
                account: dict | None = None) -> Plan:
        sym = to_contract(symbol)
        mode = mode if mode in MODES else "intraday"
        key = f"{sym}:{mode}"
        now = time.time()
        if not force:
            with self._lock:
                hit = self._plan_cache.get(key)
                if hit and now - hit[0] < self._ttl:
                    return hit[1]

        snap = self.fetch(sym, force=force)
        cfg = self.cfg
        if account:
            cfg = {**cfg, "account": {**(cfg.get("account") or {}), **account}}
        plan = build_plan(snap, cfg, mode=mode)
        with self._lock:
            self._plan_cache[key] = (now, plan)
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
        """前端一次拿全：交易计划 + 行情快照 + K线。"""
        plan = self.analyze(symbol, mode=mode, force=force, account=account)
        snap = self.fetch(symbol)
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
        return [p.to_dict() for _, p in rows]

    def health(self) -> list[dict]:
        return self.client.health()
