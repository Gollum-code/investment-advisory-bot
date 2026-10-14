"""全市场合约池：把"监控所有币种"落成两段式。

火币 USDT 永续有 300+ 个合约，其中一多半是代币化股票 / 大宗商品
（labels 带 stock / tradfi / commodities / indices），只有一百多个是加密币；
而且 24h 成交额中位数只有几万 U，绝大多数是几乎没有流动性的僵尸合约。
逐个深挖既慢又会被限流，所以全市场走两段：

1. **广度雷达**（1 个请求）：`all_tickers` 一次拿回全部合约的现价、24h 涨跌、
   成交额，再 `contract_list` 补上面值与标签，合并成一张全市场表。
2. **深度分析**：只对筛出来的候选（流动性达标 / 标签命中 / 用户强制纳入）
   做完整因子打分。筛选规则全部由 `universe` 配置块控制。

本模块只做"从行情表里挑出该关注的合约"这件事，是纯函数 + 一个薄薄的编排，
方便脱离网络做单元测试。
"""

from __future__ import annotations

from typing import Any

from .market import MarketClient
from .symbols import to_contract

# 表示"全市场"的写法：scan all / scan 全部 / symbols=*
ALL_TOKENS = {"all", "全部", "所有", "*", "全市场", "全幣", "全币"}

# 代币化股票 / 大宗商品 / 指数不是加密币，默认全市场只扫加密币
NON_CRYPTO_LABELS = {"stock", "tradfi", "commodities", "indices"}

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "asset_class": "crypto",        # crypto（默认，剔除股票/大宗商品）| all
    "min_turnover_usdt": 100000.0,  # 24h 成交额下限，过滤僵尸合约
    "min_price": 0.0,               # 现价下限，过滤近零币
    "include_labels": [],           # 命中任一标签即优先纳入（如 hot/common/new）
    "exclude_labels": [],           # 命中任一标签即剔除
    "include": [],                  # 强制纳入的合约代码
    "exclude": [],                  # 强制排除的合约代码
    "max_symbols": 60,              # 深挖上限（按成交额从高到低截断）
    "sort": "turnover",             # turnover | abs_change | change
}


