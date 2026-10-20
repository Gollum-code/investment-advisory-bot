"""快照磁盘缓存：把一次抓好的多周期快照存下来，短期内重复回测不再重新抓。

为什么要它：`validate` / 反复调参的回测会对同一批标的反复抓 5 个周期 ×
2000 根 K 线，一次几十秒。真正变的只是参数，行情没变。缓存 30 分钟既能
省掉绝大部分重复请求，又不会让分析用上过期太久的行情。

只缓存"整份快照"，不碰实盘分析路径（Analyze/scan 仍然实时抓），所以不存在
"用旧价格下单"的风险。缓存文件损坏、字段缺失、超过 TTL 都直接当作未命中。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import data_path
from .market import Candle, Snapshot

DEFAULT_DIR = "data/snapcache"
DEFAULT_TTL_SEC = 1800.0


def _snap_path(symbol: str, cache_dir: str | Path | None = None) -> Path:
    base = Path(cache_dir) if cache_dir else data_path(DEFAULT_DIR)
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in symbol)
    return base / f"{safe}.json"


def save(snap: Snapshot, cache_dir: str | Path | None = None,
         now: float | None = None) -> Path:
    p = _snap_path(snap.symbol, cache_dir)
    payload = {
        "saved_at": float(now if now is not None else time.time()),
        "symbol": snap.symbol,
        "price": snap.price,
        "turnover_24h": snap.turnover_24h,
        "funding_rate": snap.funding_rate,
        "contract_size": snap.contract_size,
        "best_bid": snap.best_bid,
        "best_ask": snap.best_ask,
        "klines": {
            period: [[c.ts, c.open, c.high, c.low, c.close, c.volume]
                     for c in rows]
            for period, rows in (snap.klines or {}).items()
        },
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return p


def load(symbol: str, *, cache_dir: str | Path | None = None,
         ttl_sec: float = DEFAULT_TTL_SEC,
         now: float | None = None) -> Snapshot | None:
    """命中返回快照，未命中/过期/损坏返回 None（调用方去真实抓）。"""
    p = _snap_path(symbol, cache_dir)
    if not p.exists():
        return None
    now = float(now if now is not None else time.time())
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        saved = float(payload.get("saved_at") or 0)
        if ttl_sec >= 0 and (now - saved) > ttl_sec:
            return None
        klines = {}
        for period, rows in (payload.get("klines") or {}).items():
            klines[period] = [
                Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                       low=float(r[3]), close=float(r[4]), volume=float(r[5]))
                for r in rows
            ]
        snap = Snapshot(
            symbol=payload.get("symbol") or symbol,
            price=payload.get("price"),
            turnover_24h=payload.get("turnover_24h"),
            funding_rate=payload.get("funding_rate"),
            contract_size=payload.get("contract_size"),
            best_bid=payload.get("best_bid"),
            best_ask=payload.get("best_ask"),
            klines=klines,
        )
        return snap if snap.price and klines else None
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def get_or_fetch(fetch, symbol: str, *, cache_dir: str | Path | None = None,
                 ttl_sec: float = DEFAULT_TTL_SEC,
                 now: float | None = None) -> Snapshot | None:
    """先读缓存，未命中再调用 fetch()，并把结果写回缓存。"""
    hit = load(symbol, cache_dir=cache_dir, ttl_sec=ttl_sec, now=now)
    if hit is not None:
        return hit
    snap = fetch()
    if snap is not None and snap.price and snap.klines:
        try:
            save(snap, cache_dir=cache_dir, now=now)
        except OSError:
            pass
    return snap
