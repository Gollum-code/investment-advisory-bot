from __future__ import annotations

import os
import tempfile
import unittest

from iabot import snapcache
from iabot.market import Candle, Snapshot

T0 = 1_700_000_000.0


def _snap(symbol="BTC-USDT", price=100.0):
    k = [Candle(ts=T0 + i * 86400, open=100.0 + i, high=101.0 + i,
                low=99.0 + i, close=100.5 + i, volume=5.0) for i in range(30)]
    return Snapshot(symbol=symbol, price=price, turnover_24h=1e7,
                    contract_size=0.001, best_bid=99.9, best_ask=100.1,
                    klines={"1day": k})


class TestSnapCache(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            snap = _snap()
            snapcache.save(snap, cache_dir=d, now=T0)
            got = snapcache.load("BTC-USDT", cache_dir=d, ttl_sec=3600, now=T0 + 10)
            self.assertIsNotNone(got)
            self.assertEqual(got.symbol, "BTC-USDT")
            self.assertAlmostEqual(got.price, 100.0)
            self.assertAlmostEqual(got.turnover_24h, 1e7)
            self.assertAlmostEqual(got.contract_size, 0.001)
            self.assertEqual(len(got.klines["1day"]), 30)
            self.assertAlmostEqual(got.klines["1day"][0].close, 100.5)
            self.assertEqual(got.klines["1day"][0].ts, T0)

    def test_expired_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            snapcache.save(_snap(), cache_dir=d, now=T0)
            got = snapcache.load("BTC-USDT", cache_dir=d, ttl_sec=60,
                                 now=T0 + 120)
            self.assertIsNone(got)

    def test_negative_ttl_never_expires(self):
        with tempfile.TemporaryDirectory() as d:
            snapcache.save(_snap(), cache_dir=d, now=T0)
            got = snapcache.load("BTC-USDT", cache_dir=d, ttl_sec=-1,
                                 now=T0 + 10_000_000)
            self.assertIsNotNone(got)

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(snapcache.load("NOPE-USDT", cache_dir=d))

    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "BTC-USDT.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertIsNone(snapcache.load("BTC-USDT", cache_dir=d))

    def test_snapshot_without_klines_is_not_cached_as_hit(self):
        with tempfile.TemporaryDirectory() as d:
            snap = _snap()
            snap.klines = {}
            snapcache.save(snap, cache_dir=d, now=T0)
            self.assertIsNone(snapcache.load("BTC-USDT", cache_dir=d, now=T0 + 1))

    def test_get_or_fetch_uses_cache_second_time(self):
        calls = []

        def fetch():
            calls.append(1)
            return _snap()

        with tempfile.TemporaryDirectory() as d:
            a = snapcache.get_or_fetch(fetch, "BTC-USDT", cache_dir=d, now=T0)
            b = snapcache.get_or_fetch(fetch, "BTC-USDT", cache_dir=d, now=T0 + 5)
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            self.assertEqual(len(calls), 1)   # 第二次命中缓存，不再抓

    def test_get_or_fetch_refetches_after_ttl(self):
        calls = []

        def fetch():
            calls.append(1)
            return _snap()

        with tempfile.TemporaryDirectory() as d:
            snapcache.get_or_fetch(fetch, "BTC-USDT", cache_dir=d, ttl_sec=10,
                                   now=T0)
            snapcache.get_or_fetch(fetch, "BTC-USDT", cache_dir=d, ttl_sec=10,
                                   now=T0 + 100)
            self.assertEqual(len(calls), 2)

    def test_symbol_with_slash_is_safe_filename(self):
        with tempfile.TemporaryDirectory() as d:
            snapcache.save(_snap(symbol="A/B.C"), cache_dir=d, now=T0)
            got = snapcache.load("A/B.C", cache_dir=d, now=T0 + 1)
            self.assertIsNotNone(got)


if __name__ == "__main__":
    unittest.main()
