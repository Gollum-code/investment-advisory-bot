"""币种表：合约代码 + 中文名 / 俗称别名。

只用于前端下拉与自然语言识别，不限制实际可分析的合约——
任何 `XXX-USDT` 形式的代码都能直接请求接口。
"""

from __future__ import annotations

from typing import Iterable

# (合约代码前缀, 显示名, 中文, 别名…)
CATALOG: list[tuple[str, str, tuple[str, ...]]] = [
    ("BTC",  "比特币",  ("大饼", "饼子", "bitcoin", "xbt")),
    ("ETH",  "以太坊",  ("二饼", "以太", "ethereum", "eth")),
    ("SOL",  "索拉纳",  ("solana", "索拉")),
    ("XRP",  "瑞波币",  ("瑞波", "ripple")),
    ("DOGE", "狗狗币",  ("狗币", "dogecoin", "doge")),
    ("BNB",  "币安币",  ("bnb", "binance")),
    ("ADA",  "艾达币",  ("cardano",)),
    ("TRX",  "波场",    ("tron",)),
    ("LTC",  "莱特币",  ("litecoin", "辣条")),
    ("LINK", "链环",    ("chainlink",)),
    ("AVAX", "雪崩",    ("avalanche",)),
    ("DOT",  "波卡",    ("polkadot",)),
    ("BCH",  "比特现金", ("bcc", "bitcoincash")),
    ("BSV",  "比特SV",  ()),
    ("ETC",  "以太经典", ()),
    ("XLM",  "恒星币",  ("stellar",)),
    ("EOS",  "柚子",    ("eos",)),
    ("FIL",  "文件币",  ("filecoin",)),
    ("UNI",  "Uniswap", ("uniswap",)),
    ("AAVE", "Aave",    ()),
    ("MKR",  "Maker",   ()),
    ("CRV",  "Curve",   ()),
    ("SUSHI","Sushi",   ()),
    ("COMP", "Compound",()),
    ("SNX",  "Synthetix",()),
    ("YFI",  "Yearn",   ()),
    ("ATOM", "宇宙币",  ("cosmos",)),
    ("NEAR", "NEAR",    ()),
    ("APT",  "Aptos",   ()),
    ("SUI",  "Sui",     ()),
    ("ARB",  "Arbitrum",()),
    ("OP",   "Optimism",()),
    ("MATIC","马蹄",    ("polygon", "matic")),
    ("POL",  "Polygon", ()),
    ("ICP",  "互联网计算机",()),
    ("ALGO", "阿尔戈",  ("algorand",)),
    ("VET",  "唯链",    ("vechain",)),
    ("THETA","Theta",   ()),
    ("EGLD", "MultiversX",()),
    ("FTM",  "Fantom",  ()),
    ("SAND", "沙盒",    ("sandbox",)),
    ("MANA", "去中心化地", ("decentraland",)),
    ("AXS",  "Axie",    ()),
    ("GALA", "Gala",    ()),
    ("APE",  "ApeCoin", ()),
    ("CHZ",  "奇利兹",  ("chiliz",)),
    ("ENJ",  "恩金",    ("enjin",)),
    ("ZEC",  "大零币",  ("zcash",)),
    ("DASH", "达世币",  ("dash",)),
    ("XMR",  "门罗币",  ("monero",)),
    ("NEO",  "小蚁",    ("neo",)),
    ("QTUM", "量子链",  ("qtum",)),
    ("ONT",  "本体",    ("ontology",)),
    ("IOTA", "埃欧塔",  ()),
    ("ZIL",  "Zilliqa", ()),
    ("ONE",  "Harmony", ()),
    ("HBAR", "哈希图",  ("hedera",)),
    ("GRT",  "TheGraph",()),
    ("RUNE", "THORChain",()),
    ("LDO",  "Lido",    ()),
    ("PEPE", "PEPE",    ("佩佩",)),
    ("SHIB", "柴犬",    ("shibainu",)),
    ("WIF",  "dogwifhat",()),
    ("BONK", "Bonk",    ()),
    ("FLOKI","Floki",   ()),
    ("ORDI", "Ordinals",()),
    ("SATS", "Sats",    ()),
    ("RATS", "Rats",    ()),
    ("TON",  "Toncoin", ()),
    ("KAS",  "Kaspa",   ()),
    ("SEI",  "Sei",     ()),
    ("TIA",  "Celestia",()),
    ("INJ",  "Injective",()),
    ("STX",  "Stacks",  ()),
    ("IMX",  "Immutable",()),
    ("WLD",  "Worldcoin",()),
    ("JUP",  "Jupiter", ()),
    ("PYTH", "Pyth",    ()),
    ("BLUR", "Blur",    ()),
]

_ALIAS: dict[str, str] = {}
for _base, _cn, _als in CATALOG:
    _ALIAS[_base.lower()] = _base
    _ALIAS[_cn] = _base
    for _a in _als:
        _ALIAS[_a.lower()] = _base


def to_contract(text: str) -> str:
    """把 `btc` / `比特币` / `BTC-USDT` / `BTCUSDT` 统一成 `BTC-USDT`。"""
    t = (text or "").strip()
    if not t:
        return ""
    up = t.upper()
    if up.endswith("-USDT"):
        return up
    if up.endswith("USDT") and len(up) > 4:
        return f"{up[:-4]}-USDT"
    base = _ALIAS.get(t.lower()) or _ALIAS.get(up.lower()) or up
    return f"{base}-USDT"


def base_of(contract: str) -> str:
    return (contract or "").split("-")[0].upper()


def display_name(contract: str) -> str:
    b = base_of(contract)
    for base, cn, _ in CATALOG:
        if base == b:
            return f"{b} · {cn}"
    return b


def all_contracts() -> list[str]:
    return [f"{b}-USDT" for b, _, _ in CATALOG]


def catalog_payload() -> list[dict]:
    return [
        {"code": f"{b}-USDT", "base": b, "name": cn, "aliases": list(als)}
        for b, cn, als in CATALOG
    ]


def detect_symbol(text: str, known: Iterable[str] | None = None) -> tuple[str | None, str | None]:
    """从一句话里识别币种，返回 (合约代码, 命中的原文)。"""
    if not text:
        return None, None
    lowered = text.lower()

    for code in known or ():
        c = str(code or "").strip()
        if not c:
            continue
        for token in (c, c.split("-")[0]):
            idx = lowered.find(token.lower())
            if idx != -1:
                return c, text[idx:idx + len(token)]

    # 显式合约写法
    import re
    m = re.search(r"([A-Za-z0-9]{2,15})-?USDT", text, re.I)
    if m:
        return to_contract(m.group(0)), m.group(0)

    for alias in sorted(_ALIAS, key=len, reverse=True):
        idx = lowered.find(alias)
        if idx != -1:
            if alias.isascii():
                before = lowered[idx - 1] if idx > 0 else ""
                after = lowered[idx + len(alias)] if idx + len(alias) < len(lowered) else ""
                if (before.isalnum() and before.isascii()) or (after.isalnum() and after.isascii()):
                    continue
            return f"{_ALIAS[alias]}-USDT", text[idx:idx + len(alias)]
    return None, None
