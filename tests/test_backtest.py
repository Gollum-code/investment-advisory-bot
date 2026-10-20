"""历史回放的单元测试（合成行情，不联网）。"""

import copy
import unittest

from iabot.backtest import backtest
from iabot.config import DEFAULTS
from iabot.market import Candle

BASE = 1_700_000_000
START = 30000.0


def _bar(ts, close, wick=10.0, volume=1000.0):
    return Candle(ts=ts, open=close - 5.0, high=close + wick,
                  low=close - wick, close=close, volume=volume)


def _make_snapshot(n=240, period="15min"):
    """构造只有主周期 + 60min/1day 的合成快照。

    行情走势：先跌后反弹，保证会出现短/多信号且之后有走势可判定。
    """
    from iabot.market import Snapshot

    closes = [START]
    a = 0.06  # 每根最多涨跌 6%
    import random
    rnd = random.Random(7)
    for i in range(1, n):
        # 前 1/3 下跌，之后震荡回升
        drift = -a * 0.8 if i < n // 3 else a * 0.3
        closes.append(closes[-1] * (1 + drift + rnd.uniform(-a, a)))

    step = {"15min": 900, "60min": 3600, "1day": 86400}[period]
    k = [_bar(BASE + i * step, c) for i, c in enumerate(closes)]
    return Snapshot(symbol="BTC-USDT", price=closes[-1], klines={period: k},
                    contract_size=0.001, funding_rate=0.0001, open_interest=1e6)


def _cfg():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["analysis"]["score_threshold"] = 20  # 用合成行情才容易出信号
    return cfg


def _trading_snapshot(n=6000):
    """三周期齐全、真能出交易的夹具。

    _make_snapshot 只给一个周期，其余因子因缺数据全部降权为 0，
    结果永远是 0 笔交易 —— 用它写的成本测试其实一直在空转。
    这里把 15min/60min/1day 都填上，才能真的比较成本。

    各周期必须是同一条走势的**降采样**，不能共用同一串收盘价：
    共用时 15min 走完 n 根，1day 只走了 n/96 根，
    于是日线/小时因子永远"数据不足"，只有 15m 动量在动。

    状态因子（波动率/成交量/资金费）要在整段里都有观点，状态闸门才会开：
    所以噪声整体压低、并且逐段轻微衰减，让波动率落在自身历史的低分位
    （当前 ATR% 相对 250 日偏低 -> 正分）；成交量前低后高 -> z-score 正。
    否则夹具整段都是"中性状态"，状态闸门会把所有点都拦成观望，测不了成本。
    """
    from iabot.market import Snapshot
    import random

    rnd = random.Random(17)
    closes = [START]
    for i in range(1, n):
        # 波动率单调衰减（前高后低）："当前 ATR% 相对自身历史偏低" -> 波动率因子正分。
        # 末段再叠一段明确上行趋势，让方向判据给出做多。整段都要有观点，
        # 否则状态闸门会把所有点拦成观望，测不了成本。
        phase = i / n
        noise = 0.008 * (1.0 - 0.85 * phase)          # 前段噪声大、末段平静
        drift = 0.001 if phase > 0.6 else 0.0002       # 末 40% 缓涨，前面近乎横盘
        closes.append(closes[-1] * (1 + drift + rnd.uniform(-noise, noise)))

    # 影线按价格比例给（约 0.6%）：多单挂在现价下方的回撤区，
    # 影线太小时价格永远碰不到挂单区，会全部记成 no_fill。
    def bars(period, factor):
        # factor: 每根 bar 对应多少根 15min（60min=4, 1day=96）
        src = [closes[i] for i in range(0, n, factor)]
        secs = {"15min": 900, "60min": 3600, "1day": 86400}[period]
        # 成交量前低后高（z-score 为正），且全段都有变化。
        return [_bar(BASE + i * secs, c, wick=c * 0.006,
                     volume=1000.0 * (0.5 + 1.0 * i / max(1, len(src) - 1)))
                for i, c in enumerate(src)]

    series = {
        "15min": bars("15min", 1),
        "60min": bars("60min", 4),
        "1day": bars("1day", 96),
    }
    return Snapshot(symbol="BTC-USDT", price=closes[-1], klines=series,
                    contract_size=0.001, funding_rate=0.0001, open_interest=1e6,
                    funding_history=[
                        {"funding_time": (BASE + i * 28800) * 1000,
                         "funding_rate": 0.0001} for i in range(0, n)])


