"""多时间框架（timeframe）的单元测试。

核心承诺（必须锁死）：
1. 不选 timeframe 时，模式行为与旧版**逐字节一致**（因子周期、止损 ATR 周期、
   级别回看全部走写死路径）——所有已回测校准的结论不允许被静默改变。
2. 选了 timeframe 时，整条链围绕该周期重定标：
   - 止损 ATR 周期 = 基础周期（plan.market.atr_period）
   - 主趋势/波动率/成交量因子用基础周期的 K 线
   - 次级趋势用上一级周期
   - 止损区间/入场带宽按 sqrt 频率比缩放
"""

import copy
import math
import unittest

from iabot.config import DEFAULTS
from iabot.market import period_seconds
from iabot.signals import (FACTOR_DEFAULTS, MODES, TIMEFRAMES, build_plan,
                           resolve_tf_spec, tf_child, tf_parent)

# 复用 test_signals 的合成行情夹具（保证各周期同向，否则趋势因子会互相打架）
from tests.test_signals import DOWN_SWING, UP, cfg_with, make_snapshot


def make_snapshot_tf(*args, **kwargs):
    """带扩展周期的快照：夹具默认只有 1min..1day，这里补 4hour/12hour/1week。"""
    snap = make_snapshot(*args, **kwargs)
    return snap


class TestResolveTfSpec(unittest.TestCase):
    """resolve_tf_spec：模式字典与因子周期 spec 的解析规则。"""

    def test_no_timeframe_returns_legacy(self):
        """不选周期：返回原模式字典 + spec=None（旧路径）。"""
        for mode in MODES:
            m, spec = resolve_tf_spec(mode, None)
            self.assertIsNone(spec, f"{mode} 无周期时 spec 必须为 None")
            self.assertIs(m, MODES[mode], f"{mode} 无周期时必须返回原模式字典")

    def test_native_timeframe_equals_legacy(self):
        """选了模式默认基础周期（swing=1day）：与不选完全一致。"""
        m0, spec0 = resolve_tf_spec("swing", None)
        m1, spec1 = resolve_tf_spec("swing", "1day")
        self.assertIsNone(spec0)
        self.assertIsNone(spec1)
        self.assertEqual(m1["atr_period"], m0["atr_period"])

    def test_invalid_timeframe_falls_back(self):
        m, spec = resolve_tf_spec("swing", "not-a-period")
        self.assertIsNone(spec)
        self.assertEqual(m["atr_period"], MODES["swing"]["atr_period"])

    def test_rebase_swaps_atr_period(self):
        m, spec = resolve_tf_spec("swing", "4hour")
        self.assertIsNotNone(spec)
        self.assertEqual(m["atr_period"], "4hour")
        self.assertEqual(spec["tf"], "4hour")
        self.assertEqual(spec["trend_main"], "4hour")
        self.assertEqual(spec["volatility"], "4hour")
        self.assertEqual(spec["volume"], "4hour")
        # 上一级周期做次级趋势/结构确认
        self.assertEqual(spec["trend_confirm"], tf_parent("4hour"))
        self.assertEqual(spec["structure"], "4hour")
        # 目标位/级别回看围绕基础周期 + 上一级
        self.assertIn("4hour", m["level_lookback"])
        self.assertIn(tf_parent("4hour"), m["level_lookback"])

    def test_stop_range_scales_by_sqrt(self):
        """止损区间按 sqrt(周期秒数比) 缩放（ATR 的随机游走近似）。"""
        m, spec = resolve_tf_spec("swing", "4hour")
        base_lo, base_hi = MODES["swing"]["stop_pct_range"]
        f = math.sqrt(period_seconds("4hour") / period_seconds("1day"))
        self.assertAlmostEqual(m["stop_pct_range"][0], base_lo * f, places=4)
        self.assertAlmostEqual(m["stop_pct_range"][1], base_hi * f, places=4)

    def test_reversion_never_rebased(self):
        """reversion 是独立通道（判据写死在日线），不参与重定标。"""
        for tf in TIMEFRAMES:
            m, spec = resolve_tf_spec("reversion", tf)
            self.assertIsNone(spec, f"reversion + {tf} 必须走原口径")
            self.assertEqual(m["atr_period"], MODES["reversion"]["atr_period"])

    def test_tf_chain_neighbors(self):
        self.assertEqual(tf_parent("4hour"), "12hour")
        self.assertEqual(tf_child("4hour"), "60min")
        self.assertEqual(tf_parent("1mon"), "1mon")   # 顶端封口
        self.assertEqual(tf_child("5min"), "5min")    # 底端封口


