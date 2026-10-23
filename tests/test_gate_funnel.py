from __future__ import annotations

import unittest

from iabot.backtest import gate_reason
from iabot.signals import Plan
from iabot.validation import GATE_CN, gate_funnel


def _plan(**kw):
    p = Plan(symbol="BTC-USDT", mode="swing", price=100.0)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class TestGateReason(unittest.TestCase):
    def test_passes_through_when_directional(self):
        self.assertEqual(gate_reason(_plan(direction="long")), "")

    def test_position_gates(self):
        self.assertEqual(gate_reason(_plan(direction="wait", gate={
            "blocked": True, "reason": "no_target"})), "no_target")
        self.assertEqual(gate_reason(_plan(direction="wait", gate={
            "blocked": True, "reason": "target_too_far"})), "target_too_far")
        self.assertEqual(gate_reason(_plan(direction="wait", gate={
            "blocked": True, "reason": "rr"})), "low_rr")

    def test_data_gates_are_not_score_gates(self):
        # 没数据不该被算成"评分不够"，否则漏斗会指错方向
        self.assertEqual(gate_reason(_plan(direction="wait", gate={
            "blocked": True, "reason": "no_price"})), "no_price")
        self.assertEqual(gate_reason(_plan(direction="wait", gate={
            "blocked": True, "reason": "no_atr"})), "no_atr")

    def test_state_gates(self):
        for key in ("trend_veto", "score_band", "liquidity",
                    "subordinate_blocked"):
            expected = "subordinate" if key == "subordinate_blocked" else key
            self.assertEqual(
                gate_reason(_plan(direction="wait", state_gate={key: True})),
                expected)

    def test_bare_wait_is_score(self):
        self.assertEqual(gate_reason(_plan(direction="wait")),
                         "score_below_threshold")


class TestGateFunnel(unittest.TestCase):
    def test_sums_and_sorts(self):
        rows = [
            {"gate_counts": {"no_target": 3, "low_rr": 10}},
            {"gate_counts": {"no_target": 2, "no_fill": 1}},
        ]
        got = gate_funnel(rows)
        self.assertEqual(list(got.items())[0], ("low_rr", 10))
        self.assertEqual(got["no_target"], 5)
        self.assertEqual(got["no_fill"], 1)

    def test_empty(self):
        self.assertEqual(gate_funnel([]), {})
        self.assertEqual(gate_funnel([{}]), {})

    def test_every_key_has_label(self):
        rows = [{"gate_counts": {k: 1 for k in
                                 ("score_below_threshold", "no_target",
                                  "target_too_far", "low_rr", "trend_veto",
                                  "score_band", "liquidity", "subordinate",
                                  "no_fill", "no_price", "no_atr")}}]
        for k in gate_funnel(rows):
            self.assertIn(k, GATE_CN, f"{k} 没有中文标签")


if __name__ == "__main__":
    unittest.main()
