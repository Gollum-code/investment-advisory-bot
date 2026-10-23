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
from .portfolio import cluster_symbols, portfolio_backtest
from .walkforward import aggregate, walk_forward

# 集中度警戒线：正 R 的贡献里，前 N 个币占比超过这个比例就报"收益集中"
CONCENTRATION_TOP_N = 2
CONCENTRATION_WARN = 0.5
# 排序能力检验：按评分分几档，看分数高的那批是不是真的更赚。
SCORE_BUCKETS = 3


def score_ranking(trade_logs: list[dict], *,
                  buckets: int = SCORE_BUCKETS) -> dict:
    """按 |评分| 分档统计每档的期望 R。

    回答一个更根本的问题：**评分到底有没有排序能力？**
    如果高分档和低分档的期望差不多，那阈值 30 就是拍脑袋定的——
    调整它不会有任何区别。反之如果单调，说明分数确实在挑单，阈值值得调。
    """
    rows = [t for t in trade_logs
            if t.get("r") is not None and t.get("score") is not None
            and t.get("outcome") != "no_fill"]
    if len(rows) < buckets * 5:
        return {"buckets": [], "usable": False,
                "reason": f"样本 {len(rows)} 笔，少于 {buckets*5}，分档没意义"}
    rows.sort(key=lambda t: abs(float(t["score"])))
    n = len(rows)
    size = n // buckets
    out: list[dict] = []
    for i in range(buckets):
        chunk = rows[i * size:] if i == buckets - 1 else rows[i * size:(i + 1) * size]
        rs = [float(t["r"]) for t in chunk]
        lo = min(abs(float(t["score"])) for t in chunk)
        hi = max(abs(float(t["score"])) for t in chunk)
        out.append({
            "label": f"低分{1 + i}" if i < buckets - 1 else "高分",
            "score_abs_min": round(lo, 1),
            "score_abs_max": round(hi, 1),
            "trades": len(rs),
            "expectancy_r": round(sum(rs) / len(rs), 4),
        })
    low = out[0]["expectancy_r"]
    high = out[-1]["expectancy_r"]
    gap = high - low
    # 单调递增才算真的有排序能力；只有两头好、中间差，说明是噪声
    monotone = all(out[i]["expectancy_r"] <= out[i + 1]["expectancy_r"]
                   for i in range(len(out) - 1))
    return {
        "buckets": out,
        "usable": True,
        "gap_r": round(gap, 4),
        "monotone": bool(monotone),
        "verdict": ("分数有排序能力" if monotone and gap > 0.05 else
                    "排序能力不明显，阈值调整大概无效"),
    }


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
            "_log": list(res.trade_log or []),
            "gate_counts": dict(res.gate_counts or {}),
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
    logs: list[dict] = []
    for r in rows:
        if not r["insufficient_data"]:
            rs.extend(r["_rs"])
            logs.extend(r["_log"])
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
        # 合并后的交易明细：给组合级模拟用（并发上限、资金约束）
        "trade_logs": logs,
    }


def factor_attribution(trade_logs: list[dict], *, top_n: int = 3) -> list[dict]:
    """按"这笔单的主导因子"分组统计期望 R。

    用来解释评分排序的结果：如果高分档集中在某个因子上，而那个因子单独看
    期望是负的，说明这个因子在拉扯分数而不是在赚钱。
    """
    groups: dict[str, list[float]] = {}
    for t in trade_logs:
        name = t.get("top_factor")
        if not name or t.get("r") is None:
            continue
        groups.setdefault(str(name), []).append(float(t["r"]))
    rows = [
        {"factor": name, "trades": len(rs),
         "expectancy_r": round(sum(rs) / len(rs), 4)}
        for name, rs in groups.items()
    ]
    rows.sort(key=lambda r: -r["trades"])
    return rows[:top_n]


def direction_stats(trade_logs: list[dict]) -> dict:
    """多/空分开统计：完整历史上空单在多空两个方向都赚钱，多单只在上涨市赚。
    这里把它落到"这次回测的样本上"，避免只靠历史结论拍板。
    """
    out: dict[str, dict] = {}
    for side in ("long", "short"):
        rs = [float(t["r"]) for t in trade_logs
              if t.get("direction") == side and t.get("r") is not None]
        if rs:
            out[side] = {
                "trades": len(rs),
                "expectancy_r": round(sum(rs) / len(rs), 4),
                "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 4),
            }
    return out


GATE_CN = {
    "score_below_threshold": "评分低于阈值",
    "no_target": "无可用目标位",
    "target_too_far": "目标位太远",
    "low_rr": "盈亏比不足",
    "trend_veto": "逆日线趋势",
    "score_band": "评分带",
    "liquidity": "流动性",
    "subordinate": "从属模式",
    "no_fill": "挂单未成交",
    "no_price": "拿不到价格",
    "no_atr": "缺少 ATR",
}


def gate_funnel(rows: list[dict]) -> dict[str, int]:
    """汇总各标的的闸门漏斗计数（按数量降序）。"""
    tot: dict[str, int] = {}
    for r in rows:
        for k, v in (r.get("gate_counts") or {}).items():
            tot[k] = tot.get(k, 0) + int(v)
    return dict(sorted(tot.items(), key=lambda kv: -kv[1]))


