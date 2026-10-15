"""并发批量扫描的单元测试（不打网络）。"""

import threading
import time
import unittest

from iabot.advisor import Advisor
from iabot.config import DEFAULTS


class FakeClient:
    """每个合约耗时可控的假行情源，顺便记录是否被并发调用过。"""

    def __init__(self, symbols=(), delay=0.0, fail=()):
        self.symbols = set(symbols)
        self.delay = delay
        self.fail = set(fail)
        self.calls = []
        self._lock = threading.Lock()
        self._live = 0
        self.peak = 0

    def snapshot(self, symbol, *, periods, kline_size):
        with self._lock:
            self.calls.append(symbol)
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            if self.delay:
                time.sleep(self.delay)
            if symbol in self.fail:
                raise RuntimeError(f"{symbol} 炸了")
        finally:
            with self._lock:
                self._live -= 1
        return _snap(symbol)

    def kline(self, symbol, period, size=300):
        return []

    def health(self):
        return []


def _snap(symbol):
    from iabot.market import Snapshot
    return Snapshot(symbol=symbol, price=100.0)


class FakePlan:
    def __init__(self, symbol, score):
        self.symbol = symbol
        self.score = score

    def to_dict(self):
        return {"symbol": self.symbol, "score": self.score, "direction": "wait"}


class ScanTest(unittest.TestCase):
    def _advisor(self, client):
        cfg = dict(DEFAULTS)
        adv = Advisor(cfg)
        adv.client = client

        # 先真的走一遍 client（触发失败/耗时/并发统计），再返回可控的分数。
        def fake_payload(symbol, *, mode="intraday", force=False, account=None):
            adv.fetch(symbol, force=True)
            base = symbol.split("-")[0]
            score = float(base.replace("C", "") or 0)
            return FakePlan(symbol, score).to_dict()
        adv.plan_payload = fake_payload
        return adv

    def test_empty_input(self):
        plans, errs = self._advisor(FakeClient()).scan([])
        self.assertEqual((plans, errs), ([], []))

    def test_sorts_by_absolute_score(self):
        adv = self._advisor(FakeClient())
        plans, errs = adv.scan(["C10-USDT", "C50-USDT", "C30-USDT"])
        self.assertEqual([p["symbol"] for p in plans],
                         ["C50-USDT", "C30-USDT", "C10-USDT"])
        self.assertEqual(errs, [])

    def test_only_signal_filters_wait(self):
        adv = Advisor(DEFAULTS)
        adv.client = FakeClient()

        def payload(symbol, *, mode="intraday", force=False, account=None):
            d = "wait" if symbol.startswith("C1") else "long"
            return {"symbol": symbol, "score": 40.0, "direction": d}
        adv.plan_payload = payload
        plans, _ = adv.scan(["C10-USDT", "C20-USDT"], only_signal=True)
        self.assertEqual([p["symbol"] for p in plans], ["C20-USDT"])

    def test_errors_are_collected_per_symbol(self):
        client = FakeClient(fail=["C2-USDT"])
        adv = self._advisor(client)
        plans, errs = adv.scan(["C1-USDT", "C2-USDT", "C3-USDT"])
        self.assertEqual(len(plans), 2)
        self.assertEqual(len(errs), 1)
        self.assertIn("C2-USDT", errs[0])
        self.assertIn("炸了", errs[0])

    def test_one_failure_does_not_abort_batch(self):
        client = FakeClient(fail=["C1-USDT", "C3-USDT"])
        plans, errs = self._advisor(client).scan(
            ["C1-USDT", "C2-USDT", "C3-USDT", "C4-USDT"])
        self.assertEqual(len(plans), 2)
        self.assertEqual(len(errs), 2)

    def test_actually_runs_in_parallel(self):
        client = FakeClient(delay=0.15)
        adv = self._advisor(client)
        syms = [f"C{i}-USDT" for i in range(1, 7)]
        t0 = time.time()
        plans, _ = adv.scan(syms, workers=6)
        elapsed = time.time() - t0
        self.assertEqual(len(plans), 6)
        self.assertGreater(client.peak, 1, "没有真正并发")
        self.assertLess(elapsed, 0.15 * 6 * 0.6, f"并发没生效，耗时 {elapsed:.2f}s")

    def test_workers_capped_by_symbol_count(self):
        client = FakeClient(delay=0.02)
        adv = self._advisor(client)
        adv.scan(["C1-USDT"], workers=32)      # 不能因为 workers 大就起 32 个线程
        self.assertLessEqual(client.peak, 1)

    def test_callback_exception_does_not_abort_batch(self):
        """on_result 负责落库+推送+SSE，它自己抛异常（比如 SQLite 锁、通知超时）
        不该把整轮扫描带下：剩下的合约全被丢弃，调用方连 run_done 都发不出去，
        前端永远等不到"这轮结束"。"""
        seen = []

        def cb(sym, plan, err):
            seen.append(sym)
            if sym == "C2-USDT":
                raise RuntimeError("store exploded")

        plans, errs = self._advisor(FakeClient()).scan(
            [f"C{i}-USDT" for i in range(1, 7)], workers=2, on_result=cb)
        self.assertEqual(len(seen), 6, "回调异常导致后续合约没被处理")
        self.assertEqual(len(plans), 6)
        self.assertTrue(any("回调异常" in e for e in errs))

    def test_callback_exception_on_error_path_does_not_abort(self):
        """失败路径上的回调抛异常，同样不能中断。"""
        def cb(sym, plan, err):
            if err:
                raise RuntimeError("notifier down")

        plans, errs = self._advisor(FakeClient(fail=["C2-USDT"])).scan(
            [f"C{i}-USDT" for i in range(1, 6)], on_result=cb)
        self.assertEqual(len(plans), 4)
        self.assertEqual(len(errs), 2)   # 1 个真实失败 + 1 个回调异常

    def test_on_result_called_for_every_symbol(self):
        adv = self._advisor(FakeClient(fail=["C2-USDT"]))
        seen = []
        adv.scan(["C1-USDT", "C2-USDT", "C3-USDT"],
                 on_result=lambda s, p, e: seen.append((s, p is not None, bool(e))))
        self.assertEqual(len(seen), 3)
        self.assertEqual(sum(1 for s, ok, _ in seen if ok), 2)
        self.assertEqual(sum(1 for s, ok, e in seen if e), 1)

    def test_should_stop_halts_dispatch(self):
        client = FakeClient(delay=0.05)
        adv = self._advisor(client)
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] >= 1        # 第一个结果回来就收工

        syms = [f"C{i}-USDT" for i in range(1, 21)]
        adv.scan(syms, workers=2, should_stop=stop)
        self.assertLess(len(client.calls), 20, "should_stop 没拦住派发")


if __name__ == "__main__":
    unittest.main()