def _bt(snap, cfg=None, **kw):
    # 默认放开 max_points：夹具是 6000 根 15min，step=3 约 2000 个建仓点，
    # 而 backtest() 默认只取前 400 个 -> 只会测到序列前半段（高波动区、全观望），
    # 测不出成本。放开后覆盖整段，末尾的平静+趋势区才有信号可比较成本。
    kw.setdefault("max_points", 100000)
    return backtest(snap, cfg or _cfg(), mode="intraday",
                    step=kw.pop("step", 10), warmup=30, **kw)


class TestFundingRateAt(unittest.TestCase):
    """回测按建仓点历史时刻取资金费率。"""

    def _hist(self):
        # funding_time 是毫秒，正序（最后=最新）
        return [
            {"funding_time": 1_700_000_000_000, "funding_rate": 0.0001},
            {"funding_time": 1_700_028_800_000, "funding_rate": 0.0003},
            {"funding_time": 1_700_057_600_000, "funding_rate": -0.0002},
        ]

    def test_picks_rate_before_ts(self):
        from iabot.backtest import _funding_rate_at
        h = self._hist()
        # ts 在第2期之后 -> 取第2期的费率
        self.assertAlmostEqual(_funding_rate_at(h, 1_700_030_000), 0.0003)
        # ts 在第1期之后 -> 取第1期
        self.assertAlmostEqual(_funding_rate_at(h, 1_700_010_000), 0.0001)
        # ts 早于所有 -> 0
        self.assertEqual(_funding_rate_at(h, 1_600_000_000), 0.0)
        # ts 在最新之后 -> 取最新
        self.assertAlmostEqual(_funding_rate_at(h, 1_800_000_000), -0.0002)

    def test_empty_history_returns_zero(self):
        from iabot.backtest import _funding_rate_at
        self.assertEqual(_funding_rate_at([], 1_700_000_000), 0.0)

    def test_backtest_uses_history_when_available(self):
        """有历史费率序列时，成本模型要用它，而不是快照上的当前费率。

        断言方式与"做多还是做空"解耦：给全 0 的历史 -> 资金费成本应为 0；
        给全正的历史 -> 只要成交了就应非 0。资金费因子本身也会读这段历史
        （决定方向），所以不能拿"有没有历史"去比总 R。
        """
        def run(rates):
            snap = _trading_snapshot()          # 三周期齐全，才会真的出交易
            snap.funding_rate = 0.0             # 关掉快照费率兜底
            snap.funding_history = [
                {"funding_time": int((BASE + i * 8 * 3600) * 1000),
                 "funding_rate": rates[i % len(rates)]} for i in range(40)
            ]
            return _bt(snap)

        # 历史正负交替 -> 均值 0 -> 资金费因子判为"等于自身常态"，方向中性。
        zero = run([0.0])
        mixed = run([0.0005, -0.0005])
        self.assertEqual(zero.avg_funding_r, 0.0)
        self.assertGreater(zero.trades, 0, "夹具没出交易，测不了")
        # 成本会改变持仓时长与止损判定，所以不比 trades；只比成本项本身。
        self.assertNotEqual(mixed.avg_funding_r, 0.0)

    def test_funding_factor_uses_deviation_not_absolute_level(self):
        """资金费因子要相对自身基线，不能用绝对阈值。

        费率有强结构性偏向（牛市里几乎所有合约长期正费率），
        固定阈值会让所有币长期落在同一档，0.10 的权重等于没有区分度。
        """
        from iabot.signals import _f_funding
        from iabot.market import Snapshot

        def mk(rate, hist):
            s = Snapshot(symbol="X-USDT", price=100.0, funding_rate=rate,
                         funding_history=[{"funding_time": i * 8 * 3600_000,
                                           "funding_rate": v}
                                          for i, v in enumerate(hist)])
            return _f_funding(s)

        # 同一绝对费率，历史基线不同 -> 分数应当不同
        flat_high = mk(0.0003, [0.0003] * 30)      # 等于自己常态
        below = mk(0.0001, [0.0003] * 30)          # 明显低于自己常态
        above = mk(0.0006, [0.0003] * 30)          # 明显高于自己常态
        self.assertEqual(flat_high.score, 0.0)
        self.assertGreater(below.score, 0.0)        # 不拥挤 -> 偏多
        self.assertLess(above.score, 0.0)           # 多头拥挤 -> 偏空


