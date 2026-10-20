"""组合级验证：跨币汇总 + 收益集中度检查。

两个单币回测都回答不了的问题：

1) **这些币合起来是正的吗？** 单币一个折常常只有十几笔，逐币看结论太不稳。
   跨币合并所有交易后的期望与区间才是真正该看的数。

2) **这个正期望是靠几个币撑起来的？** reversion 就是这么骗人的：300 根窗口下
   +0.367R 看着不错，其中 52% 的 R 来自两个币。换个币种就没了。
   集中度检查就是专门抓这种形态：正 R 的贡献如果集中在少数标的上，
   那它不是"策略有效"，是"那几个币那段时间有效"。
"""

from __future__ import annotations

from .backtest import _bootstrap_mean_ci, backtest
from .market import Snapshot
from .walkforward import aggregate, walk_forward

# 集中度警戒线：正 R 的贡献里，前 N 个币占比超过这个比例就报"收益集中"
CONCENTRATION_TOP_N = 2
CONCENTRATION_WARN = 0.5


def _symbol_rows(snapshots: dict[str, Snapshot], cfg: dict, *,
                 mode: str, step: int, warmup: int, max_points: int) -> list[dict]:
    rows: list[dict] = []
    for sym, snap in snapshots.items():
        res = backtest(snap, cfg, mode=mode, step=step, warmup=warmup,
                       max_points=max_points)
        rows.append({
            "symbol": sym,
            "trades": res.trades,
            "wins": res.wins,
            "losses": res.losses,
            "total_r": round(res.total_r, 3),
            "expectancy_r": round(res.expectancy_r, 4),
            "expectancy_ci": ([round(x, 4) for x in res.expectancy_ci]
                              if res.expectancy_ci else None),
            "win_rate": round(res.win_rate, 4),
            "insufficient_data": res.insufficient_data,
            "missing_periods": list(res.missing_periods),
            "_rs": list(res.trade_rs or []),
        })
    return rows


def _concentration_pairs(pairs: list[tuple[str, float]]) -> dict:
    """正 R 的贡献有多集中。

    只看赚钱的标的：如果正 R 里有一大半来自一两个标的，那这个策略基本等价于
    "押注那一两个标的"。
    """
    pos = [(s, v) for s, v in pairs if v > 0]
    total_pos = sum(v for _, v in pos)
    if not pos or total_pos <= 0:
        return {"positive_symbols": 0, "top_share": 0.0,
                "top_symbols": [], "warning": ""}
    pos.sort(key=lambda x: -x[1])
    top = pos[:CONCENTRATION_TOP_N]
    top_share = sum(v for _, v in top) / total_pos
    warn = ""
    if top_share >= CONCENTRATION_WARN and len(pos) > CONCENTRATION_TOP_N:
        warn = (f"收益集中：{len(pos)} 个赚钱的标的里，前 {CONCENTRATION_TOP_N} 个"
                f"（{'、'.join(s for s, _ in top)}）贡献了正 R 的 "
                f"{top_share * 100:.0f}%")
    return {
        "positive_symbols": len(pos),
        "total_positive_r": round(total_pos, 2),
        "top_share": round(top_share, 4),
        "top_symbols": [s for s, _ in top],
        "warning": warn,
    }


def _concentration(rows: list[dict]) -> dict:
    return _concentration_pairs([(r["symbol"], r["total_r"]) for r in rows])


def multi_backtest(snapshots: dict[str, Snapshot], cfg: dict, *,
                   mode: str = "swing", step: int = 10, warmup: int = 60,
                   max_points: int = 100000) -> dict:
    """对多个合约跑同一套规则，合并出整体期望，并检查收益是否集中在少数币上。"""
    rows = _symbol_rows(snapshots, cfg, mode=mode, step=step, warmup=warmup,
                        max_points=max_points)
    rs: list[float] = []
    for r in rows:
        if not r["insufficient_data"]:
            rs.extend(r["_rs"])
    exp = (sum(rs) / len(rs)) if rs else 0.0
    ci = _bootstrap_mean_ci(rs)
    wins = sum(r["wins"] for r in rows)
    losses = sum(r["losses"] for r in rows)
    resolved = wins + losses
    usable = [r for r in rows if not r["insufficient_data"]]
    public = [{k: v for k, v in r.items() if k != "_rs"} for r in rows]
    return {
        "mode": mode,
        "symbols": len(rows),
        "usable_symbols": len(usable),
        "rows": public,
        "pooled": {
            "trades": len(rs),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / resolved, 4) if resolved else 0.0,
            "expectancy_r": round(exp, 4),
            "expectancy_ci": ([round(x, 4) for x in ci] if ci else None),
            "total_r": round(sum(rs), 2),
        },
        "concentration": _concentration(usable),
    }


def validate(snapshots: dict[str, Snapshot], cfg: dict, *,
             mode: str = "swing", folds: int = 5, step: int = 10,
             warmup: int = 60) -> dict:
    """完整验证：整段回测 + 折间一致性 + 集中度，三件事一起给结论。

    整段正期望只说明"历史上总体是赚的"；折间一致性说明"不是靠某一段行情"；
    集中度说明"不是靠某几个币"。三样都过才算真的能用。
    """
    full = multi_backtest(snapshots, cfg, mode=mode, step=step, warmup=warmup)
    wf_rows = []
    for _sym, snap in snapshots.items():
        wf_rows.append(walk_forward(snap, cfg, mode=mode, folds=folds,
                                    step=step, warmup=warmup))
    wf = aggregate(wf_rows)
    wf_conc = _concentration_pairs([
        (r.get("symbol") or "?", (r.get("pooled") or {}).get("total_r") or 0.0)
        for r in wf_rows if r.get("ok")
    ])
    conc = full["concentration"]
    p = full["pooled"]
    notes: list[str] = []
    if not p["expectancy_ci"]:
        notes.append("样本不足，给不出区间")
    elif p["expectancy_ci"][0] <= 0 <= p["expectancy_ci"][1]:
        notes.append("整段期望的 95% 区间跨 0：分不清 edge 和噪声")
    if wf["folds_total"]:
        if wf["folds_positive_frac"] < 0.6:
            notes.append(f"只有 {wf['folds_positive_frac']*100:.0f}% 的时间窗口为正，"
                         f"edge 不稳定")
    else:
        notes.append("没有可用的折，无法检验时间一致性")
    if conc.get("warning"):
        notes.append(conc["warning"])
    if wf_conc.get("warning"):
        notes.append("折间口径下" + wf_conc["warning"])
    return {
        "mode": mode,
        "full": full,
        "walkforward": wf,
        "concentration": conc,
        "walkforward_concentration": wf_conc,
        "notes": notes,
        "verdict": "通过" if not notes else "有保留",
    }
