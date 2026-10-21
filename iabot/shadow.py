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
    # 分数带：前后半段分离检验里单笔期望稳定更好，但组合层面收益差 5-14 倍，
    # 所以不放进默认影子列表（每轮都要多算一遍，收益不配）。
    # 保留方案本身，需要时可手动跑 `shadow --variant band_40_55`。
    "band_40_55": {"analysis": {"score_band": {"enabled": True,
                                               "min": 40.0, "max": 55.0}}},
}

DEFAULT_PATH = "data/shadow.jsonl"
# 一个方案至少要有这么多"已判定"样本，才值得据此改默认值。
# 30 是统计显著性下限（与 backtest 的 MIN_TRADES_FOR_SIGNIFICANCE 一致），
# 100 是"足够放心"的经验值。
MIN_SAMPLES = 30
READY_SAMPLES = 100


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

    written = 0
    skipped_wait = 0
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
                plan.generated_at = ts
                row = {"ts": ts, "symbol": sym, "variant": name,
                       "mode": mode, "plan": plan.to_dict()}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
                by_variant[name] += 1
    return {"path": str(p), "written": written, "skipped_wait": skipped_wait,
            "by_variant": by_variant}


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

    candles_by_symbol: {symbol: [Candle, ...]}（该标的信号之后的主周期 K 线）。
    exit_by_variant:   {variant: {"breakeven_at_r":..., "trail_atr_mult":...}}
                       通常直接用 VARIANTS 里的配置；不传就按基线出场。
    """
    rows: list[dict] = []
    for e in entries:
        sym = e.get("symbol")
        candles = candles_by_symbol.get(sym) or []
        plan = e.get("plan") or {}
        variant = e.get("variant") or "?"
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
