"""影子账户 / A-B 对照：同一批信号，用两套参数各记一份，事后比真实结局。

为什么需要它：回测只能在历史里验证参数。想改默认值（比如 trend_veto 打开、
移动止损打开、多空风险不对称）时，唯一可信的证据是**这段参数在未来的真实
行情里也成立**。做法是把两套参数同时跑，只记录不下单，等过一段时间用真实
K 线判结局，再对比。

记录文件是 JSONL（data/shadow.jsonl），每行一条信号：
    {ts, symbol, variant, mode, plan}
这样不碰信号库的表结构，随时可以删掉重来。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .backtest import _bootstrap_mean_ci
from .config import data_path
from .outcomes import evaluate_outcome
from .signals import build_plan

# 内置对照方案：名字 -> 要覆盖的配置片段
VARIANTS: dict[str, dict] = {
    "base": {},
    "trend_veto": {"analysis": {"trend_veto": True}},
    "trail_1atr": {"analysis": {"exit": {"trail_atr_mult": 1.0}}},
    "be_1r": {"analysis": {"exit": {"breakeven_at_r": 1.0}}},
    "short_heavy": {"analysis": {"risk_mult_long": 0.5,
                                 "risk_mult_short": 1.5}},
    # 多时间框架对照：同一批快照按 4hour 口径再记一份。
    # 组合验证显示 4hour 显著占优（折间 77% vs 日线），但那是历史回测；
    # 要在未来也成立，必须攒前向样本。这个变体就是干这个的。
    "tf4h": {"analysis": {"timeframe": "4hour"}},
}

DEFAULT_PATH = "data/shadow.jsonl"
# 一个方案至少要有这么多"已判定"样本，才值得据此改默认值。
# 30 是统计显著性下限（与 backtest 的 MIN_TRADES_FOR_SIGNIFICANCE 一致），
# 100 是"足够放心"的经验值。
MIN_SAMPLES = 30
READY_SAMPLES = 100


def required_periods(variants: list[str] | None) -> list[str]:
    """这些方案里需要额外抓取的 K 线周期（去重、保持顺序）。

    比如 tf4h 方案重现定标到 4hour，快照必须含 4hour/12hour/60min 的 K 线，
    否则 build_plan 按"数据不足"降级、一条也记不下来。
    默认快照（1min~1day）不含 4hour，所以调用方抓影子快照前先算它。
    """
    from .signals import tf_required_periods
    out: list[str] = []
    for n in variants or []:
        patch = VARIANTS.get(n) or {}
        tf = ((patch.get("analysis") or {}).get("timeframe") or "").strip()
        if not tf:
            continue
        for p in tf_required_periods(tf):
            if p not in out:
                out.append(p)
    return out


def _merge_patch(cfg: dict, patch: dict) -> dict:
    import copy
    out = copy.deepcopy(cfg)
    for section, values in (patch or {}).items():
        if isinstance(values, dict) and isinstance(out.get(section), dict):
            out[section].update(copy.deepcopy(values))
        else:
            out[section] = copy.deepcopy(values)
    return out


def record(snaps: dict, cfg: dict, *, variants: list[str] | None = None,
           mode: str | None = None, path: str | Path | None = None,
           now: float | None = None) -> dict:
    """对每个 (标的 × 方案) 生成计划并追加到 JSONL。

    只记录有方向的计划（观望没有可验证的结局）。返回统计，便于 CLI 汇报。

    去重（analysis 级，读 schedule.shadow.dedupe）：同一个 (标的 × 方案) 若上一条
    记录方向未变、评分也没明显变化、且未超出冷却期，就不重复记。否则会像实测
    那样（5 天 994 条但独立信号只有 25 个），把同一笔信号反复判成同一个结局，
    样本"看着够了"其实全是重复。
    """
    from .config import DEFAULT_MODE
    mode = mode or cfg.get("mode") or DEFAULT_MODE
    names = variants or ["base"]
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        raise ValueError(f"未知方案 {unknown}；可选：{sorted(VARIANTS)}")
    p = Path(path) if path else data_path(DEFAULT_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = float(now if now is not None else time.time())
    ded = _dedupe_config(cfg)
    # 一次读全文件建索引： (symbol, variant) -> 该组合的最后一条记录。
    # 样本量小（数百条），O(n) 一次足够；循环里再逐组合去读就是 O(n²)。
    last_by = {}
    if ded["enabled"]:
        for r in load(p):
            key = (r.get("symbol"), r.get("variant"))
            if last_by.get(key, 0) and (r.get("ts") or 0) <= last_by[key].get("ts", 0):
                continue
            last_by[key] = r
    written = 0
    skipped_wait = 0
    skipped_dup = 0
    by_variant: dict[str, int] = {n: 0 for n in names}
    with p.open("a", encoding="utf-8", newline="\n") as fh:
        for name in names:
            vcfg = _merge_patch(cfg, VARIANTS[name])
            for sym, snap in snaps.items():
                try:
                    plan = build_plan(snap, vcfg, mode=mode)
                except Exception:
                    continue
                if plan.direction not in ("long", "short"):
                    skipped_wait += 1
                    continue
                # 去重：与同 (标的 × 方案) 的上一条记录比较，
                # 方向/评分没明显变化且在冷却期内 => 重复采样，跳过
                if ded["enabled"]:
                    prev = last_by.get((sym, name))
                    if prev and not _is_new_signal(prev, plan, ts, ded):
                        skipped_dup += 1
                        continue
                plan.generated_at = ts
                row = {"ts": ts, "symbol": sym, "variant": name,
                       "mode": mode, "plan": plan.to_dict()}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
                by_variant[name] += 1
                if ded["enabled"]:
                    last_by[(sym, name)] = row
    out = {"path": str(p), "written": written, "skipped_wait": skipped_wait,
           "by_variant": by_variant}
    if ded["enabled"]:
        out["skipped_dup"] = skipped_dup
    return out


def _dedupe_config(cfg: dict) -> dict:
    """影子去重配置，缺省开：冷却 6 小时、评分变化阈值 8。"""
    sh = (cfg.get("schedule") or {}).get("shadow") or {}
    d = sh.get("dedupe") or {}
    return {
        "enabled": bool(d.get("enabled", True)),
        "cooldown_sec": float(d.get("cooldown_sec") or 21600),
        "score_delta": float(d.get("score_delta") or 8.0),
    }


def _is_new_signal(prev: dict, plan, ts: float, ded: dict) -> bool:
    """要不要把这笔信号当成"新的独立信号"记下来。

    方向变了 → 新信号；评分变化超过阈值 → 强度升级的新信号；
    超出冷却期 → 旧信号已结束的独立信号；否则算同一信号的重复采样，跳过。
    """
    if prev["plan"].get("direction") != plan.direction:
        return True
    score_now = abs(float(plan.score or 0))
    score_prev = abs(float(prev["plan"].get("score") or 0))
    if abs(score_now - score_prev) >= ded["score_delta"]:
        return True
    return (ts - float(prev.get("ts") or 0)) >= ded["cooldown_sec"]


def load(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else data_path(DEFAULT_PATH)
    if not p.exists():
        return []
    rows = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def report(entries: list[dict], candles_by_symbol: dict, *,
           exit_by_variant: dict | None = None) -> dict:
    """用真实 K 线判定每条影子信号的结局，按方案汇总。

    candles_by_symbol 支持两种格式：
    - {symbol: [Candle, ...]}                    旧格式：所有记录用同一组 K 线
    - {symbol: {period: [Candle, ...]}}          新格式：按该记录的止损 ATR 周期
      （plan.market.atr_period）取判定 K 线——tf4h 的止损/止盈按 4hour ATR 算，
      用日线判会漏掉 4h 级别的出场（一根日线直接跳过止损位）。
    exit_by_variant:   {variant: {"breakeven_at_r":..., "trail_atr_mult":...}}
                       通常直接用 VARIANTS 里的配置；不传就按基线出场。
    """
    rows: list[dict] = []
    for e in entries:
        sym = e.get("symbol")
        per = candles_by_symbol.get(sym) or []
        plan = e.get("plan") or {}
        variant = e.get("variant") or "?"
        if isinstance(per, dict):
            atrp = (plan.get("market") or {}).get("atr_period") or "1day"
            candles = per.get(atrp) or []
        else:
            candles = per
        if not candles:
            # 还没拿到后续行情 -> 待定，不要当成"没有这笔"
            rows.append({"variant": variant, "symbol": sym,
                         "outcome": "pending", "r": None})
            continue
        ex = (exit_by_variant or {}).get(e.get("variant"), {}) or {}
        res = evaluate_outcome(
            plan, candles,
            breakeven_at_r=ex.get("breakeven_at_r"),
            trail_atr_mult=ex.get("trail_atr_mult"),
            atr=(plan.get("market") or {}).get("atr"))
        if not res or res.get("outcome") == "no_fill":
            rows.append({"variant": e.get("variant"), "symbol": sym,
                         "outcome": (res or {}).get("outcome") or "pending",
                         "r": None})
            continue
        if res.get("outcome") == "timeout":
            r = 0.0
        elif res["outcome"].startswith("tp"):
            r = float(res.get("rr") if res.get("rr") is not None else 1.0)
        else:
            r = float(res.get("rr") if res.get("rr") is not None else -1.0)
        rows.append({"variant": e.get("variant"), "symbol": sym,
                     "outcome": res["outcome"], "r": r})
    return summarize(rows)


def readiness(summary: dict, *, target: int = READY_SAMPLES) -> dict:
    """每个方案"样本够不够下结论"。

    pending 很多而 decided 很少时，说明记录时间还不够长——这时候对比期望值
    没有意义，先等。返回的 advice 直接给 CLI/网页用。
    """
    out: dict[str, dict] = {}
    for name, v in (summary.get("variants") or {}).items():
        decided = int(v.get("decided") or 0)
        if decided >= target:
            status = "ready"
            advice = "样本够了，可以据此调整默认值"
        elif decided >= MIN_SAMPLES:
            status = "初步"
            advice = f"达到显著性下限，建议再攒到 {target} 笔"
        else:
            status = "不足"
            need = MIN_SAMPLES - decided
            advice = f"还差 {max(0, need)} 笔到显著性下限（{MIN_SAMPLES}）"
        out[name] = {
            "decided": decided,
            "pending": int(v.get("pending") or 0),
            "target": target,
            "progress": round(min(1.0, decided / target), 3),
            "status": status,
            "advice": advice,
        }
    return out


def summarize(rows: list[dict]) -> dict:
    """按方案汇总：成交笔数、胜率、期望 R、95% 区间、待定笔数。"""
    variants = sorted({r.get("variant") or "?" for r in rows})
    out: dict[str, dict] = {}
    for v in variants:
        mine = [r for r in rows if (r.get("variant") or "?") == v]
        decided = [r for r in mine if r.get("r") is not None]
        rs = [float(r["r"]) for r in decided]
        wins = sum(1 for r in decided if (r.get("outcome") or "").startswith("tp"))
        ci = _bootstrap_mean_ci(rs)
        out[v] = {
            "signals": len(mine),
            "decided": len(decided),
            "pending": len(mine) - len(decided),
            "wins": wins,
            "win_rate": round(wins / len(decided), 4) if decided else 0.0,
            "expectancy_r": round(sum(rs) / len(rs), 4) if rs else 0.0,
            "expectancy_ci": ([round(x, 4) for x in ci] if ci else None),
        }
    return {"variants": out, "rows": rows}