def validate(snapshots: dict[str, Snapshot], cfg: dict, *,
             mode: str = "swing", folds: int = 5, step: int = 10,
             warmup: int = 60, max_concurrent: int = 5,
             max_heat_pct: float | None = None,
             max_per_cluster: int | None = None,
             corr_threshold: float = 0.7,
             risk_mult_long: float | None = None,
             risk_mult_short: float | None = None) -> dict:
    """完整验证：整段回测 + 折间一致性 + 集中度 + 组合模拟，一起给结论。

    整段正期望只说明"历史上总体是赚的"；折间一致性说明"不是靠某一段行情"；
    集中度说明"不是靠某几个币"；组合模拟说明"资金和并发够不够"。
    四样都过才算真的能用。
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
    acc = cfg.get("account") or {}
    clusters = cluster_symbols(snapshots, threshold=corr_threshold)
    an = cfg.get("analysis") or {}
    ml = float(risk_mult_long if risk_mult_long is not None
               else (an.get("risk_mult_long") or 1.0))
    ms = float(risk_mult_short if risk_mult_short is not None
               else (an.get("risk_mult_short") or 1.0))
    port = portfolio_backtest(
        full.get("trade_logs") or [],
        equity=float(acc.get("equity_usdt") or 450.0),
        risk_pct=float(acc.get("risk_pct_per_trade") or 2.0),
        max_concurrent=max_concurrent,
        max_heat_pct=max_heat_pct,
        clusters=clusters,
        max_per_cluster=max_per_cluster,
        direction_mults={"long": ml, "short": ms})
    conc = full["concentration"]
    p = full["pooled"]
    ranking = score_ranking(full.get("trade_logs") or [])
    dirs = direction_stats(full.get("trade_logs") or [])
    attrib = factor_attribution(full.get("trade_logs") or [])
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
    if ranking.get("usable") and ranking["verdict"] != "分数有排序能力":
        notes.append(f"评分排序能力不明显（低分档 {ranking['buckets'][0]['expectancy_r']:+.3f}R"
                     f" vs 高分档 {ranking['buckets'][-1]['expectancy_r']:+.3f}R）："
                     f"调阈值大概不会有用，问题在因子本身")
    if port["skipped_capacity"]:
        pct = port["skipped_capacity"] / max(1, port["signals"]) * 100
        notes.append(f"并发上限 {max_concurrent} 挡掉了 {port['skipped_capacity']} "
                     f"笔（{pct:.0f}%）：单笔期望不等于组合能拿到的收益")
    if port.get("skipped_heat"):
        notes.append(f"组合热度上限挡掉 {port['skipped_heat']} 笔："
                     f"未平仓仓位的风险总额超出上限")
    if port.get("skipped_cluster"):
        notes.append(f"相关性簇限制挡掉 {port['skipped_cluster']} 笔："
                     f"同一簇里最多同时持有 {max_per_cluster} 个仓")
    # 闸门漏斗：新加一道闸门后最需要的诊断。没有它，"我加了上限"和
    # "我没信号了"分不清——只看到交易数掉了，不知道掉在哪一步。
    funnel = gate_funnel(full.get("rows") or [])
    for name, cnt in funnel.items():
        label = GATE_CN.get(name, name)
        notes.append(f"闸门[{label}] 挡掉 {cnt} 个检查点")
    if funnel.get("score_below_threshold", 0) > funnel.get("no_target", 0):
        notes.append("大部分检查点连方向都没评出来（评分低于阈值）："
                     "瓶颈在因子，不在目标位")
    if funnel.get("no_fill", 0) > max(1, p["trades"]):
        notes.append(f"挂单未成交 {funnel['no_fill']} 次，多于成交笔数："
                     f"入场区间定得太远，价格根本没回去过")
    if port["max_drawdown_pct"] >= 30:
        notes.append(f"组合最大回撤 {port['max_drawdown_pct']:.0f}%："
                     f"单笔风险 {port['expectancy_r']:+.3f}R 掩盖了这个尾部")
    # 挡掉的单子如果明显更好，说明上限限太紧了（在拿收益换安全）
    skipped_e = port.get("skipped_expectancy_r")
    taken_e = port.get("taken_expectancy_r")
    if skipped_e is not None and taken_e is not None:
        gap = skipped_e - taken_e
        if gap > 0.05:
            notes.append(f"被挡掉的单子期望 {skipped_e:+.3f}R，明显高于做上的 "
                         f"{taken_e:+.3f}R：上限限太紧了，正在拿收益换安全")
        elif gap < -0.05:
            notes.append(f"被挡掉的单子期望 {skipped_e:+.3f}R，低于做上的 "
                         f"{taken_e:+.3f}R：上限筛掉了较差的一批，挡得有道理")
    return {
        "mode": mode,
        "full": full,
        "walkforward": wf,
        "concentration": conc,
        "walkforward_concentration": wf_conc,
        "clusters": clusters,
        "score_ranking": ranking,
        "directions": dirs,
        "factor_attribution": attrib,
        "gates": funnel,
        "portfolio": {k: v for k, v in port.items() if k != "equity_curve"},
        "notes": notes,
        "verdict": "通过" if not notes else "有保留",
    }
