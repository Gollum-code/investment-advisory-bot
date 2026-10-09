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
    },

    # 定时任务
    "schedule": {
        "enabled": False,
        "interval_sec": 300,         # 循环间隔（秒）
        "symbols": ["BTC-USDT"],
        "run_on_start": True,
        "only_on_signal": False,     # 只在有明确方向时落库
    },

    "storage": {
        "db_file": "data/signals.db",
        "keep_days": 30,
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


def load_config() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg = _merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:  # 配置坏了也要能启动
            print(f"[config] 读取 config.json 失败，使用默认配置: {exc}")
    return cfg


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
