"""按基础周期扫描候选因子权重（调参工具，纯标准库零依赖）。

用法:
    python -m tools.sweep_tf_weights 4hour     # 需先跑 validate --tf 4hour 生成快照缓存

它读取 snapcache 里该周期的快照（ttl=-1 永不过期，权重比较是相对排序，
行情新鲜度无关紧要），对每组候选权重跑 multi_backtest，输出期望/胜率/区间。

第一轮实测结论（2026-11，4hour/12hour 各 5 组候选）：
组间期望差异全部落在 95% 置信区间重叠带内——微调权重不是有效杠杆。
此工具的价值是"每个周期能独立配参做 A/B"，不是"调了就更准"。
真正有统计分辨度的差异在周期本身（4hour 显著优于 12hour/日线）。
"""
import sys
import time

from iabot import snapcache
from iabot.__main__ import VALIDATE_SYMBOLS
from iabot.config import DEFAULT_MODE, DEFAULTS, load_config
from iabot.validation import multi_backtest

CANDIDATES = {
    "base(日线校准权重)": {},
    "提波动率+成交量": {
        "volatility": 0.34, "volume": 0.22,
        "trend_daily": 0.08, "trend_intraday": 0.03, "momentum": 0.05,
        "structure": 0.04, "vwap": 0.02, "funding": 0.08,
        "open_interest": 0.05, "elite": 0.05, "orderbook": 0.04,
    },
    "削趋势类、更纯的状态模型": {
        "volatility": 0.34, "volume": 0.22,
        "trend_daily": 0.04, "trend_intraday": 0.02, "momentum": 0.04,
        "structure": 0.03, "vwap": 0.02, "funding": 0.09,
        "open_interest": 0.06, "elite": 0.06, "orderbook": 0.04,
    },
    "空头偏置(提空头确认因子)": {
        "volatility": 0.30, "volume": 0.18,
        "trend_daily": 0.10, "trend_intraday": 0.04, "momentum": 0.06,
        "structure": 0.05, "vwap": 0.03, "funding": 0.10,
        "open_interest": 0.07, "elite": 0.07, "orderbook": 0.04,
    },
    "重成交量+资金费(反过热)": {
        "volatility": 0.28, "volume": 0.24,
        "trend_daily": 0.06, "trend_intraday": 0.03, "momentum": 0.05,
        "structure": 0.04, "vwap": 0.02, "funding": 0.12,
        "open_interest": 0.06, "elite": 0.05, "orderbook": 0.04,
    },
}


def main() -> int:
    tf = (sys.argv[1] if len(sys.argv) > 1 else "4hour").strip()
    cfg = load_config()
    snaps = {}
    for s in VALIDATE_SYMBOLS:
        # 权重扫描是"同一批快照上做相对比较"，行情新鲜度无关紧要；
        # ttl_sec=-1 = 永不过期，直接复用 validate 留下的旧缓存。
        got = snapcache.load(s, variant=tf, ttl_sec=-1)
        if got is not None and got.klines.get(tf):
            snaps[got.symbol] = got
    if len(snaps) < 3:
        print(f"[error] 快照不足：variant={tf} 只有 {len(snaps)} 个标的。"
              f"请先跑 validate --tf {tf} 生成缓存。")
        return 1

    print(f"基础周期 {tf} · {len(snaps)} 个标的 · 候选权重扫描\n")
    rows = []
    for name, w in CANDIDATES.items():
        cc = dict(DEFAULTS)
        cc["analysis"] = {**cfg.get("analysis", {}), "factor_weights": w}
        cc["account"] = cfg.get("account") or {}
        t0 = time.time()
        r = multi_backtest(snaps, cc, mode=DEFAULT_MODE, step=10, warmup=60)
        p = r["pooled"]
        rows.append((name, r))
        print(f"  {name:<16} 笔 {p['trades']:>5}  胜率 {p['win_rate']*100:5.1f}%  "
              f"期望 {p['expectancy_r']:+.4f}R  "
              f"区间 [{p['expectancy_ci'][0]:+.3f},{p['expectancy_ci'][1]:+.3f}]"
              if p.get("expectancy_ci") else
              f"  {name:<16} 笔 {p['trades']:>5}  胜率 {p['win_rate']*100:5.1f}%  "
              f"期望 {p['expectancy_r']:+.4f}R  样本不足")
        print(f"      ({time.time()-t0:.0f}s)")

    rows.sort(key=lambda x: -x[1]["pooled"]["expectancy_r"])
    print("\n  按期望排序：")
    for name, r in rows:
        p = r["pooled"]
        print(f"    {name:<16} {p['expectancy_r']:+.4f}R / {p['trades']} 笔")
    return 0


if __name__ == "__main__":
    sys.exit(main())