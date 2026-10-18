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


if __name__ == "__main__":
    unittest.main()