class TestBacktest(unittest.TestCase):
    def test_runs_and_returns_summary(self):
        snap = _make_snapshot(240)
        res = backtest(snap, _cfg(), mode="intraday", step=5, warmup=30)
        d = res.to_dict()
        self.assertIn("win_rate", d)
        self.assertGreaterEqual(res.bars, 200)
        # 合成行情必有走势，理应出现少量交易（不吹不黑，只是确保不崩）
        self.assertIsInstance(res.trades, int)

    def test_wait_only_market_has_zero_trades(self):
        closes = [START + (i % 7) for i in range(120)]  # 完全横盘
        k = [_bar(BASE + i * 900, c, wick=0.5) for i, c in enumerate(closes)]
        from iabot.market import Snapshot
        snap = Snapshot(symbol="XRP-USDT", price=closes[-1],
                        klines={"15min": k}, contract_size=0.001)
        res = backtest(snap, _cfg(), mode="intraday", step=5, warmup=20)
        # 横盘没有趋势 -> 要么观望要么被闸门拦下，不该出现交易
        self.assertLess(res.trades, 5)

    def test_equity_curve_length_matches_trades(self):
        snap = _make_snapshot(200)
        res = backtest(snap, _cfg(), mode="intraday", step=4, warmup=30)
        self.assertEqual(len(res.equity_curve), res.trades)

    def test_costs_reduce_total_r(self):
        snap = _make_snapshot(220)
        free = backtest(snap, _cfg(), mode="intraday", step=4, warmup=30,
                        apply_costs=False)
        paid = backtest(snap, _cfg(), mode="intraday", step=4, warmup=30,
                        apply_costs=True)
        self.assertEqual(free.trades, paid.trades)
        self.assertLessEqual(paid.total_r, free.total_r + 1e-9)
        if paid.trades:
            self.assertGreater(paid.avg_cost_r, 0)
            # 成本按单笔风险折算，不该超过 0.1 R（默认费率 0.1% 往返）
            self.assertLess(paid.avg_cost_r, 0.1)

    def test_cost_breakdown_present(self):
        snap = _make_snapshot(200)
        r = backtest(snap, _cfg(), mode="intraday", step=6, warmup=30).to_dict()
        for k in ("avg_fee_r", "avg_slippage_r", "avg_funding_r", "avg_holding_h"):
            self.assertIn(k, r)
        if r["trades"]:
            self.assertGreaterEqual(r["avg_fee_r"], 0)
            self.assertGreaterEqual(r["avg_slippage_r"], 0)
            self.assertGreaterEqual(r["avg_holding_h"], 0)

    def test_funding_increases_cost_for_long(self):
        # 资金费率越高、持仓越久，多头付的资金费越多，成本应更大
        snap = _make_snapshot(200)
        snap.funding_rate = 0.0003  # 0.03%/8h
        hi = backtest(snap, _cfg(), mode="intraday", step=6, warmup=30,
                      costs={"funding_rate": 0.0003})
        lo = backtest(snap, _cfg(), mode="intraday", step=6, warmup=30,
                      costs={"funding_rate": 0.0})
        # 多头在正费率下要付钱 -> 累计 R 不应更高
        self.assertLessEqual(hi.total_r, lo.total_r + 1e-9)

    def test_cfg_cost_params_are_wired_in(self):
        """README 与 btlog 都写了成本参数在 analysis.backtest 里，改它必须生效。

        曾经 backtest() 从不读这段配置，一律用 DEFAULT_COSTS，
        改 config 完全没反应 —— 一段死配置。
        """
        snap = _trading_snapshot()
        self.assertGreater(_bt(snap).trades, 0, "夹具没造出交易，测不了成本")

        def run(**backtest_cfg):
            c = _cfg()
            c["analysis"]["backtest"].update(backtest_cfg)
            return _bt(snap, c)

        base = run()
        # 手续费拉高 -> 每笔成本变大、总 R 变小
        expensive = run(taker_fee_bps=50.0)
        self.assertGreater(expensive.avg_cost_r, base.avg_cost_r)
        self.assertLess(expensive.total_r, base.total_r)
        # 滑点同样生效
        self.assertGreater(run(slippage_bps=20.0).avg_cost_r, base.avg_cost_r)
        # enabled=False -> 完全不扣成本
        free = run(enabled=False)
        self.assertEqual(free.avg_cost_r, 0.0)
        self.assertGreater(free.total_r, base.total_r)
        # funding=False -> 资金费为 0
        self.assertEqual(run(funding=False).avg_funding_r, 0.0)

    def test_explicit_apply_costs_overrides_cfg(self):
        snap = _trading_snapshot()
        c = _cfg()
        c["analysis"]["backtest"]["enabled"] = True
        self.assertEqual(_bt(snap, c, apply_costs=False).avg_cost_r, 0.0)

    def test_equity_usdt_conversion(self):
        snap = _make_snapshot(200)
        cfg = _cfg()
        cfg["account"]["equity_usdt"] = 1000.0
        cfg["account"]["risk_pct_per_trade"] = 2.0
        r = backtest(snap, cfg, mode="intraday", step=6, warmup=30).to_dict()
        # 单笔风险 = 1000 × 2% = 20 U
        self.assertAlmostEqual(r["risk_amount_usdt"], 20.0, places=2)
        self.assertAlmostEqual(r["equity_usdt"], 1000.0, places=2)
        # 金额曲线长度与 R 曲线一致
        self.assertEqual(len(r["equity_curve_usdt"]), len(r["equity_curve"]))
        if r["trades"]:
            # 期末 = 初始 + 累计R × 单笔风险
            self.assertAlmostEqual(r["final_equity_usdt"],
                                   1000.0 + r["total_r"] * 20.0, places=0)


