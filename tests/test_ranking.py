from __future__ import annotations

import unittest

from iabot.validation import (direction_stats, factor_attribution,
                              score_ranking)


def _log(score, r, direction="long", factor="趋势日线", outcome="tp1"):
    return {"score": score, "r": r, "direction": direction,
            "top_factor": factor, "outcome": outcome}


class TestScoreRanking(unittest.TestCase):
    def test_monotone_means_ranking_works(self):
        # 分数越高越赚 -> 有限排序能力（每档 5 笔，共 15 笔）
        logs = [_log(s, r) for s, r in
                [(4, -1.0), (5, -0.9), (6, -0.8), (7, -0.7), (8, -0.6),
                 (12, 0.1), (14, 0.2), (16, 0.3), (18, 0.4), (20, 0.5),
                 (40, 1.0), (50, 1.2), (60, 1.4), (70, 1.6), (80, 1.8)]]
        out = score_ranking(logs, buckets=3)
        self.assertTrue(out["usable"])
        self.assertTrue(out["monotone"])
        self.assertEqual(out["verdict"], "分数有排序能力")
        self.assertGreater(out["gap_r"], 0.05)

    def test_non_monotone_is_flagged(self):
        # 中间档最好、最高档最差（实测就是这样）
        logs = ([_log(5, 0.2)] * 5 + [_log(15, 0.9)] * 5 +
                [_log(60, 0.1)] * 5)
        out = score_ranking(logs, buckets=3)
        self.assertTrue(out["usable"])
        self.assertFalse(out["monotone"])
        self.assertIn("排序能力不明显", out["verdict"])

    def test_too_few_samples(self):
        out = score_ranking([_log(10, 1.0)] * 4, buckets=3)
        self.assertFalse(out["usable"])
        self.assertEqual(out["buckets"], [])

    def test_ignores_pending_rows(self):
        logs = [_log(10, None)] * 20 + [_log(20, 1.0)] * 20
        out = score_ranking(logs, buckets=2)
        self.assertTrue(out["usable"])
        self.assertEqual(sum(b["trades"] for b in out["buckets"]), 20)

    def test_bucket_counts_cover_all(self):
        logs = [_log(10 + i, 0.1 * i) for i in range(30)]
        out = score_ranking(logs, buckets=3)
        self.assertEqual(sum(b["trades"] for b in out["buckets"]), 30)


class TestDirectionStats(unittest.TestCase):
    def test_split(self):
        logs = ([_log(10, 1.0, "long")] * 4 +
                [_log(10, -0.5, "short")] * 2)
        out = direction_stats(logs)
        self.assertEqual(out["long"]["trades"], 4)
        self.assertAlmostEqual(out["long"]["expectancy_r"], 1.0)
        self.assertEqual(out["short"]["trades"], 2)
        self.assertAlmostEqual(out["short"]["expectancy_r"], -0.5)
        self.assertAlmostEqual(out["long"]["win_rate"], 1.0)
        self.assertAlmostEqual(out["short"]["win_rate"], 0.0)

    def test_missing_side_omitted(self):
        out = direction_stats([_log(10, 1.0, "long")])
        self.assertIn("long", out)
        self.assertNotIn("short", out)

    def test_short_win_rate_uses_positive_r(self):
        # 空单的"赢"看 R 正负，不能按价格方向判断
        out = direction_stats([_log(10, 2.0, "short"),
                               _log(10, -1.0, "short")])
        self.assertAlmostEqual(out["short"]["win_rate"], 0.5)


class TestFactorAttribution(unittest.TestCase):
    def test_grouped_and_sorted_by_count(self):
        logs = ([_log(10, 0.2, factor="趋势日线")] * 5 +
                [_log(10, -0.1, factor="波动率")] * 2)
        out = factor_attribution(logs)
        self.assertEqual(out[0]["factor"], "趋势日线")
        self.assertEqual(out[0]["trades"], 5)
        self.assertAlmostEqual(out[0]["expectancy_r"], 0.2)
        self.assertEqual(out[1]["factor"], "波动率")
        self.assertAlmostEqual(out[1]["expectancy_r"], -0.1)

    def test_top_n(self):
        logs = ([_log(10, 0.1, factor="A")] * 3 +
                [_log(10, 0.1, factor="B")] * 2 +
                [_log(10, 0.1, factor="C")] * 2 +
                [_log(10, 0.1, factor="D")] * 1)
        out = factor_attribution(logs, top_n=2)
        self.assertEqual(len(out), 2)

    def test_rows_without_factor_skipped(self):
        self.assertEqual(factor_attribution([_log(10, 1.0, factor="")]), [])


if __name__ == "__main__":
    unittest.main()
