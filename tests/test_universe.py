"""全市场合约池的单元测试（不打网络，全部走假行情）。"""

import unittest

from iabot import universe as U


def row(symbol, *, turnover=1e6, price=10.0, labels=(), asset=None, change=None):
    labs = {str(x).lower() for x in labels}
    return {
        "symbol": symbol,
        "price": price,
        "change_pct": change,
        "turnover": turnover,
        "volume": turnover / price if price else 0.0,
        "count": 10,
        "contract_size": 0.001,
        "labels": sorted(labs),
        "asset_class": asset or ("crypto" if not labs else U.classify({"labels": labs})),
        "status": 1,
    }


ROWS = [
    row("BTC-USDT", turnover=3e8, labels=["hot", "common"]),
    row("ETH-USDT", turnover=2e8, labels=["common"]),
    row("SCAM-USDT", turnover=5e4, labels=["new"]),              # 流动性不够
    row("TSLA-USDT", turnover=9e7, labels=["stock", "tradfi"]),  # 代币化股票
    row("XAU-USDT", turnover=5e6, labels=["commodities"]),       # 黄金
    row("DEAD-USDT", turnover=0.0, price=0.0),
]


class TestSpec(unittest.TestCase):
    def test_defaults(self):
        s = U.merge_spec({})
        self.assertEqual(s["asset_class"], "crypto")
        self.assertEqual(s["max_symbols"], 60)
        self.assertEqual(s["sort"], "turnover")
        self.assertEqual(s["min_turnover_usdt"], 100000.0)

    def test_partial_override_keeps_defaults(self):
        s = U.merge_spec({"universe": {"max_symbols": 5}})
        self.assertEqual(s["max_symbols"], 5)
        self.assertEqual(s["asset_class"], "crypto")

    def test_lists_normalised_to_contracts(self):
        s = U.merge_spec({"universe": {"include": ["btc", " ETH-usdt "],
                                       "exclude": ["sol"]}})
        self.assertEqual(s["include"], ["BTC-USDT", "ETH-USDT"])
        self.assertEqual(s["exclude"], ["SOL-USDT"])

    def test_max_symbols_floor_is_one(self):
        self.assertEqual(U.merge_spec({"universe": {"max_symbols": 0}})["max_symbols"], 1)


class TestAllTokens(unittest.TestCase):
    def test_recognised(self):
        for t in ("all", "ALL", " 全部 ", "*", "全市场"):
            self.assertTrue(U.is_all_token(t), t)

    def test_not_recognised(self):
        for t in ("BTC-USDT", "", "alla", "eth"):
            self.assertFalse(U.is_all_token(t), t)