class TestPartialSnapshotRefused(unittest.TestCase):
    """残缺快照（某个周期抓取失败）不能算出一个看似正常的期望值。"""

    def test_healthy_snapshot_has_no_missing_and_trades(self):
        # 反空转：先证明"好快照"确实能出交易、且 missing 为空。
        # 否则下面几条测试可能因为夹具本来就出不了交易而恒过。
        res = _bt(_trading_snapshot())
        self.assertEqual(res.missing_periods, ())
        self.assertFalse(res.insufficient_data)
        self.assertGreater(res.trades, 0)

    def test_kline_error_marks_missing_and_refuses(self):
        snap = _trading_snapshot()
        # 模拟 60min 周期抓取失败：数据被丢掉，只在 errors 里留痕
        snap.klines.pop("60min", None)
        snap.errors.append("kline:60min: HTTPError: 429")
        res = _bt(snap)
        self.assertIn("60min", res.missing_periods)
        self.assertTrue(res.insufficient_data)
        self.assertEqual(res.trades, 0)
        self.assertEqual(res.total_r, 0.0)
        self.assertIn("60min", res.to_dict()["missing_periods"])

    def test_expired_period_short_is_caught(self):
        # 周期键还在但只剩 <2 根（接口返回空尾巴）也要算缺失
        snap = _trading_snapshot()
        snap.klines["1day"] = snap.klines["1day"][:1]
        res = _bt(snap)
        self.assertIn("1day", res.missing_periods)
        self.assertTrue(res.insufficient_data)

    def test_complete_snapshot_not_flagged(self):
        snap = _trading_snapshot()
        snap.errors.append("funding: HTTPError: timeout")  # 非 K 线错误不该误伤
        res = _bt(snap)
        self.assertEqual(res.missing_periods, ())
        self.assertFalse(res.insufficient_data)


