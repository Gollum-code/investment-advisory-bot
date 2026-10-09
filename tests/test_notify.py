"""信号通知的单元测试（不发真实请求，mock 掉 HTTP 层）。"""

import unittest

from iabot.notify import Notifier, render_signal


class _Plan:
    @staticmethod
    def long():
        return {"symbol": "BTC-USDT", "mode": "intraday", "direction": "long",
                "score": 65.0, "confidence": "高", "price": 30000.0,
                "entry_low": 29800.0, "entry_high": 29900.0, "stop": 29700.0,
                "stop_pct": 0.8, "rr": 1.8,
                "targets": [{"price": 30200.0, "rr": 1.8, "label": "TP1"}],
                "sizing": {"applicable": True, "contracts": 3, "margin_usdt": 90.0,
                           "loss_if_stopped_usdt": 7.0},
                "entry_note": "等回踩再进"}


class TestRenderSignal(unittest.TestCase):
    def test_long_contains_key_fields(self):
        text = render_signal(_Plan.long())
        self.assertIn("BTC-USDT", text)
        self.assertIn("做多", text)
        self.assertIn("止损", text)
        self.assertIn("TP1", text)

    def test_wait_has_no_sizing(self):
        p = _Plan.long()
        p["direction"] = "wait"
        text = render_signal(p)
        self.assertIn("观望", text)
        self.assertNotIn("仓位", text)


class _Notifier(Notifier):
    """记录 send 调用，不发真实请求。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.sent = []

    def send(self, title, text):
        self.sent.append((title, text))
        return 1 if self.enabled and self.configured else 0


def _cfg(**notify):
    base = {"tls_verify": True, "notify": {"enabled": True,
                                           "webhook": {"url": "https://example.invalid/hook"}}}
    base["notify"].update(notify)
    return base


class TestNotifySignal(unittest.TestCase):
    def test_disabled_by_default(self):
        n = _Notifier({"notify": {"enabled": False, "webhook": {"url": "x"}}})
        self.assertEqual(n.notify_signal(_Plan.long()), 0)

    def test_strong_signal_notified(self):
        n = _Notifier(_cfg(min_abs_score=45))
        self.assertEqual(n.notify_signal(_Plan.long()), 1)
        self.assertEqual(len(n.sent), 1)

    def test_weak_signal_skipped(self):
        p = _Plan.long()
        p["score"] = 20.0
        n = _Notifier(_cfg(min_abs_score=45))
        self.assertEqual(n.notify_signal(p), 0)

    def test_direction_change_notified(self):
        p = _Plan.long()
        p["score"] = 20.0  # 弱信号，但方向翻转也应推
        n = _Notifier(_cfg(min_abs_score=45, on_direction_change=True))
        self.assertEqual(n.notify_signal(p, prev_direction="short"), 1)

    def test_wait_never_notified(self):
        p = _Plan.long()
        p["direction"] = "wait"
        n = _Notifier(_cfg(min_abs_score=0))
        self.assertEqual(n.notify_signal(p, prev_direction="long"), 0)

    def test_notify_outcome(self):
        n = _Notifier(_cfg())
        self.assertEqual(n.notify_outcome("BTC-USDT", "tp1", mfe_pct=2.0, mae_pct=-0.5), 1)
        self.assertIn("BTC-USDT", n.sent[0][1])

    def test_status_reports_channels(self):
        n = _Notifier(_cfg())
        st = n.status()
        self.assertTrue(st["configured"])
        self.assertTrue(st["channels"]["webhook"])


if __name__ == "__main__":
    unittest.main()
