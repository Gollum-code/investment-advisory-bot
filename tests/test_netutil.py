"""BasePool 探测缓存保鲜（域名自愈）的测试，不联网。"""

import time
import unittest

from iabot.netutil import BasePool


class FakePool(BasePool):
    """把 _probe 换成纯内存实现，验证探测缓存的行为。"""

    def __init__(self, results):
        super().__init__(list(results), timeout=1.0, retries=1)
        self.results = results
        self.probe_calls = []

    def _probe(self, base):
        self.probe_calls.append(base)
        return self.results[base]


class TestProbeExpiry(unittest.TestCase):
    def test_fresh_probe_results_are_not_repeated(self):
        p = FakePool({"https://a.invalid": (True, 1.0, ""),
                      "https://b.invalid": (True, 2.0, "")})
        p.probe_all()
        p.probe_all()
        self.assertEqual(len(p.probe_calls), 2)

    def test_expired_results_are_reprobed(self):
        p = FakePool({"https://a.invalid": (True, 1.0, "")})
        p._probe_ttl = 0.01
        p.probe_all()
        time.sleep(0.02)
        p.probe_all()
        self.assertEqual(len(p.probe_calls), 2)  # 第二次重新探了

    def test_recovery_is_detected_after_ttl(self):
        """域名从可用变不可用后，过期会重新探测并看到新状态。"""
        p = FakePool({"https://a.invalid": (True, 1.0, "")})
        p._probe_ttl = 0.01
        p.probe_all()
        self.assertTrue(p._probe_cache["https://a.invalid"][0])
        p.results["https://a.invalid"] = (False, 1.0, "down")
        time.sleep(0.02)
        p.probe_all()
        self.assertFalse(p._probe_cache["https://a.invalid"][0])

    def test_health_uses_new_cache_shape(self):
        p = FakePool({"https://a.invalid": (True, 1.0, ""),
                      "https://b.invalid": (False, 2.0, "x")})
        rows = {r["base"]: r for r in p.health()}
        self.assertTrue(rows["https://a.invalid"]["ok"])
        self.assertFalse(rows["https://b.invalid"]["ok"])


if __name__ == "__main__":
    unittest.main()
