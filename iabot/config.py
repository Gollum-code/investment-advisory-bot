"""配置加载与保存。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
DATA_DIR = ROOT / "data"

# 行情域名。火币合约主域名在某些网络环境会被拦截，
# 这里按顺序做健康探测，用第一个能通的。
REST_BASES = [
    "https://api.hbdm.com",
    "https://api.btcgateway.pro",
    "https://api.hbdm.vip",
]

DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8972,
    "open_browser": True,
    "rest_bases": REST_BASES,
    "tls_verify": True,
    "http_timeout_sec": 12.0,
    "http_retries": 2,

    # 默认关注的合约
    "symbols": [
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT",
        "BNB-USDT", "LTC-USDT", "LINK-USDT", "AVAX-USDT", "ADA-USDT",
    ],

    # 账户与风控
    "account": {
        "equity_usdt": 450.0,
        "risk_pct_per_trade": 2.0,   # 单笔最大亏损占账户百分比
        "max_leverage": 10,          # 杠杆上限（建议值）
        "preferred_leverage": 10,    # 实际下单杠杆
        "max_margin_pct": 50.0,      # 单笔最多占用账户保证金比例
    },

    # 分析参数
    "analysis": {
        "tf_periods": ["1min", "5min", "15min", "60min", "1day"],
        "kline_size": 300,
        "score_threshold": 30,       # |score| 达到该值才给方向（tanh 归一化后）
        "cache_ttl_sec": 20,          # 同一合约多久内重复查询直接复用结果
        "snapshot_ttl_sec": 20,       # 行情快照保鲜期（定时任务靠它拿到新数据）
        # 因子权重覆盖（可选）：{"trend_daily": 0.2, "momentum": 0.1, ...}
        # 不填用内置默认；网页上可调并保存到这里
        "factor_weights": {},
    },

    # 参数方案：命名打包"权重+阈值+模式"，可回测对比后一键应用
    "profiles": {},
    "active_profile": "",

    # 定时任务
    "schedule": {
        "enabled": False,
        "interval_sec": 300,         # 循环间隔（秒）
        "symbols": ["BTC-USDT"],
        "run_on_start": True,
        "only_on_signal": False,     # 只在有明确方向时落库
        # 落库去重：方向没变且评分变化不大时，间隔期内不重复记录同一个信号
        "dedupe": {
            "cooldown_sec": 3600,    # 同一合约同一方向两次落库的最小间隔
            "score_delta": 5.0,      # 评分变化超过这个值才算"新信号"
        },
        # 结果回填：拿后续 K 线判断信号先碰止损还是先到目标，写回胜率统计
        "verify": {
            "enabled": True,
            "min_age_sec": 900,      # 信号生成多久之后才开始回填
            "max_per_run": 50,       # 每轮最多回填多少条
        },
    },

    "storage": {
        "db_file": "data/signals.db",
        "keep_days": 30,
    },

    # HTTP 访问控制：绑定到非本机地址时建议设 token（只放本地 config.json）
    "auth": {
        "token": "",
    },

    # 可选访问日志：留空则只在控制台打印；填路径则追加写入
    "log_file": "",

    # 信号通知（配置里的 token/URL 只在本地 config.json，不进仓库）
    "notify": {
        "enabled": False,
        "min_abs_score": 45.0,       # 只推 |score| 达到这个的信号
        "on_direction_change": True,  # 方向翻转时也推
        "timeout_sec": 8.0,
        "webhook": {},               # {"url": "...", "headers": {...}}
        "telegram": {},              # {"token": "xxx", "chat_id": "123"}
        "bark": {},                  # {"url": "https://api.day.app/KEY"}
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce_num(val, name, lo=None, hi=None, default=0.0):
    """把配置里的数值字段转成 float，超范围/非数字时钳回默认并告警。"""
    try:
        v = float(val)
    except (TypeError, ValueError):
        print(f"[config] 字段 {name}={val!r} 不是数字，改用 {default}")
        return default
    if lo is not None and v < lo:
        print(f"[config] 字段 {name}={v} 低于下限 {lo}，改用 {lo}")
        return lo
    if hi is not None and v > hi:
        print(f"[config] 字段 {name}={v} 高于上限 {hi}，改用 {hi}")
        return hi
    return v


def _coerce_int(val, name, lo=None, hi=None, default=0):
    return int(_coerce_num(val, name, lo, hi, float(default)))


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """类型强转 + 范围钳制。用户手改 config.json 最容易把数字写成字符串，
    这里在启动时兜底：非法值回落默认并打一行告警，不让整个服务崩掉。

    返回一个新的 dict，不改原 cfg。
    """
    out = copy.deepcopy(cfg)
    acc = out.setdefault("account", {})
    acc["equity_usdt"] = _coerce_num(acc.get("equity_usdt"), "account.equity_usdt",
                                     1, None, 450.0)
    acc["risk_pct_per_trade"] = _coerce_num(acc.get("risk_pct_per_trade"),
                                            "account.risk_pct_per_trade", 0.01, 100, 2.0)
    acc["max_leverage"] = _coerce_int(acc.get("max_leverage"), "account.max_leverage",
                                      1, 125, 10)
    acc["preferred_leverage"] = _coerce_int(acc.get("preferred_leverage"),
                                            "account.preferred_leverage", 1, 125, 10)
    acc["preferred_leverage"] = min(acc["preferred_leverage"], acc["max_leverage"])
    acc["max_margin_pct"] = _coerce_num(acc.get("max_margin_pct"),
                                        "account.max_margin_pct", 1, 100, 50.0)

    an = out.setdefault("analysis", {})
    an["score_threshold"] = _coerce_num(an.get("score_threshold"),
                                        "analysis.score_threshold", 0, 100, 30.0)
    an["cache_ttl_sec"] = _coerce_num(an.get("cache_ttl_sec"),
                                      "analysis.cache_ttl_sec", 1, 3600, 20.0)
    an["snapshot_ttl_sec"] = _coerce_num(an.get("snapshot_ttl_sec"),
                                         "analysis.snapshot_ttl_sec", 1, 3600, 20.0)
    fw = an.get("factor_weights")
    if fw is None:
        an["factor_weights"] = {}
    elif not isinstance(fw, dict):
        print("[config] analysis.factor_weights 不是字典，已重置为空")
        an["factor_weights"] = {}

    out["http_timeout_sec"] = _coerce_num(out.get("http_timeout_sec"),
                                          "http_timeout_sec", 1, 120, 12.0)
    out["http_retries"] = _coerce_int(out.get("http_retries"), "http_retries", 0, 5, 2)

    if not isinstance(out.get("symbols"), list):
        out["symbols"] = list(DEFAULTS["symbols"])
    if not isinstance(out.get("profiles"), dict):
        out["profiles"] = {}
    if not isinstance(out.get("active_profile"), str):
        out["active_profile"] = ""
    sc = out.setdefault("schedule", {})
    sc["interval_sec"] = _coerce_int(sc.get("interval_sec"), "schedule.interval_sec",
                                     30, 86400, 300)
    return out


def load_config() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg = _merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:  # 配置坏了也要能启动
            print(f"[config] 读取 config.json 失败，使用默认配置: {exc}")
    return validate_config(cfg)


def _write_json(path: Path, obj: Any) -> None:
    # newline="\n"：Windows 上默认会把 \n 翻译成 \r\n，仓库里统一用 LF
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def save_config(cfg: dict[str, Any]) -> None:
    _write_json(CONFIG_PATH, cfg)


def ensure_example() -> None:
    """把当前默认配置写成 config.example.json（内容变了就刷新）。

    这只是给人看的参考文件；用户自己的配置在 config.json，这里不会碰。
    """
    text = json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n"
    try:
        if EXAMPLE_PATH.read_text(encoding="utf-8") == text:
            return
    except FileNotFoundError:
        pass
    _write_json(EXAMPLE_PATH, DEFAULTS)


def data_path(rel: str) -> Path:
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
