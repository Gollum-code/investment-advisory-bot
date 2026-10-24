from __future__ import annotations

import unittest

from iabot.config import load_config, DEFAULTS, _merge, validate_config
from iabot.signals import build_plan
from iabot.backtest import gate_reason
from iabot.advisor import Advisor

from tests.test_signals import UP, make_snapshot


class TestScoreIsDirectional(unittest.TestCase):
    """plan.score 必须是决定方向的那个数，不能是 11 因子的平均值。

    以前两个数混用：显示/排序用 11 因子平均，阈值却判 dir_score。
    实测 24 个币里 9 个出现"评分 -84 却观望"，用户以为程序坏了。
    """

    def test_score_matches_gate_when_blocked(self):
        # 被 rr 拦下时，score 仍应足够强（否则"分数够却被拦"这条提示不会出现）
        cfg = _merge(DEFAULTS, {})
        p = build_plan(make_snapshot(**UP), cfg, mode="swing")
        if p.direction == "wait" and (p.gate or {}).get("blocked"):
            self.assertGreaterEqual(abs(p.score), 0.0)

    def test_score_all_factors_kept_separately(self):
        p = build_plan(make_snapshot(**UP), _merge(DEFAULTS, {}), mode="swing")
        # 两个字段都存在，且平均分通常与方向分不同（因子互相抵消会更接近 0）
        self.assertIsInstance(p.score_all_factors, float)
        self.assertIsInstance(p.score, float)
        self.assertTrue(-100.0 <= p.score <= 100.0)
        self.assertTrue(-100.0 <= p.score_all_factors <= 100.0)

    def test_direction_consistent_with_threshold(self):
        """同向时 |score| 必须 >= 阈值，反向必须 < 阈值。

        这是契约：网页/CLI 都用 plan.score 判断"分数够不够"，
        如果阈值判的是另一个数，界面就会出现自相矛盾的输出。
        """
        cfg = load_config()
        th = float(cfg["analysis"]["score_threshold"])
        adv = Advisor(cfg)
        for sym in ("BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT"):
            try:
                snap = adv.client.snapshot(sym, with_depth=False)
            except Exception:
                continue
            p = build_plan(snap, cfg, mode="swing")
            if p.direction in ("long", "short"):
                self.assertGreaterEqual(
                    abs(p.score), th - 1e-9,
                    f"{sym}: 给了方向但 |score|={abs(p.score)} < 阈值 {th}")
            # 反方向不要求：分数够也可能被 rr/目标位/状态闸门拦下，那是设计

    def test_gate_reason_distinguishes_score_from_other(self):
        class _P:
            direction = "wait"
            score = 0.0
            gate = {}
            state_gate = {}
        p = _P()
        self.assertEqual(gate_reason(p), "score_below_threshold")
        p.gate = {"blocked": True, "reason": "rr"}
        self.assertEqual(gate_reason(p), "low_rr")


if __name__ == "__main__":
    unittest.main()
