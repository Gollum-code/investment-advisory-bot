"""HTTP 服务端点的单元测试：本机起一个临时服务，不联网。"""

import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from iabot.config import DEFAULTS
from iabot.server import build_server, make_app


class TestServerAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = copy.deepcopy(DEFAULTS)
        cfg["open_browser"] = False
        cfg["rest_bases"] = []  # 不探测域名，避免测试联网
        cfg["storage"] = {"db_file": str(Path(self.tmp.name) / "signals.db"),
                          "keep_days": 30}
        self.app = make_app(cfg)
        self.srv = build_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.app.stop()
        self.tmp.cleanup()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_meta_ok(self):
        code, body = self._get("/api/meta")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_history_bad_limit_is_400(self):
        code, body = self._get("/api/history?limit=abc")
        self.assertEqual(code, 400)
        err = json.loads(body)
        self.assertFalse(err["ok"])
        self.assertIn("abc", err["error"])

    def test_stats_bad_hours_is_400(self):
        code, _ = self._get("/api/stats?hours=xyz")
        self.assertEqual(code, 400)

    def test_plan_bad_equity_is_400(self):
        code, _ = self._get("/api/plan?symbol=BTC-USDT&equity=abc")
        self.assertEqual(code, 400)

    def test_history_export_empty_is_200(self):
        code, body = self._get("/api/history/export")
        self.assertEqual(code, 200)
        self.assertEqual(body, "")

    def test_history_export_after_save(self):
        self.app.store.save({"symbol": "BTC-USDT", "mode": "intraday",
                             "direction": "long", "score": 12.3,
                             "confidence": "低", "generated_at": 1.0})
        code, body = self._get("/api/history/export")
        self.assertEqual(code, 200)
        self.assertIn("BTC-USDT", body)

    def test_purge_bad_days_is_400(self):
        import urllib.request as ur
        req = ur.Request(f"http://127.0.0.1:{self.port}/api/history/purge",
                         data=b'{"days": "abc"}', method="POST",
                         headers={"Content-Type": "application/json"})
        try:
            with ur.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        self.assertEqual(code, 400)

    def test_win_stats_empty_ok(self):
        code, body = self._get("/api/win-stats")
        self.assertEqual(code, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertEqual(d["win"]["resolved"], 0)

    def test_win_stats_reports_filled_outcome(self):
        rid = self.app.store.save({"symbol": "ETH-USDT", "mode": "intraday",
                                   "direction": "long", "score": 65.0,
                                   "confidence": "中", "generated_at": 1.0})
        self.app.store.set_outcome(rid, "tp1", rr=2.0, mfe=2.0, mae=-0.5)
        code, body = self._get("/api/win-stats")
        self.assertEqual(code, 200)
        win = json.loads(body)["win"]
        self.assertEqual(win["resolved"], 1)
        self.assertEqual(win["wins"], 1)

    def test_breadth_aggregates(self):
        from iabot.server import market_breadth
        plans = [
            {"symbol": "BTC-USDT", "direction": "long", "score": 60.0},
            {"symbol": "ETH-USDT", "direction": "long", "score": 35.0},
            {"symbol": "SOL-USDT", "direction": "short", "score": -55.0},
            {"symbol": "XRP-USDT", "direction": "wait", "score": 10.0},
        ]
        b = market_breadth(plans)
        self.assertEqual(b["scanned"], 4)
        self.assertEqual(b["longs"], 2)
        self.assertEqual(b["shorts"], 1)
        self.assertEqual(b["waits"], 1)
        self.assertAlmostEqual(b["bull_ratio"], 2 / 3, places=2)
        self.assertAlmostEqual(b["bear_ratio"], 1 / 3, places=2)
        self.assertEqual(b["sentiment"], "偏多")


if __name__ == "__main__":
    unittest.main()