class TestBuildPlanTf(unittest.TestCase):
    """build_plan 的周期重定标端到端行为。"""

    def _snap(self, scenario=UP):
        return make_snapshot_tf(**scenario)

    def test_default_matches_legacy_byte_for_byte(self):
        """timeframe=None 的计划与旧调用完全一致（回归锁）。

        剔除随时间变化的字段（generated_at 每次调用都不同），其余必须逐字段相等。
        """
        for mode in ("swing", "intraday"):
            with self.subTest(mode=mode):
                snap = self._snap()
                p_legacy = build_plan(snap, cfg_with(), mode=mode)
                snap2 = self._snap()
                p_default = build_plan(snap2, cfg_with(), mode=mode, timeframe=None)
                d_a = p_legacy.to_dict()
                d_b = p_default.to_dict()
                for v in (d_a, d_b):
                    v.pop("generated_at", None)
                    v["market"] = {k: x for k, x in (v.get("market") or {}).items()
                                   if k != "timeframe"}
                self.assertEqual(d_a, d_b)
                self.assertIsNone(p_default.timeframe)
                self.assertEqual(p_default.market["atr_period"],
                                 MODES[mode]["atr_period"])

    def test_cfg_timeframe_used_when_param_none(self):
        """参数为 None 时读 config 的 analysis.timeframe。"""
        cfg = cfg_with()
        cfg["analysis"]["timeframe"] = "4hour"
        # 夹具没有 4hour K 线 -> 重定标后 ATR 数据缺失 -> 观望 + no_atr 闸门，
        # 但 market.timeframe 必须标注成 4hour（证明配置生效了）
        p = build_plan(self._snap(), cfg, mode="swing")
        self.assertEqual(p.market.get("timeframe"), "4hour")

    def test_native_tf_stays_legacy(self):
        """swing + 1day（= 模式默认基础周期）= 旧口径，不进实验分支。"""
        p = build_plan(self._snap(), cfg_with(), mode="swing", timeframe="1day")
        self.assertIsNone(p.timeframe)
        self.assertEqual(p.market["atr_period"], "1day")

    def test_rebased_atr_period_follows_tf(self):
        p = build_plan(self._snap(), cfg_with(), mode="swing", timeframe="4hour")
        self.assertEqual(p.market["timeframe"], "4hour")
        self.assertEqual(p.market["atr_period"], "4hour")
        # 夹具没有 4hour K 线，应该明确报 no_atr，而不是拿日线 ATR 冒充
        self.assertTrue(p.gate.get("blocked"))
        self.assertEqual(p.gate.get("reason"), "no_atr")

    def test_rebased_factors_use_new_periods(self):
        """补上 4hour K 线后：主趋势因子应报 [4小时]，不再用日线。"""
        snap = self._snap()
        # 合成 4hour K 线（夹具的日线序列降采样 1:6）
        k1d = snap.klines.get("1day") or []
        k4h = [c for i, c in enumerate(k1d) if i % 6 == 0] or k1d
        snap.klines["4hour"] = list(k4h)
        snap.klines["12hour"] = [c for i, c in enumerate(k1d) if i % 12 == 0] or k1d
        p = build_plan(snap, cfg_with(), mode="swing", timeframe="4hour")
        self.assertEqual(p.market["atr_period"], "4hour")
        by = {f.name: f for f in p.factors}
        self.assertIn("4小时", by["trend_daily"].detail)
        self.assertNotIn("日线数据不足", by["trend_daily"].detail)
        # 波动率/成交量换到基础周期上打分
        self.assertIn("4小时", by["volatility"].detail)
        # 次级趋势用上一级周期（12hour）
        self.assertIn("12小时", by["trend_intraday"].detail)

    def test_plan_records_timeframe(self):
        p = build_plan(self._snap(), cfg_with(), mode="swing", timeframe="4hour")
        d = p.to_dict()
        self.assertEqual(d["timeframe"], "4hour")
        self.assertEqual(d["market"]["timeframe"], "4hour")

    def test_tf_note_attached(self):
        snap = self._snap()
        k1d = snap.klines.get("1day") or []
        snap.klines["4hour"] = [c for i, c in enumerate(k1d) if i % 6 == 0] or k1d
        snap.klines["12hour"] = [c for i, c in enumerate(k1d) if i % 12 == 0] or k1d
        p = build_plan(snap, cfg_with(), mode="swing", timeframe="4hour")
        self.assertTrue(any("多时间框架" in n for n in p.notes))

    def test_tf_weights_override_per_period(self):
        """tf_weights[tf] 覆盖该周期的因子权重（其他周期不受影响）。"""
        cfg = cfg_with()
        cfg["analysis"]["tf_weights"] = {"4hour": {"volatility": 0.5}}
        snap = self._snap()
        k1d = snap.klines.get("1day") or []
        snap.klines["4hour"] = [c for i, c in enumerate(k1d) if i % 6 == 0] or k1d
        snap.klines["12hour"] = [c for i, c in enumerate(k1d) if i % 12 == 0] or k1d

        # 4hour 周期：volatility 权重被覆盖为 0.5
        p4 = build_plan(snap, cfg, mode="swing", timeframe="4hour")
        w4 = {f.name: f.weight for f in p4.factors}
        self.assertEqual(w4["volatility"], 0.5)
        # 未配置 tf（默认日线口径）：volatility 仍是内置默认，不受 tf_weights 影响
        p0 = build_plan(snap, cfg_with(), mode="swing", timeframe=None)
        w0 = {f.name: f.weight for f in p0.factors}
        self.assertEqual(w0["volatility"], FACTOR_DEFAULTS["volatility"])

    def test_tf_risk_override_per_period(self):
        """tf_risk[tf] 覆盖该周期的多空风险倍数（其他周期不受影响）。"""
        cfg = cfg_with()
        cfg["analysis"]["tf_risk"] = {"4hour": {"long": 0.7, "short": 1.4}}
        snap = self._snap()
        k1d = snap.klines.get("1day") or []
        snap.klines["4hour"] = [c for i, c in enumerate(k1d) if i % 6 == 0] or k1d
        snap.klines["12hour"] = [c for i, c in enumerate(k1d) if i % 12 == 0] or k1d

        p4 = build_plan(snap, cfg, mode="swing", timeframe="4hour")
        self.assertEqual(p4.risk_mult_long, 0.7)
        self.assertEqual(p4.risk_mult_short, 1.4)
        # 默认口径不受 tf_risk 影响
        p0 = build_plan(snap, cfg_with(), mode="swing", timeframe=None)
        self.assertEqual(p0.risk_mult_long, 1.0)
        self.assertEqual(p0.risk_mult_short, 1.0)


if __name__ == "__main__":
    unittest.main()