class TestClassify(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(U.classify({"labels": {"stock"}}), "stock")
        self.assertEqual(U.classify({"labels": {"tradfi"}}), "tradfi")
        self.assertEqual(U.classify({"labels": {"commodities"}}), "commodity")
        self.assertEqual(U.classify({"labels": {"indices"}}), "index")
        self.assertEqual(U.classify({"labels": {"hot"}}), "crypto")
        self.assertEqual(U.classify({"labels": set()}), "crypto")

    def test_is_crypto(self):
        self.assertTrue(U.is_crypto({"hot", "common"}))
        self.assertFalse(U.is_crypto({"stock"}))
        self.assertFalse(U.is_crypto({"tradfi"}))


class TestSelect(unittest.TestCase):
    def test_filters_stock_and_illiquid_by_default(self):
        picked = [r["symbol"] for r in U.select(ROWS, U.merge_spec({}))]
        self.assertEqual(picked, ["BTC-USDT", "ETH-USDT"])

    def test_asset_class_all_includes_everything_liquid(self):
        spec = U.merge_spec({"universe": {"asset_class": "all"}})
        picked = [r["symbol"] for r in U.select(ROWS, spec)]
        self.assertIn("TSLA-USDT", picked)
        self.assertIn("XAU-USDT", picked)
        self.assertNotIn("SCAM-USDT", picked)   # 成交额仍不达标

    def test_min_turnover_slider(self):
        spec = U.merge_spec({"universe": {"min_turnover_usdt": 2.5e8}})
        picked = [r["symbol"] for r in U.select(ROWS, spec)]
        self.assertEqual(picked, ["BTC-USDT"])

    def test_max_symbols_caps(self):
        spec = U.merge_spec({"universe": {"max_symbols": 1}})
        self.assertEqual([r["symbol"] for r in U.select(ROWS, spec)], ["BTC-USDT"])

    def test_sort_by_abs_change(self):
        rows = [
            row("A-USDT", turnover=9e6, change=1.0),
            row("B-USDT", turnover=9e6, change=-8.0),
            row("C-USDT", turnover=9e6, change=3.0),
        ]
        spec = U.merge_spec({"universe": {"sort": "abs_change"}})
        self.assertEqual([r["symbol"] for r in U.select(rows, spec)],
                         ["B-USDT", "C-USDT", "A-USDT"])

    def test_exclude_beats_include(self):
        spec = U.merge_spec({"universe": {"include": ["BTC-USDT"],
                                          "exclude": ["BTC-USDT"]}})
        self.assertNotIn("BTC-USDT", [r["symbol"] for r in U.select(ROWS, spec)])

    def test_include_waives_liquidity_floor_and_sorts_first(self):
        spec = U.merge_spec({"universe": {"include": ["SCAM-USDT"], "max_symbols": 1}})
        self.assertEqual([r["symbol"] for r in U.select(ROWS, spec)], ["SCAM-USDT"])

    def test_include_labels_waives_liquidity_floor(self):
        """include_labels 是白名单式捞回：命中标签的合约豁免流动性门槛。

        它以前只被解析进 spec 却从没用过，README 却写成"命中任一标签即优先纳入"。
        """
        rows = [
            row("AAA-USDT", turnover=1e7, labels=["new"]),
            row("BBB-USDT", turnover=5e4, labels=["new"]),    # 流动性不达标
            row("CCC-USDT", turnover=5e4, labels=["hot"]),    # 标签不匹配
        ]
        spec = U.merge_spec({"universe": {"include_labels": ["NEW"]}})
        picked = [r["symbol"] for r in U.select(rows, spec)]
        self.assertEqual(picked, ["AAA-USDT", "BBB-USDT"])
        self.assertNotIn("CCC-USDT", picked)

    def test_include_labels_sorts_after_normal_candidates(self):
        """靠标签捞回来的排在正常候选之后（成交额只有 1000 U 的新币不该压过主流币），
        但要为它预留 max_symbols 名额，否则照样被截掉。"""
        rows = [row(f"C{i}-USDT", turnover=1e8 - i, labels=["hot"]) for i in range(5)]
        rows.append(row("NEWCOIN-USDT", turnover=1000, labels=["new"]))
        spec = U.merge_spec({"universe": {"include_labels": ["new"], "max_symbols": 3}})
        picked = [r["symbol"] for r in U.select(rows, spec)]
        self.assertEqual(len(picked), 3)
        self.assertEqual(picked[-1], "NEWCOIN-USDT")   # 留了名额
        self.assertEqual(picked[:2], ["C0-USDT", "C1-USDT"])

    def test_include_labels_still_respects_exclude(self):
        rows = [row("BBB-USDT", turnover=1, labels=["new"])]
        spec = U.merge_spec({"universe": {"include_labels": ["new"],
                                          "exclude": ["BBB-USDT"]}})
        self.assertEqual(U.select(rows, spec), [])

    def test_include_labels_keeps_output_shape(self):
        rows = [row("NEWCOIN-USDT", turnover=1, labels=["new"])]
        spec = U.merge_spec({"universe": {"include_labels": ["new"]}})
        out = U.select(rows, spec)[0]
        self.assertEqual(sorted(out), sorted(rows[0]))

    def test_exclude_labels(self):
        spec = U.merge_spec({"universe": {"exclude_labels": ["HOT"]}})
        self.assertNotIn("BTC-USDT", [r["symbol"] for r in U.select(ROWS, spec)])

    def test_min_price_rejects_near_zero(self):
        rows = [row("A-USDT", turnover=9e6, price=0.0), row("B-USDT", turnover=9e6, price=2.0)]
        spec = U.merge_spec({"universe": {"min_price": 0.001}})
        self.assertEqual([r["symbol"] for r in U.select(rows, spec)], ["B-USDT"])


class TestRadarMerge(unittest.TestCase):
    """build_radar 用假 client，不触网。"""

    class FakeClient:
        def __init__(self, tickers, contracts):
            self._t = tickers
            self._c = contracts

        def contract_list(self):
            return self._c

        def all_tickers(self):
            return self._t

    def test_merges_and_classifies(self):
        client = self.FakeClient(
            tickers=[
                {"symbol": "BTC-USDT", "price": 100.0, "change_pct": 2.0,
                 "turnover": 3e8, "volume": 1.0, "count": 5},
                {"symbol": "TSLA-USDT", "price": 400.0, "change_pct": -1.0,
                 "turnover": 9e7, "volume": 1.0, "count": 3},
                {"symbol": "ETH-USDT", "price": None, "change_pct": None,
                 "turnover": 0.0, "volume": 0.0, "count": 0},
            ],
            contracts=[
                {"contract_code": "BTC-USDT", "contract_size": 0.001,
                 "labels": ["hot"], "contract_status": 1},
                {"contract_code": "TSLA-USDT", "contract_size": 0.0001,
                 "labels": ["stock", "tradfi"], "contract_status": 1},
                # 有清单没行情：也要出现在表里
                {"contract_code": "GONE-USDT", "contract_size": 1.0,
                 "labels": ["common"], "contract_status": 3},
            ])
        rows = U.build_radar(client)
        by = {r["symbol"]: r for r in rows}
        self.assertEqual(len(rows), 4)
        self.assertEqual(by["BTC-USDT"]["asset_class"], "crypto")
        self.assertEqual(by["TSLA-USDT"]["asset_class"], "stock")
        self.assertEqual(by["GONE-USDT"]["price"], None)
        self.assertEqual(by["GONE-USDT"]["status"], 3)
        # 按成交额降序
        self.assertEqual([r["symbol"] for r in rows],
                         ["BTC-USDT", "TSLA-USDT", "ETH-USDT", "GONE-USDT"])

    def test_survives_contract_list_failure(self):
        class Boom(self.FakeClient):
            def contract_list(self):
                raise RuntimeError("接口挂了")
        client = Boom(tickers=[{"symbol": "BTC-USDT", "price": 1.0, "turnover": 5.0,
                                "volume": 1.0, "count": 1, "change_pct": 0.0}],
                      contracts=[])
        rows = U.build_radar(client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset_class"], "crypto")

    def test_resolve_applies_override(self):
        client = self.FakeClient(
            tickers=[{"symbol": s, "price": 1.0, "turnover": t, "volume": 1.0,
                      "count": 1, "change_pct": 0.0}
                     for s, t in (("BTC-USDT", 1e8), ("ETH-USDT", 2e6))],
            contracts=[])
        got = U.resolve(client, {"universe": {"min_turnover_usdt": 1000}},
                        override={"min_turnover_usdt": 5e7})
        self.assertEqual(got, ["BTC-USDT"])

    def test_expand_tokens_mixes_all_with_literal(self):
        client = self.FakeClient(
            tickers=[{"symbol": "BTC-USDT", "price": 1.0, "turnover": 1e8,
                      "volume": 1.0, "count": 1, "change_pct": 0.0},
                     {"symbol": "ETH-USDT", "price": 1.0, "turnover": 2e6,
                      "volume": 1.0, "count": 1, "change_pct": 0.0}],
            contracts=[])
        got = U.expand_tokens(client, ["SOL-USDT", "all"],
                              {"universe": {"min_turnover_usdt": 1000}})
        # 字面写出的合约排在前面，全市场候选接在后面
        self.assertEqual(got, ["SOL-USDT", "BTC-USDT", "ETH-USDT"])

    def test_expand_tokens_dedupes(self):
        client = self.FakeClient(
            tickers=[{"symbol": "BTC-USDT", "price": 1.0, "turnover": 1e8,
                      "volume": 1.0, "count": 1, "change_pct": 0.0}],
            contracts=[])
        got = U.expand_tokens(client, ["btc", "BTC-USDT", "all"],
                              {"universe": {"min_turnover_usdt": 1000}})
        self.assertEqual(got, ["BTC-USDT"])

    def test_expand_tokens_without_all(self):
        client = self.FakeClient(tickers=[], contracts=[])
        self.assertEqual(U.expand_tokens(client, ["btc", "sol"]), ["BTC-USDT", "SOL-USDT"])


if __name__ == "__main__":
    unittest.main()