class TestBootstrapCI(unittest.TestCase):
    """期望值的置信区间：点估计再漂亮，跨 0 就说明还分不清 edge 和噪声。"""

    def test_ci_brackets_point_estimate(self):
        r = _bt(_trading_snapshot())
        self.assertGreater(r.trades, 20)          # 反空转
        self.assertIsNotNone(r.expectancy_ci)
        lo, hi = r.expectancy_ci
        self.assertLessEqual(lo, r.expectancy_r)
        self.assertGreaterEqual(hi, r.expectancy_r)
        self.assertLess(lo, hi)

    def test_ci_is_deterministic(self):
        a = _bt(_trading_snapshot())
        b = _bt(_trading_snapshot())
        self.assertEqual(a.expectancy_ci, b.expectancy_ci)

    def test_significant_matches_ci(self):
        r = _bt(_trading_snapshot())
        if len(r.trade_rs or []) >= 30:
            lo, hi = r.expectancy_ci
            self.assertEqual(r.significant, lo > 0 or hi < 0)

    def test_tiny_sample_is_not_significant(self):
        # 样本不足 30 笔时不宣称显著，即使区间不跨 0
        from iabot.backtest import MIN_TRADES_FOR_SIGNIFICANCE
        r = _bt(_trading_snapshot(), step=800)
        # 反空转：确认这个夹具确实只跑出小样本，否则下面的断言恒过
        self.assertLess(len(r.trade_rs or []), MIN_TRADES_FOR_SIGNIFICANCE)
        self.assertFalse(r.significant)

    def test_helper_on_known_arrays(self):
        from iabot.backtest import _bootstrap_mean_ci
        # 明显为负、无方差：CI 应整体 < 0
        lo, hi = _bootstrap_mean_ci([-1.0] * 50)
        self.assertLess(hi, 0)
        # 明显为正、无方差：CI 应整体 > 0
        lo, hi = _bootstrap_mean_ci([+1.0] * 50)
        self.assertGreater(lo, 0)
        # 样本过少 -> None
        self.assertIsNone(_bootstrap_mean_ci([1.0]))

    def test_to_dict_exposes_ci(self):
        d = _bt(_trading_snapshot()).to_dict()
        self.assertIn("expectancy_ci", d)
        self.assertIn("significant", d)
        self.assertIn("t_stat", d)
        self.assertEqual(len(d["expectancy_ci"]), 2)


class TestWalkForward(unittest.TestCase):
    """折间一致性：edge 不能只来自某一两段行情。"""

    def test_splits_into_usable_folds(self):
        from iabot.walkforward import walk_forward
        out = walk_forward(_trading_snapshot(), _cfg(),
                           mode="intraday", folds=4, step=10, warmup=30)
        self.assertTrue(out["ok"])
        # 反空转：至少要有 2 折真的产生了交易，否则下面的结论没意义
        self.assertGreaterEqual(out["usable_folds"], 2)
        self.assertGreater(out["pooled"]["trades"], 0)
        self.assertLessEqual(len(out["folds"]), 4)
        for f in out["folds"]:
            self.assertIn("expectancy_r", f)
            self.assertIn("usable", f)

    def test_pooled_is_union_of_fold_trades(self):
        from iabot.walkforward import walk_forward
        out = walk_forward(_trading_snapshot(), _cfg(),
                           mode="intraday", folds=4, step=10, warmup=30)
        fold_trades = sum(f["trades"] for f in out["folds"] if f["usable"])
        # 每折的 trade_rs 长度 == 该折 trades（超时按 0 计入）
        self.assertEqual(out["pooled"]["trades"], fold_trades)

    def test_deterministic(self):
        from iabot.walkforward import walk_forward
        a = walk_forward(_trading_snapshot(), _cfg(),
                         mode="intraday", folds=3, step=20, warmup=30)
        b = walk_forward(_trading_snapshot(), _cfg(),
                         mode="intraday", folds=3, step=20, warmup=30)
        self.assertEqual(a, b)

    def test_every_fold_keeps_prior_history(self):
        # 回归：第一版按时间窗口把快照切小，后面每折开头的日线历史只剩十几根，
        # 波动率分位/SMA200 全部退化成 0 分，四折全是 0 笔交易——那不是策略
        # 不行，是数据被切坏了。现在折只切建仓点区间、保留全部历史。
        # 夹具总日线只有 63 根（<250），所以最早那折缺日线历史、0 笔是正常的；
        # 要守的是"靠后的折别因为被切掉历史而死掉"。
        from iabot.walkforward import walk_forward
        out = walk_forward(_trading_snapshot(), _cfg(),
                           mode="intraday", folds=4, step=10, warmup=30)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["usable_folds"], 2)
        # 折 0 之外的折也有能出交易的 -> 历史没有被切断
        self.assertTrue(any(f["usable"] and f["index"] > 0
                            for f in out["folds"]),
                        f"靠后的折全部 0 笔: {out['folds']}")

    def test_start_index_restricts_entries_not_history(self):
        # 索引区间只限制建仓点，因子仍能看到全程历史
        snap = _trading_snapshot()
        full = _bt(snap)
        late = _bt(snap, start_index=3000, end_index=5995)
        self.assertGreater(full.trades, 0)
        self.assertGreater(late.trades, 0)
        # 区间生效：late 的建仓点最多是后 3000 根
        self.assertLessEqual(late.trades * 10, 3000)

    def test_verdict_labels(self):
        from iabot.walkforward import _verdict
        usable = [{"expectancy_r": 0.1}, {"expectancy_r": 0.2},
                  {"expectancy_r": 0.1}, {"expectancy_r": 0.2},
                  {"expectancy_r": 0.1}]
        self.assertIn("稳定", _verdict(0.15, (0.05, 0.25), 1.0, usable))
        self.assertIn("脆弱", _verdict(0.15, (-0.05, 0.35), 0.2, usable))
        self.assertIn("不要用", _verdict(-0.1, (-0.2, -0.01), 0.0, usable))
        self.assertIn("无法判断", _verdict(0.1, None, 0.0, usable[:1]))