def merge_spec(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """把 config 里的 `universe` 块和默认值合并成一份完整规格。"""
    raw = (cfg or {}).get("universe") or {}
    spec = {**DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}
    spec["min_turnover_usdt"] = float(spec.get("min_turnover_usdt") or 0.0)
    spec["min_price"] = float(spec.get("min_price") or 0.0)
    try:
        n_max = int(spec.get("max_symbols"))
    except (TypeError, ValueError):
        n_max = DEFAULTS["max_symbols"]
    spec["max_symbols"] = max(1, n_max)   # 0 或负数没有意义，兜底成 1
    spec["asset_class"] = str(spec.get("asset_class") or "crypto").lower()
    spec["include_labels"] = [str(x).lower() for x in (spec.get("include_labels") or [])]
    spec["exclude_labels"] = [str(x).lower() for x in (spec.get("exclude_labels") or [])]
    spec["include"] = [to_contract(x) for x in (spec.get("include") or []) if x]
    spec["exclude"] = [to_contract(x) for x in (spec.get("exclude") or []) if x]
    return spec


def is_all_token(token: str) -> bool:
    """判断一个合约写法是不是"全市场"占位符。"""
    t = (token or "").strip().lower()
    return t in ALL_TOKENS


def labels_of(contract: dict) -> set[str]:
    return {str(x).lower() for x in (contract.get("labels") or [])}


def is_crypto(labels: set[str]) -> bool:
    return not (labels & NON_CRYPTO_LABELS)


def classify(row: dict) -> str:
    """给全市场表里的一行打上粗分类，供前端展示。"""
    labels = row.get("labels") or set()
    if "stock" in labels:
        return "stock"
    if "tradfi" in labels:
        return "tradfi"
    if "commodities" in labels:
        return "commodity"
    if "indices" in labels:
        return "index"
    return "crypto"


def build_radar(client: MarketClient) -> list[dict]:
    """合并合约清单 + 批量行情，得到全市场雷达表（按成交额降序）。

    合约清单拿面值/标签，批量行情拿现价/涨跌/成交额。两个接口任一失败都会
    让整张表缺列，所以这里对缺数据的合约做兜底而不是丢弃。
    """
    info_by_code: dict[str, dict] = {}
    try:
        for c in client.contract_list():
            info_by_code[str(c.get("contract_code") or "")] = c
    except Exception:
        pass

    rows: list[dict] = []
    for t in client.all_tickers():
        code = str(t.get("symbol") or "")
        info = info_by_code.get(code) or {}
        labels = labels_of(info) if info else set()
        try:
            size = float(info.get("contract_size") or 0.0) or None
        except (TypeError, ValueError):
            size = None
        rows.append({
            "symbol": code,
            "price": t.get("price"),
            "change_pct": t.get("change_pct"),
            "turnover": float(t.get("turnover") or 0.0),
            "volume": float(t.get("volume") or 0.0),
            "count": int(t.get("count") or 0),
            "contract_size": size,
            "labels": sorted(labels),
            "asset_class": classify({"labels": labels}),
            "status": info.get("contract_status"),
        })

    # 只有清单、没有行情的合约（停牌/新上线未开盘）也补进来，方便前端全量展示
    seen = {r["symbol"] for r in rows}
    for code, info in info_by_code.items():
        if code in seen:
            continue
        labels = labels_of(info)
        rows.append({
            "symbol": code, "price": None, "change_pct": None, "turnover": 0.0,
            "volume": 0.0, "count": 0,
            "contract_size": float(info.get("contract_size") or 0.0) or None,
            "labels": sorted(labels),
            "asset_class": classify({"labels": labels}),
            "status": info.get("contract_status"),
        })

    rows.sort(key=lambda r: -float(r.get("turnover") or 0.0))
    return rows


def _passes(row: dict, spec: dict[str, Any]) -> bool:
    symbol = row.get("symbol") or ""
    if symbol in spec["exclude"]:
        return False
    if spec["asset_class"] != "all" and row.get("asset_class") != "crypto":
        return False
    labels = {str(x).lower() for x in (row.get("labels") or [])}
    if labels & set(spec["exclude_labels"]):
        return False
    turnover = float(row.get("turnover") or 0.0)
    price = row.get("price")
    # 强制纳入的合约豁免流动性与价格门槛
    if symbol not in spec["include"]:
        if turnover < spec["min_turnover_usdt"]:
            return False
        if price is not None and float(price) < spec["min_price"]:
            return False
    return True


def select(rows: list[dict], spec: dict[str, Any]) -> list[dict]:
    """按规格过滤 + 排序 + 截断，返回该深挖的合约行。"""
    kept = [r for r in rows if _passes(r, spec)]

    forced = set(spec["include"])
    sort_key = spec.get("sort") or "turnover"

    def key(r: dict):
        # 强制纳入的排最前，保证不会被 max_symbols 截断掉
        if r.get("symbol") in forced:
            return (0, 0.0)
        if sort_key == "abs_change":
            return (1, -abs(float(r.get("change_pct") or 0.0)))
        if sort_key == "change":
            return (1, -float(r.get("change_pct") or 0.0))
        return (1, -float(r.get("turnover") or 0.0))

    kept.sort(key=key)
    return kept[:spec["max_symbols"]]


def symbols_from_rows(rows: list[dict]) -> list[str]:
    return [r["symbol"] for r in rows if r.get("symbol")]


def resolve(client: MarketClient, cfg: dict[str, Any] | None = None,
            *, override: dict[str, Any] | None = None) -> list[str]:
    """发现并筛选全市场合约，返回该深挖的合约代码列表。

    override 用于一次性覆盖配置（CLI 参数 / 网页查询串），不写回配置。
    """
    spec = merge_spec(cfg)
    if override:
        spec.update({k: v for k, v in override.items() if v is not None})
        spec = merge_spec({"universe": spec})
    rows = build_radar(client)
    return symbols_from_rows(select(rows, spec))


def expand_tokens(client: MarketClient, tokens: list[str],
                  cfg: dict[str, Any] | None = None,
                  *, override: dict[str, Any] | None = None) -> list[str]:
    """把混合的合约写法展开成全量列表。

    列表里出现 `all` / `全部` 之一就并上全市场筛选结果；其余按字面合约处理。
    这样 `scan BTC-USDT,all` 这种写法也能用。
    """
    out: list[str] = []
    seen: set[str] = set()
    want_all = False
    for tok in tokens:
        if is_all_token(tok):
            want_all = True
            continue
        sym = to_contract(tok)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    if want_all:
        for sym in resolve(client, cfg, override=override):
            if sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out
