"""滚动的样本外验证：把历史切成若干折，逐折回测，看 edge 是否稳定。

为什么需要它：到现在所有"结论"都是在一整段历史上跑出来的点估计。即使加了
置信区间，也只能说明"整段样本里这个数是不是运气"，回答不了另一个更要命的
问题——**这个 edge 是均匀分布的，还是只来自其中一两段行情？**

reversion 就是后者：300 根窗口下 +0.367R 看着不错，但那 52% 的 R 来自两个币、
且只有 2025-2026 为正。如果当时按时间切折看，一眼就能看穿。

本模块的策略没有"训练参数"，所以这里的 walk-forward 是**折间一致性检验**：
同一套固定规则在各折上都必须成立。任何"只在一折好"的结论都该被怀疑。

实现要点：折与折之间只切换**建仓点的区间**，绝不裁剪历史。第一版按时间窗口
把快照切小，结果每折开头的日线历史只剩十几根，波动率分位/SMA200 这类因子
全部退化成 0 分，四折全是 0 笔交易——那不是策略的问题，是把数据切坏了。
"""

from __future__ import annotations

from .backtest import _bootstrap_mean_ci, backtest
from .market import Snapshot


def walk_forward(base: Snapshot, cfg: dict, *, mode: str = "swing",
                 folds: int = 5, step: int = 10, warmup: int = 60,
                 max_points: int = 100000) -> dict:
    """把主周期 K 线按时间均分成 folds 段，逐段独立取建仓点回测。

    每个折都保留该折之前的全部历史（因子看到的信息与真实当时一致），
    只是不在这段之外建仓——所以每折的结果都是真正的样本外。
    """
    main_period = "15min" if mode == "intraday" else "1day"
    main = base.klines.get(main_period) or []
    n = len(main)
    folds = max(2, int(folds))
    min_fold = warmup + step + 30
    if n < folds * min_fold:
        return {"ok": False, "reason": "样本不足以切这么多折",
                "bars": n, "folds": []}

    bounds = [int(round(i * n / folds)) for i in range(folds + 1)]
    rows: list[dict] = []
    pooled: list[float] = []
    for k in range(folds):
        lo_i, hi_i = bounds[k], bounds[k + 1]
        if hi_i - lo_i < min_fold:
            continue
        res = backtest(base, cfg, mode=mode, step=step, warmup=warmup,
                       max_points=max_points,
                       start_index=lo_i, end_index=hi_i)
        usable = (not res.insufficient_data) and res.trades > 0
        if usable:
            pooled.extend(res.trade_rs or [])
        rows.append({
            "index": k,
            "start_ts": main[lo_i].ts,
            "end_ts": main[hi_i - 1].ts,
            "bars": hi_i - lo_i,
            "trades": res.trades,
            "expectancy_r": round(res.expectancy_r, 4),
            "expectancy_ci": ([round(x, 4) for x in res.expectancy_ci]
                              if res.expectancy_ci else None),
            "win_rate": round(res.win_rate, 4),
            "total_r": round(res.total_r, 3),
            "insufficient_data": res.insufficient_data,
            "usable": usable,
        })

    usable_rows = [r for r in rows if r["usable"]]
    pos = sum(1 for r in usable_rows if r["expectancy_r"] > 0)
    pooled_n = len(pooled)
    pooled_exp = (sum(pooled) / pooled_n) if pooled_n else 0.0
    pooled_ci = _bootstrap_mean_ci(pooled)
    frac_pos = (pos / len(usable_rows)) if usable_rows else 0.0

    return {
        "ok": bool(usable_rows),
        "bars": n,
        "folds": rows,
        "usable_folds": len(usable_rows),
        "positive_folds": pos,
        "positive_fold_frac": round(frac_pos, 3),
        "pooled": {
            "trades": pooled_n,
            "expectancy_r": round(pooled_exp, 4),
            "expectancy_ci": ([round(x, 4) for x in pooled_ci]
                              if pooled_ci else None),
            # 合并后的每笔 R：跨币汇总时要用（单个币的样本太小）
            "rs": pooled,
        },
        "verdict": _verdict(pooled_exp, pooled_ci, frac_pos, usable_rows),
    }


def aggregate(results: list[dict]) -> dict:
    """把多个币的 walk-forward 结果合并成一个整体判断。

    单币样本太小（一个折常常只有十几笔），逐币看结论会很不稳；真正该看的
    是"所有币、所有折合起来"——有多少个时间窗口是正的，合并后的期望区间
    跨不跨 0。
    """
    rs: list[float] = []
    folds: list[dict] = []
    for r in results:
        if not r.get("ok"):
            continue
        rs.extend(r.get("pooled", {}).get("rs") or [])
        folds.extend(f for f in r.get("folds", []) if f.get("usable"))
    pos = sum(1 for f in folds if f["expectancy_r"] > 0)
    exp = (sum(rs) / len(rs)) if rs else 0.0
    ci = _bootstrap_mean_ci(rs)
    frac = (pos / len(folds)) if folds else 0.0
    reliable = ci is not None and (ci[0] > 0 or ci[1] < 0)
    if not folds:
        verdict = "没有可用折"
    elif exp > 0 and frac >= 0.7 and reliable:
        verdict = "整体成立：多数折为正，合并区间不跨 0"
    elif exp > 0 and frac >= 0.6:
        verdict = "大体成立，但约三成时间窗口为负，需要更多样本"
    elif exp > 0:
        verdict = "脆弱：正收益集中在少数折"
    else:
        verdict = "整体为负，不要用"
    return {
        "symbols": len([r for r in results if r.get("ok")]),
        "folds_total": len(folds),
        "folds_positive": pos,
        "folds_positive_frac": round(frac, 3),
        "trades": len(rs),
        "expectancy_r": round(exp, 4),
        "expectancy_ci": ([round(x, 4) for x in ci] if ci else None),
        "significant": bool(reliable),
        "verdict": verdict,
    }


def _verdict(exp: float, ci, frac_pos: float, usable: list[dict]) -> str:
    """给一句人话结论。分水岭是"折间是否一致"，不是"总期望是否为正"。"""
    if len(usable) < 2:
        return "可用折不足，无法判断稳定性"
    reliable = ci is not None and (ci[0] > 0 or ci[1] < 0)
    if exp > 0 and frac_pos >= 0.8 and reliable:
        return "稳定：多数折为正，合并区间不跨 0"
    if exp > 0 and frac_pos >= 0.6:
        return "大体成立，但有折为负，需要更多样本"
    if exp > 0:
        return "脆弱：正收益集中在少数折，换一段行情可能就没了"
    return "整段为负，不要用"