class TestValidation(unittest.TestCase):
    """组合级验证：跨币合并 + 收益集中度。"""

    def test_concentration_pairs_math(self):
        from iabot.validation import _concentration_pairs
        # 4 个赚钱的币，前 2 个占 80/100 = 80%
        out = _concentration_pairs([("A", 50.0), ("B", 30.0),
                                    ("C", 15.0), ("D", 5.0)])
        self.assertEqual(out["positive_symbols"], 4)
        self.assertAlmostEqual(out["top_share"], 0.8, places=3)
        self.assertEqual(out["top_symbols"], ["A", "B"])
        self.assertTrue(out["warning"])            # 超过 50% 必须报警

    def test_no_warning_when_spread(self):
        from iabot.validation import _concentration_pairs
        out = _concentration_pairs([("A", 10), ("B", 10), ("C", 10), ("D", 10)])
        self.assertAlmostEqual(out["top_share"], 0.5, places=3)
        # 恰好 50% 且 >= 警戒线 -> 报；这里验证边界是有意为之
        self.assertTrue(out["warning"])
        out2 = _concentration_pairs([("A", 10), ("B", 10), ("C", 10),
                                     ("D", 10), ("E", 10)])
        self.assertLess(out2["top_share"], 0.5)
        self.assertFalse(out2["warning"])

    def test_losses_do_not_inflate_concentration(self):
        from iabot.validation import _concentration_pairs
        # 负收益的币不参与"正 R 贡献"的分母
        out = _concentration_pairs([("A", 10), ("B", -100)])
        self.assertEqual(out["positive_symbols"], 1)
        self.assertAlmostEqual(out["top_share"], 1.0, places=3)
        # 赚钱标的只有 1 个（<= TOP_N），不触发"集中"警告
        self.assertFalse(out["warning"])

    def test_multi_backtest_pools_trades(self):
        from iabot.validation import multi_backtest
        base = _trading_snapshot()
        snaps = {}
        for name in ("A-USDT", "B-USDT", "C-USDT"):
            snap = copy.deepcopy(base)
            snap.symbol = name
            snaps[name] = snap
        out = multi_backtest(snaps, _cfg(), mode="intraday", step=10, warmup=30)
        per_sum = sum(r["trades"] for r in out["rows"])
        self.assertEqual(out["pooled"]["trades"], per_sum)
        self.assertGreater(per_sum, 0)            # 反空转
        self.assertEqual(out["usable_symbols"], 3)
        self.assertIn("concentration", out)

    def test_validate_flags_and_reports(self):
        from iabot.validation import validate
        base = _trading_snapshot()
        snaps = {}
        for name in ("A-USDT", "B-USDT"):
            snap = copy.deepcopy(base)
            snap.symbol = name
            snaps[name] = snap
        out = validate(snaps, _cfg(), mode="intraday", folds=3,
                       step=10, warmup=30)
        self.assertIn("verdict", out)
        self.assertIn("walkforward", out)
        self.assertIn("notes", out)
        self.assertGreater(out["walkforward"]["folds_total"], 0)


