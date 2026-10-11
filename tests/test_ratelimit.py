"""429 限流识别与指数退避重试的单元测试（不联网）。"""

import unittest
import urllib.error
from unittest import mock

from iabot import netutil


def _http_429(retry_after=None):
    hdrs = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError("http://x", 429, "Too Many Requests", hdrs, None)


class _Resp:
    def __init__(self, payload=b'{"status":"ok","tick":{"a":1}}'):
        self._p = payload

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRateLimit(unittest.TestCase):
    def test_is_rate_limited(self):
        self.assertTrue(netutil._is_rate_limited(_http_429()))
        other = urllib.error.HTTPError("http://x", 500, "err", {}, None)
        self.assertFalse(netutil._is_rate_limited(other))
        self.assertFalse(netutil._is_rate_limited(ValueError("nope")))

    def test_retry_after_parsing(self):
        self.assertEqual(netutil._retry_after(_http_429(3), 1.0), 3.0)
        # 无/非法 Retry-After -> 用默认
        self.assertEqual(netutil._retry_after(_http_429(), 1.0), 1.0)
        self.assertEqual(netutil._retry_after(_http_429("bad"), 2.0), 2.0)

    def test_retries_on_429_then_succeeds(self):
        calls = {"n": 0}

        def fake_urlopen(*a, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:      # 前两次 429
                raise _http_429()
            return _Resp()

        with mock.patch.object(netutil.urllib.request, "urlopen", fake_urlopen):
            with mock.patch.object(netutil.time, "sleep"):
                d = netutil.http_json("http://x", retries=2)
        self.assertEqual(d["status"], "ok")
        self.assertEqual(calls["n"], 3)   # 2 次失败 + 1 次成功

    def test_gives_up_after_retries(self):
        calls = {"n": 0}

        def always_429(*a, **kw):
            calls["n"] += 1
            raise _http_429()

        with mock.patch.object(netutil.urllib.request, "urlopen", always_429):
            with mock.patch.object(netutil.time, "sleep"):
                with self.assertRaises(urllib.error.HTTPError):
                    netutil.http_json("http://x", retries=2)
        self.assertEqual(calls["n"], 3)   # 初始 + 2 次重试

    def test_non_429_not_retried(self):
        calls = {"n": 0}

        def always_500(*a, **kw):
            calls["n"] += 1
            raise urllib.error.HTTPError("http://x", 500, "e", {}, None)

        with mock.patch.object(netutil.urllib.request, "urlopen", always_500):
            with self.assertRaises(urllib.error.HTTPError):
                netutil.http_json("http://x", retries=3)
        self.assertEqual(calls["n"], 1)   # 非限流不重试


if __name__ == "__main__":
    unittest.main()
