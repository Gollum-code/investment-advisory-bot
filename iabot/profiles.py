"""参数方案（profile）：把"权重 + 阈值 + 模式"打包成一个可命名、可对比、可一键应用的组合。

用途：调参不是盲调。用多个方案分别回测同一段历史，按期望 R / 胜率排序，
再把最优方案一键应用到实盘扫描与定时任务——这就是"纸盘跟单"的参数层。

方案存在 config.json（随 config 保存），结构：
    "profiles": {
      "稳健": {"factor_weights": {...}, "score_threshold": 40, "mode": "intraday", "note": "..."}
    },
    "active_profile": "稳健"
"""

from __future__ import annotations

import copy
from typing import Any

from .signals import FACTOR_DEFAULTS


def _norm_weights(w: dict | None) -> dict:
    out = {}
    for k in FACTOR_DEFAULTS:
        v = (w or {}).get(k)
        out[k] = float(v) if isinstance(v, (int, float)) and v > 0 else FACTOR_DEFAULTS[k]
    return out


class ProfileStore:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    @property
    def profiles(self) -> dict:
        p = self.cfg.get("profiles")
        return p if isinstance(p, dict) else {}

    @property
    def active(self) -> str | None:
        return self.cfg.get("active_profile") or None

    def list(self) -> list[dict]:
        out = []
        for name, p in self.profiles.items():
            p = p or {}
            w = _norm_weights(p.get("factor_weights"))
            custom = sum(1 for k, v in w.items() if abs(v - FACTOR_DEFAULTS[k]) > 1e-9)
            out.append({
                "name": name,
                "note": p.get("note", ""),
                "mode": p.get("mode") or "intraday",
                "score_threshold": float(p.get("score_threshold") or 30),
                "weights": w,
                "custom": custom,
                "active": name == self.active,
            })
        return sorted(out, key=lambda x: x["name"])

    def get(self, name: str) -> dict | None:
        p = self.profiles.get(name)
        if not p:
            return None
        return {
            "name": name,
            "note": p.get("note", ""),
            "mode": p.get("mode") or "intraday",
            "score_threshold": float(p.get("score_threshold") or 30),
            "factor_weights": _norm_weights(p.get("factor_weights")),
        }

    def save(self, name: str, *, factor_weights: dict, score_threshold: float,
             mode: str, note: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("方案名不能为空")
        self.cfg.setdefault("profiles", {})[name] = {
            "factor_weights": _norm_weights(factor_weights),
            "score_threshold": float(score_threshold),
            "mode": mode,
            "note": note,
        }
        return self.get(name)  # type: ignore[return-value]

    def delete(self, name: str) -> bool:
        if name in self.profiles:
            del self.cfg["profiles"][name]
            if self.active == name:
                self.cfg["active_profile"] = ""
            return True
        return False

    def apply(self, name: str) -> dict:
        """把方案参数写进运行时配置（分析权重 / 阈值 / 定时任务模式）。"""
        p = self.get(name)
        if not p:
            raise ValueError(f"方案不存在: {name}")
        an = self.cfg.setdefault("analysis", {})
        an["factor_weights"] = p["factor_weights"]
        an["score_threshold"] = p["score_threshold"]
        self.cfg.setdefault("schedule", {})["mode"] = p["mode"]
        self.cfg["active_profile"] = name
        return p

    def current_params(self) -> dict:
        """当前运行时生效的参数（未保存为方案时也能存成一个方案）。"""
        an = self.cfg.get("analysis") or {}
        return {
            "factor_weights": _norm_weights(an.get("factor_weights")),
            "score_threshold": float(an.get("score_threshold") or 30),
            "mode": (self.cfg.get("schedule") or {}).get("mode") or "intraday",
        }


def cfg_with_profile(cfg: dict, profile: dict | None) -> dict:
    """返回一个把某方案的权重/阈值/模式注入后的 cfg 副本（用于回测对比）。"""
    if not profile:
        return cfg
    out = copy.deepcopy(cfg)
    an = out.setdefault("analysis", {})
    an["factor_weights"] = _norm_weights(profile.get("factor_weights"))
    an["score_threshold"] = float(profile.get("score_threshold") or 30)
    out.setdefault("schedule", {})["mode"] = profile.get("mode") or "intraday"
    return out


def backtest_all(cfg: dict, *, snapshot, symbol: str, bars: int = 300,
                 step: int = 10) -> list[dict]:
    """对当前参数 + 所有已保存方案，在同一段历史上逐个回测并排序。

    snapshot 复用同一份历史快照（各方案只改权重/阈值/模式），结果可比。
    """
    from .backtest import backtest

    ps = ProfileStore(cfg)
    rows: list[dict] = []
    base = {"name": "当前参数", "active": True if ps.active else False,
            "mode": (cfg.get("schedule") or {}).get("mode") or "intraday",
            "score_threshold": (cfg.get("analysis") or {}).get("score_threshold") or 30,
            "profile": None}
    prof_cfg = cfg_with_profile(cfg, None)
    res = backtest(snapshot, prof_cfg, mode=base["mode"], step=step, max_points=2000)
    rows.append({**base, "result": res.to_dict()})
    for p in ps.list():
        r = backtest(snapshot, cfg_with_profile(cfg, p), mode=p["mode"],
                     step=step, max_points=2000)
        rows.append({"name": p["name"], "active": p["active"], "mode": p["mode"],
                     "score_threshold": p["score_threshold"],
                     "profile": p, "result": r.to_dict()})
    rows.sort(key=lambda x: -(x["result"].get("expectancy_r") or -999))
    return rows