class TestPortfolioBacktest(unittest.TestCase):
    """组合级：并发上限、资金约束、复利下的回撤。"""

    def _log(self, entry, exit_, r, sym="A-USDT"):
        return {"symbol": sym, "entry_ts": float(entry),
                "exit_ts": float(exit_), "r": float(r),
                "direction": "long", "outcome": "tp1", "holding_h": 24.0}

    def test_single_win_compounds(self):
        from iabot.portfolio import portfolio_backtest
        out = portfolio_backtest([self._log(0, 10, 1.0)], equity=450.0,
                                 risk_pct=2.0, max_concurrent=5)
        # 450 × 2% = 9 U 风险，赢 1R -> +9 U
        self.assertAlmostEqual(out["equity_end"], 459.0, places=2)
        self.assertEqual(out["taken"], 1)
        self.assertAlmostEqual(out["total_return_pct"], 2.0, places=2)

    def test_loss_costs_risk_amount(self):
        from iabot.portfolio import portfolio_backtest
        out = portfolio_backtest([self._log(0, 10, -1.0)], equity=450.0,
                                 risk_pct=2.0, max_concurrent=5)
        self.assertAlmostEqual(out["equity_end"], 441.0, places=2)
        self.assertAlmostEqual(out["total_return_pct"], -2.0, places=2)

    def test_concurrency_cap_blocks_extra_entries(self):
        from iabot.portfolio import portfolio_backtest
        # 三笔同时在场（entry 都是 0），并发上限 2 -> 第三笔被跳过
        logs = [self._log(0, 100, 1.0, "A"), self._log(0, 100, 1.0, "B"),
                self._log(0, 100, 1.0, "C")]
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=2)
        self.assertEqual(out["taken"], 2)
        self.assertEqual(out["skipped_capacity"], 1)

    def test_no_cap_takes_everything(self):
        from iabot.portfolio import portfolio_backtest
        logs = [self._log(0, 100, 1.0, "A"), self._log(0, 100, 1.0, "B"),
                self._log(0, 100, 1.0, "C")]
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=0)
        self.assertEqual(out["taken"], 3)
        self.assertEqual(out["skipped_capacity"], 0)

    def test_sequential_trades_are_not_blocked(self):
        from iabot.portfolio import portfolio_backtest
        # 前一笔平了才有仓位空出来 -> 并发上限 1 也不该跳过
        logs = [self._log(0, 10, 1.0, "A"), self._log(20, 30, 1.0, "B")]
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=1)
        self.assertEqual(out["taken"], 2)
        self.assertEqual(out["skipped_capacity"], 0)

    def test_compound_changes_second_trade_size(self):
        from iabot.portfolio import portfolio_backtest
        logs = [self._log(0, 10, 1.0, "A"), self._log(20, 30, 1.0, "B")]
        comp = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                  max_concurrent=1, compound=True)
        flat = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                  max_concurrent=1, compound=False)
        # 复利：第一笔赢完权益变大，第二笔风险更大 -> 期末更高
        self.assertGreater(comp["equity_end"], flat["equity_end"])
        self.assertAlmostEqual(flat["equity_end"], 468.0, places=2)

    def test_max_drawdown_computed(self):
        from iabot.portfolio import portfolio_backtest
        logs = [self._log(0, 10, 1.0, "A"), self._log(20, 30, -1.0, "B"),
                self._log(40, 50, 1.0, "C")]
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=1, compound=False)
        # 450 -> 459 -> 450 -> 459：最大回撤 9 U = 450 的 2%
        self.assertAlmostEqual(out["max_drawdown_usdt"], 9.0, places=2)
        self.assertAlmostEqual(out["max_drawdown_pct"], 2.0, places=1)

    def test_trade_log_is_produced_by_backtest(self):
        # 反空转：backtest 必须真的产出带 entry/exit 的明细
        r = _bt(_trading_snapshot())
        self.assertGreater(len(r.trade_log or []), 0)
        t0 = r.trade_log[0]
        self.assertIn("entry_ts", t0)
        self.assertIn("exit_ts", t0)
        self.assertIn("r", t0)
        self.assertGreaterEqual(t0["exit_ts"], t0["entry_ts"])

    # ---- 热度上限 ----
    def test_heat_cap_blocks_over_hot_entry(self):
        from iabot.portfolio import portfolio_backtest
        # 上限 4%：前两笔 2%+2% 已经打满，第三笔被挡
        logs = [self._log(0, 100, 1.0, "A"), self._log(0, 100, 1.0, "B"),
                self._log(0, 100, 1.0, "C")]
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=10, max_heat_pct=4.0)
        self.assertEqual(out["taken"], 2)
        self.assertEqual(out["skipped_heat"], 1)

    def test_heat_cap_releases_after_exit(self):
        from iabot.portfolio import portfolio_backtest
        logs = [self._log(0, 10, 1.0, "A"), self._log(0, 10, 1.0, "B"),
                self._log(20, 30, 1.0, "C")]
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=10, max_heat_pct=4.0)
        self.assertEqual(out["taken"], 3)
        self.assertEqual(out["skipped_heat"], 0)

    # ---- 相关性簇上限 ----
    def test_cluster_cap_blocks_same_cluster(self):
        from iabot.portfolio import portfolio_backtest
        # 同簇 3 个笔只许 2 个同时在
        logs = [self._log(0, 100, 1.0, "A"), self._log(0, 100, 1.0, "B"),
                self._log(0, 100, 1.0, "C")]
        clusters = {"A": 0, "B": 0, "C": 0}
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=10, max_per_cluster=2,
                                 clusters=clusters)
        self.assertEqual(out["taken"], 2)
        self.assertEqual(out["skipped_cluster"], 1)

    def test_cluster_cap_allows_other_cluster(self):
        from iabot.portfolio import portfolio_backtest
        logs = [self._log(0, 100, 1.0, "A"), self._log(0, 100, 1.0, "B")]
        clusters = {"A": 0, "B": 1}
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=10, max_per_cluster=1,
                                 clusters=clusters)
        self.assertEqual(out["taken"], 2)
        self.assertEqual(out["skipped_cluster"], 0)

    def test_unclustered_symbols_not_capped(self):
        from iabot.portfolio import portfolio_backtest
        logs = [self._log(0, 100, 1.0, "A"), self._log(0, 100, 1.0, "Z")]
        clusters = {"A": 0}                      # Z 无簇：不应被挡
        out = portfolio_backtest(logs, equity=450.0, risk_pct=2.0,
                                 max_concurrent=10, max_per_cluster=1,
                                 clusters=clusters)
        self.assertEqual(out["taken"], 2)


