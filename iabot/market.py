"""火币(HTX) USDT 本位永续合约行情客户端。

全部是公开只读接口，不需要 API Key。任何单个接口失败都不会让整个分析崩掉——
拿不到的数据会以 None / 空列表返回，由上层决定怎么降权。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .netutil import BasePool

PERIODS = ["1min", "5min", "15min", "30min", "60min", "4hour", "12hour", "1day", "1week", "1mon"]

# 不同周期的"每年多少根K线"，用来算年化波动率
_PERIOD_BARS_PER_YEAR = {
    "1min": 525600, "5min": 105120, "15min": 35040, "30min": 17520,
    "60min": 8760, "4hour": 2190, "12hour": 730, "1day": 365,
    "1week": 52, "1mon": 12,
}
_PERIOD_SECONDS = {
    "1min": 60, "5min": 300, "15min": 900, "30min": 1800, "60min": 3600,
    "4hour": 14400, "12hour": 43200, "1day": 86400, "1week": 604800, "1mon": 2592000,
}


def bars_per_year(period: str) -> float:
    return float(_PERIOD_BARS_PER_YEAR.get(period, 365))


def period_seconds(period: str) -> int:
    return _PERIOD_SECONDS.get(period, 86400)


@dataclass
class Candle:
    ts: int          # 秒
    open: float
    high: float
    low: float
    close: float
    volume: float    # 张

    def to_dict(self) -> dict:
        return {"ts": self.ts, "open": self.open, "high": self.high,
                "low": self.low, "close": self.close, "volume": self.volume}


@dataclass
class Snapshot:
    """一次完整的行情快照。"""
    symbol: str
    fetched_at: float = field(default_factory=time.time)
    base_used: str = ""
    price: float | None = None
    index_price: float | None = None
    open24: float | None = None
    high: float | None = None
    low: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    bid_volume: float | None = None   # 张
    ask_volume: float | None = None
    volume_24h: float | None = None   # 张
    turnover_24h: float | None = None # USDT

    contract_size: float = 0.001   # 1 张 = 多少 BTC/币

    klines: dict[str, list[Candle]] = field(default_factory=dict)
    depth: dict[str, list[list[float]]] = field(default_factory=dict)  # {"asks": [[p,q],...]}
    funding_rate: float | None = None
    funding_next_ts: int | None = None
    funding_history: list[dict] = field(default_factory=list)
    open_interest: float | None = None      # 张
    oi_history: list[dict] = field(default_factory=list)
    elite_account: list[dict] = field(default_factory=list)
    elite_position: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # ---- 便捷属性 ----
    @property
    def mid(self) -> float | None:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.price

    @property
    def spread(self) -> float | None:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def change_24h_pct(self) -> float | None:
        if self.price and self.open24:
            return (self.price / self.open24 - 1.0) * 100.0
        return None

    @property
    def basis_pct(self) -> float | None:
        if self.price and self.index_price:
            return (self.price / self.index_price - 1.0) * 100.0
        return None

    def closes(self, period: str) -> list[float]:
        return [c.close for c in self.klines.get(period, [])]

    def summary(self) -> dict:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "index_price": self.index_price,
            "change_24h_pct": self.change_24h_pct,
            "basis_pct": self.basis_pct,
            "high": self.high,
            "low": self.low,
            "spread": self.spread,
            "volume_24h": self.volume_24h,
            "turnover_24h": self.turnover_24h,
            "funding_rate": self.funding_rate,
            "open_interest": self.open_interest,
            "contract_size": self.contract_size,
            "base_used": self.base_used,
            "fetched_at": self.fetched_at,
            "errors": self.errors,
        }


class MarketClient:
    """带缓存的行情客户端。同一个 (symbol, period) 在 ttl 内不会重复请求。"""

    def __init__(self, bases: list[str], *, timeout: float = 12.0, retries: int = 2,
                 verify: bool = True, cache_ttl: float = 2.0) -> None:
        self.pool = BasePool(bases, timeout=timeout, retries=retries, verify=verify)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._ttl = cache_ttl

    # ---------- 底层 ----------
    def _cached(self, key: str, fn):
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
        val = fn()
        with self._lock:
            self._cache[key] = (now, val)
        return val

    def _get(self, path: str):
        return self.pool.get(path)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ---------- 单接口 ----------
    def ticker(self, symbol: str) -> dict:
        return self._cached(f"ticker:{symbol}", lambda: self._get(
            f"/linear-swap-ex/market/detail/merged?contract_code={symbol}"))

    def kline(self, symbol: str, period: str, size: int = 300) -> list[Candle]:
        def fetch():
            d = self._get(
                f"/linear-swap-ex/market/history/kline?contract_code={symbol}"
                f"&period={period}&size={size}")
            rows = d.get("data") or []
            out = [Candle(ts=int(r["id"]), open=float(r["open"]), high=float(r["high"]),
                          low=float(r["low"]), close=float(r["close"]),
                          volume=float(r.get("vol") or r.get("volume") or 0.0))
                   for r in rows]
            out.sort(key=lambda c: c.ts)
            return out
        return self._cached(f"kline:{symbol}:{period}:{size}", fetch)

    def depth(self, symbol: str, kind: str = "step2") -> dict:
        def fetch():
            d = self._get(f"/linear-swap-ex/market/depth?contract_code={symbol}&type={kind}")
            tick = d.get("tick") or {}
            return {
                "asks": [[float(p), float(q)] for p, q in (tick.get("asks") or [])],
                "bids": [[float(p), float(q)] for p, q in (tick.get("bids") or [])],
            }
        return self._cached(f"depth:{symbol}:{kind}", fetch)

    def contract_info(self, symbol: str) -> dict:
        def fetch():
            d = self._get(f"/linear-swap-api/v1/swap_contract_info?contract_code={symbol}")
            rows = d.get("data") or []
            return rows[0] if rows else {}
        return self._cached(f"cinfo:{symbol}", fetch)

    def funding(self, symbol: str) -> dict:
        def fetch():
            d = self._get(f"/linear-swap-api/v1/swap_funding_rate?contract_code={symbol}")
            return d.get("data") or {}
        return self._cached(f"fund:{symbol}", fetch)

    def funding_history(self, symbol: str, page_size: int = 90) -> list[dict]:
        def fetch():
            d = self._get(
                f"/linear-swap-api/v1/swap_historical_funding_rate"
                f"?contract_code={symbol}&page_size={page_size}")
            rows = (d.get("data") or {}).get("data") or []
            # 火币该接口返回的是倒序，统一排成正序（最后一条 = 最新）
            return sorted(rows, key=lambda r: int(r.get("funding_time") or 0))
        return self._cached(f"fundh:{symbol}", fetch)

    def open_interest(self, symbol: str) -> float | None:
        def fetch():
            d = self._get(f"/linear-swap-api/v1/swap_open_interest?contract_code={symbol}")
            rows = d.get("data") or []
            if not rows:
                return None
            return float(rows[0].get("volume") or 0.0) or None
        return self._cached(f"oi:{symbol}", fetch)

    def oi_history(self, symbol: str, period: str = "1day", size: int = 60) -> list[dict]:
        def fetch():
            d = self._get(
                f"/linear-swap-api/v1/swap_his_open_interest?contract_code={symbol}"
                f"&period={period}&amount_type=1&size={size}")
            rows = (d.get("data") or {}).get("tick") or []
            # 火币该接口返回的是倒序，统一排成正序（最后一条 = 最新）
            return sorted(rows, key=lambda r: int(r.get("ts") or 0))
        return self._cached(f"oih:{symbol}:{period}", fetch)

    def elite_account(self, symbol: str, period: str = "1day") -> list[dict]:
        def fetch():
            d = self._get(
                f"/linear-swap-api/v1/swap_elite_account_ratio"
                f"?contract_code={symbol}&period={period}")
            rows = (d.get("data") or {}).get("list") or []
            return sorted(rows, key=lambda r: int(r.get("ts") or 0))
        return self._cached(f"ea:{symbol}:{period}", fetch)

    def elite_position(self, symbol: str, period: str = "1day") -> list[dict]:
        def fetch():
            d = self._get(
                f"/linear-swap-api/v1/swap_elite_position_ratio"
                f"?contract_code={symbol}&period={period}")
            return (d.get("data") or {}).get("list") or []
        return self._cached(f"ep:{symbol}:{period}", fetch)

    def index_price(self, symbol: str) -> float | None:
        def fetch():
            d = self._get(f"/linear-swap-api/v1/swap_index?contract_code={symbol}")
            rows = d.get("data") or []
            return float(rows[0]["index_price"]) if rows else None
        return self._cached(f"idx:{symbol}", fetch)

    # ---------- 组合 ----------
    def snapshot(self, symbol: str, *, periods: list[str] | None = None,
                 kline_size: int = 300, with_depth: bool = True,
                 with_extra: bool = True) -> Snapshot:
        """一次性抓齐分析所需的全部数据，单个接口失败只记录不抛错。"""
        periods = periods or ["1min", "5min", "15min", "60min", "1day"]
        snap = Snapshot(symbol=symbol)

        def guard(tag: str, fn, default=None):
            try:
                return fn()
            except Exception as exc:
                snap.errors.append(f"{tag}: {type(exc).__name__}: {exc}")
                return default

        info = guard("contract_info", lambda: self.contract_info(symbol), {})
        if info.get("contract_size"):
            snap.contract_size = float(info["contract_size"])

        tick = guard("ticker", lambda: self.ticker(symbol), {})
        t = (tick or {}).get("tick") or {}
        if t:
            snap.price = float(t.get("close") or 0) or None
            snap.open24 = float(t.get("open") or 0) or None
            snap.high = float(t.get("high") or 0) or None
            snap.low = float(t.get("low") or 0) or None
            snap.volume_24h = float(t.get("amount") or 0) or None
            snap.turnover_24h = float(t.get("trade_turnover") or 0) or None
            bid = t.get("bid") or []
            ask = t.get("ask") or []
            if len(bid) >= 2:
                snap.best_bid, snap.bid_volume = float(bid[0]), float(bid[1])
            if len(ask) >= 2:
                snap.best_ask, snap.ask_volume = float(ask[0]), float(ask[1])

        snap.index_price = guard("index", lambda: self.index_price(symbol))

        # K 线之间互相独立，并行拉取（串行时 5 个周期要 ~5 秒）
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(periods)))) as pool:
            futs = {p: pool.submit(self.kline, symbol, p, kline_size) for p in periods}
            for p, fut in futs.items():
                try:
                    rows = fut.result()
                except Exception as exc:
                    snap.errors.append(f"kline:{p}: {type(exc).__name__}: {exc}")
                    rows = []
                if rows:
                    snap.klines[p] = rows

        if with_depth:
            snap.depth = guard("depth", lambda: self.depth(symbol, "step2"), {}) or {}

        if with_extra:
            # 这几个接口也互相独立，一起并发
            extra = {}
            jobs = {
                "funding": lambda: self.funding(symbol),
                "funding_history": lambda: self.funding_history(symbol, 90),
                "open_interest": lambda: self.open_interest(symbol),
                "oi_history": lambda: self.oi_history(symbol, "1day", 60),
                "elite_account": lambda: self.elite_account(symbol, "1day"),
                "elite_position": lambda: self.elite_position(symbol, "1day"),
            }
            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = {k: pool.submit(v) for k, v in jobs.items()}
                for k, fut in futs.items():
                    try:
                        extra[k] = fut.result()
                    except Exception as exc:
                        snap.errors.append(f"{k}: {type(exc).__name__}: {exc}")
                        extra[k] = None

        if with_extra:
            f = extra.get("funding") or {}
            if f.get("funding_rate") not in (None, ""):
                snap.funding_rate = float(f["funding_rate"])
            if f.get("funding_time"):
                snap.funding_next_ts = int(int(f["funding_time"]) // 1000)
            snap.funding_history = extra.get("funding_history") or []
            snap.open_interest = extra.get("open_interest")
            snap.oi_history = extra.get("oi_history") or []
            snap.elite_account = extra.get("elite_account") or []
            snap.elite_position = extra.get("elite_position") or []

        snap.base_used = self.pool.current or ""
        return snap

    def health(self) -> list[dict]:
        return self.pool.health()
