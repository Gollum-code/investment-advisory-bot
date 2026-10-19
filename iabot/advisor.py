"""编排层：把 行情抓取 -> 指标计算 -> 信号打分 -> 交易计划 串起来，并做结果缓存。"""

from __future__ import annotations

import threading
import time
from typing import Any

from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import DEFAULT_MODE
from .market import MarketClient, Snapshot
from .signals import MODES, Plan, build_plan
from .symbols import to_contract
from .universe import build_radar, expand_tokens, resolve

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

    def analyze(self, symbol: str, *, mode: str = DEFAULT_MODE, force: bool = False,
                account: dict | None = None, snap: Snapshot | None = None) -> Plan:
        sym = to_contract(symbol)
        mode = mode if mode in MODES else DEFAULT_MODE
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
            # 写入时刻用"现在"而不是函数入口的 now：并发抓行情时单次可能要好几秒，
            # 按开始时刻算会让条目一落地就"过期大半"（与 MarketClient._cached 同理）。
            self._plan_cache[key] = (time.time(), snap.fetched_at, plan)
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
            self._snap_cache[sym] = (time.time(), snap)
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
    def plan_payload(self, symbol: str, *, mode: str = DEFAULT_MODE, force: bool = False,
                     account: dict | None = None) -> dict:
        return self.analyze(symbol, mode=mode, force=force, account=account).to_dict()

    def full_payload(self, symbol: str, *, mode: str = DEFAULT_MODE, force: bool = False,
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

    # ------------------------------------------------------------------
    def scan(self, symbols: list[str], *, mode: str = DEFAULT_MODE,
             account: dict | None = None, workers: int = 4, force: bool = True,
             only_signal: bool = False,
             on_result: Any = None, should_stop: Any = None,
             ) -> tuple[list[dict], list[str]]:
        """并发分析一批合约，返回 (plans, errors)，plans 按 |评分| 降序。

        全市场动辄几十上百个合约，串行每个 3-10 秒根本跑不完，所以用线程池。
        workers 默认 4：火币公开接口对高频请求会限流（429），并发抬太高反而
        更慢——429 会触发指数退避重试。on_result(symbol, plan, error) 每完成
        一个回调一次；should_stop() 返回 True 时不再派发新任务（定时任务收尾用）。
        """
        plans: list[dict] = []
        errors: list[str] = []
        if not symbols:
            return plans, errors
        workers = max(1, min(int(workers or 4), len(symbols)))

        def work(sym: str) -> dict:
            return self.plan_payload(sym, mode=mode, account=account, force=force)

        def _notify(sym, plan, err):
            # on_result 是调用方给的回调（落库 + 推送 + SSE），它自己抛异常
            # 不该把整轮扫描带下——否则剩下的合约全被丢弃，调用方连
            # run_done 都发不出去，前端永远等不到"这轮结束"。
            if not on_result:
                return
            try:
                on_result(sym, plan, err)
            except Exception as exc:
                errors.append(f"{sym}: 回调异常 {type(exc).__name__}: {exc}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(work, s): s for s in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                if should_stop and should_stop():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    p = fut.result()
                except Exception as exc:
                    msg = f"{sym}: {type(exc).__name__}: {exc}"
                    errors.append(msg)
                    _notify(sym, None, msg)
                    continue
                if not only_signal or p.get("direction") != "wait":
                    plans.append(p)
                _notify(sym, p, None)
        plans.sort(key=lambda p: -abs(p.get("score") or 0))
        return plans, errors

    def radar(self) -> list[dict]:
        """全市场雷达表：所有合约的现价/涨跌/成交额/分类（一次请求）。"""
        return build_radar(self.client)

    def universe(self, *, override: dict | None = None) -> list[str]:
        """按配置筛选出该深挖的全市场合约代码列表。"""
        return resolve(self.client, self.cfg, override=override)

    def expand_symbols(self, tokens: list[str], *,
                       override: dict | None = None) -> list[str]:
        """把 `all` / 具体合约混合的写法展开成完整合约列表。"""
        return expand_tokens(self.client, tokens, self.cfg, override=override)

    def health(self) -> list[dict]:
        return self.client.health()