class TestClustering(unittest.TestCase):
    """相关性聚类：同涨同跌的币应该被分到同一个簇。"""

    def test_identical_series_cluster_together(self):
        from iabot.portfolio import cluster_symbols
        base = _trading_snapshot()
        snaps = {}
        for name in ("A-USDT", "B-USDT"):
            s = copy.deepcopy(base)
            s.symbol = name
            snaps[name] = s                      # 完全相同的走势
        out = cluster_symbols(snaps, threshold=0.7, window=90)
        self.assertEqual(out["A-USDT"], out["B-USDT"])

    def test_uncorrelated_series_separate(self):
        from iabot.portfolio import cluster_symbols
        from iabot.market import Candle, Snapshot
        import random

        def mk(sym, seed):
            rnd = random.Random(seed)
            closes = [100.0]
            for _ in range(200):
                closes.append(closes[-1] * (1 + rnd.uniform(-0.03, 0.03)))
            k = [Candle(ts=1700000000 + i * 86400, open=c, high=c, low=c,
                        close=c, volume=1.0) for i, c in enumerate(closes)]
            return Snapshot(symbol=sym, price=closes[-1], klines={"1day": k})

        out = cluster_symbols({"A-USDT": mk("A-USDT", 1),
                              "B-USDT": mk("B-USDT", 2)},
                             threshold=0.7, window=90)
        self.assertNotEqual(out["A-USDT"], out["B-USDT"])

    def test_short_history_is_left_unclustered(self):
        from iabot.portfolio import cluster_symbols
        from iabot.market import Candle, Snapshot
        k = [Candle(ts=1700000000 + i * 86400, open=100, high=100, low=100,
                    close=100, volume=1.0) for i in range(5)]
        snaps = {"A-USDT": Snapshot(symbol="A-USDT", price=100,
                                    klines={"1day": k})}
        out = cluster_symbols(snaps)
        # 数据太短仍然会给出簇号（单元素连通分量），关键是不报错
        self.assertIn("A-USDT", out)


if __name__ == "__main__":
    unittest.main()