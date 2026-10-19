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

from .config import DEFAULT_MODE, SCHEDULABLE_MODES
from .signals import FACTOR_DEFAULTS


def _norm_weights(w: dict | None) -> dict:
    out = {}
    for k in FACTOR_DEFAULTS:
        v = (w or {}).get(k)
        out[k] = float(v) if isinstance(v, (int, float)) and v > 0 else FACTOR_DEFAULTS[k]
    return out


def _thr(v) -> float:
    """score_threshold 缺省取 30；但显式填 0 是合法的（阈值 0 = 不设门槛），
    不能因为 0 为假就替换成 30。"""
    if v is None or v == "":
        return 30.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 30.0


def _mode(v) -> str:
    """方案模式必须落在可排程白名单里。老配置文件里可能已经存了 reversion，
    读的时候也要纠正，否则 apply() 会把负期望通道塞进 schedule.mode。"""
    return v if v in SCHEDULABLE_MODES else DEFAULT_MODE


def _exp_key(v) -> float:
    """排序键：期望 R 缺失时排最后。不能写 `v or -999`——0.0 是合法值却为假，
    会被当成 -999 而排到所有负数后面。"""
    if v is None:
        return -999.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return -999.0


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
                "mode": _mode(p.get("mode")),
                "score_threshold": _thr(p.get("score_threshold")),
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
            "mode": _mode(p.get("mode")),
            "score_threshold": _thr(p.get("score_threshold")),
            "factor_weights": _norm_weights(p.get("factor_weights")),
        }

    def save(self, name: str, *, factor_weights: dict, score_threshold: float,
             mode: str, note: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("方案名不能为空")
        # 方案里的模式会经 apply() 写进 schedule.mode 被定时任务真的用上，
        # 所以这里必须和白名单校验：reversion 完整历史为负期望，scalp 之类
        # 的拼写错误也要拦下，不能让存错的名字变成实盘扫描的模式。
        mode = mode if mode in SCHEDULABLE_MODES else DEFAULT_MODE
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
        self.cfg.setdefault("schedule", {})["mode"] = _mode(p["mode"])
        self.cfg["active_profile"] = name
        return p

    def current_params(self) -> dict:
        """当前运行时生效的参数（未保存为方案时也能存成一个方案）。"""
        an = self.cfg.get("analysis") or {}
        return {
            "factor_weights": _norm_weights(an.get("factor_weights")),
            "score_threshold": _thr(an.get("score_threshold")),
            "mode": _mode((self.cfg.get("schedule") or {}).get("mode")),
        }


def cfg_with_profile(cfg: dict, profile: dict | None) -> dict:
    """返回一个把某方案的权重/阈值/模式注入后的 cfg 副本（用于回测对比）。"""
    if not profile:
        return cfg
    out = copy.deepcopy(cfg)
    an = out.setdefault("analysis", {})
    an["factor_weights"] = _norm_weights(profile.get("factor_weights"))
    an["score_threshold"] = _thr(profile.get("score_threshold"))
    out.setdefault("schedule", {})["mode"] = _mode(profile.get("mode"))
    return out


def backtest_all(cfg: dict, *, snapshot, symbol: str, bars: int = 2000,
                 step: int = 10) -> list[dict]:
    """对当前参数 + 所有已保存方案，在同一段历史上逐个回测并排序。

    snapshot 复用同一份历史快照（各方案只改权重/阈值/模式），结果可比。
    """
    from .backtest import backtest

    ps = ProfileStore(cfg)
    rows: list[dict] = []
    base = {"name": "当前参数", "active": True if ps.active else False,
            "mode": _mode((cfg.get("schedule") or {}).get("mode")),
            "score_threshold": _thr((cfg.get("analysis") or {}).get("score_threshold")),
            "profile": None}
    prof_cfg = cfg_with_profile(cfg, None)
    res = backtest(snapshot, prof_cfg, mode=base["mode"], step=step, max_points=2000)
    rows.append({**base, "result": res.to_dict()})
    for p in ps.list():
        # 必须取 get() 的完整方案：list() 返回的键是 "weights"，
        # 而 cfg_with_profile 读的是 "factor_weights"——直接用 list() 的元素
        # 会让权重静默回落成默认值，几个方案回测出来结果一模一样。
        prof = ps.get(p["name"]) or p
        r = backtest(snapshot, cfg_with_profile(cfg, prof), mode=p["mode"],
                     step=step, max_points=2000)
        rows.append({"name": p["name"], "active": p["active"], "mode": p["mode"],
                     "score_threshold": p["score_threshold"],
                     "profile": p, "result": r.to_dict()})
    # 数据不足的方案排到最后：expectancy 0 不该被当成"最好的方案"
    rows.sort(key=lambda x: (not x["result"].get("insufficient_data"),
                             -_exp_key(x["result"].get("expectancy_r"))))
    return rows
