"""配置加载与保存。"""

from __future__ import annotations

import copy
import json
import os
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

# 默认分析模式。选 swing 的唯一理由是历史数据：完整回测里 intraday 是负期望
# （10056 笔 -0.094R），swing 是正期望（5418 笔 +0.184R）。所有"没指定模式"
# 的入口（定时任务、CLI、网页 API、参数方案）都回落到这里，避免再把用户
# 默认带进一个亏钱的模式。
DEFAULT_MODE = "swing"
# 流动性体检的默认阈值（唯一出处，liquidity.py 从这里读）
DEFAULT_MIN_TURNOVER = 5_000_000.0   # 24h 成交额下限（U）
DEFAULT_MAX_SPREAD_BPS = 20.0       # 买卖价差上限（0.2%）

# 能被定时任务/参数方案带进实盘扫描的模式白名单。
# 与 config.validate_config 里对 schedule.mode 的钳制必须是同一个集合——
# 参数方案会把它的 mode 直接写进 schedule.mode，绕开一次校验就会把
# 用户带进 reversion（完整历史 -0.384R）之类没有正期望的通道。
# reversion 仍可用于 backtest / 手动 analyze，只是不允许被排程。
SCHEDULABLE_MODES = ("intraday", "swing")

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

    # 全市场合约池：决定"该关注哪些合约"。火币 USDT 永续有 300+ 个合约，
    # 一半多是代币化股票/大宗商品，且 24h 成交额中位数只有几万 U，
    # 所以默认只扫流动性达标的加密币；深挖数量再由 max_symbols 截断。
    "universe": {
        "enabled": False,             # 是否启用全市场模式（网页/CLI 也可临时开启）
        "asset_class": "crypto",      # crypto=只加密币（剔除股票/商品/指数）| all
        "min_turnover_usdt": 100000.0,  # 24h 成交额下限，过滤僵尸合约
        "min_price": 0.0,             # 现价下限，过滤近零价合约
        "include_labels": [],         # 命中任一标签优先纳入，如 ["hot","common","new"]
        "exclude_labels": [],         # 命中任一标签直接剔除
        "include": [],                # 强制纳入（豁免流动性门槛，保证不被 max_symbols 挤掉）
        "exclude": [],                # 强制排除
        "max_symbols": 60,            # 深挖上限（按 sort 排序后截断）
        "sort": "turnover",           # turnover=成交额 | abs_change=24h 波动 | change=24h 涨跌
    },

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
        "kline_size": 2000,
        "plan_cache_max": 128,       # 计划缓存条数上限（长跑服务防止无限膨胀）
        "score_threshold": 30,       # |score| 达到该值才给方向（tanh 归一化后）
        "cache_ttl_sec": 20,          # 同一合约多久内重复查询直接复用结果
        "snapshot_ttl_sec": 20,       # 行情快照保鲜期（定时任务靠它拿到新数据）
        # 因子权重覆盖（可选）：{"trend_daily": 0.2, "momentum": 0.1, ...}
        # 不填用内置默认；网页上可调并保存到这里
        "factor_weights": {},
        # 多周期共振因子（可选第 11 因子，默认关闭）：日线偏置 × 1h 偏置，
        "resonance": {"enabled": False, "weight": 0.06},
        # 流动性体检：24h 成交额下限 + 买卖价差上限。
        # enforce=true 时不达标的合约直接降级为观望（默认只警告不拦）。
        "liquidity": {
            "min_turnover_24h": 5_000_000.0,
            "max_spread_bps": 20.0,
            "enforce": False,
        },
        # 多空不对称风险：空单在历史上两个方向都赚钱，多单只在上涨市赚钱。
        # 1.0 = 不调整（默认）；调大 = 该方向单笔风险按倍数放大。
        "risk_mult_long": 1.0,
        "risk_mult_short": 1.0,
        # 趋势衰减实验：实测评分 79% 的单子由"日线趋势"主导，且趋势越极端
        # 越可能是行情尾声（高分档期望 +0.111R，中分档 +0.288R）。
        # 这里对 |日线趋势| 超过 dampen_at 的部分做衰减：k=1 时衰减到 1/2。
        # 默认关；是因子层改动，要先用影子账户验证，不能凭一次回测就打开。
        "trend_dampen": {
            "enabled": False,
            "dampen_at": 0.6,   # 超过这个绝对值才开始衰减
            "k": 1.0,          # 衰减速度：k 越大衰减越弱
        },
        # 分数带闸门：只接受 |评分| 落在 [min, max] 的信号。
        # 依据是实测的排序诊断——高分档（|score| 最大的那档）期望最低，
        # 中分档最好，所以"分数越高越该做"这个前提不成立。与其调阈值
        # （调高会选中最差那档），不如直接圈定甜点区间。
        # 默认关：这是用同一批数据发现再检验的，有过拟合风险，
        # 要靠影子账户在未来的行情里验证。
        "score_band": {
            "enabled": False,
            "min": 40.0,
            "max": 55.0,
        },
        # 第二道闸门：目标位盈亏比。默认 tp1 = 只看第一目标（实际最先命中的那档）。
        # 回测过 weighted(0.5/0.3/0.2) 与 max，与 tp1 差异在噪声内，故保持 tp1。
        # 目标位筛选门槛：低于此盈亏比的目标位不作为 TP 候选。
        "gate": {
            "measure": "tp1",         # tp1 | weighted | max
            # 0.8 而非 1.0：把成交要求修对之后实测，0.8 在两段时期里都是
            # 「胜率更高、期望值不降」的占优选择（前段 47.9%/+0.139R、
            # 后段 53.0%/+0.198R，对比 min_rr=1.0 的 44.9%/+0.131R、48.5%/+0.187R）。
            "min_rr": 0.8,            # 闸门最低盈亏比
            "weights": [0.5, 0.3, 0.2],   # measure=weighted 时各档权重
            # 必须 <= min_rr，否则闸门形同虚设（候选门槛自己就把低 RR 目标滤掉了）
            "min_target_rr": 0.5,     # 目标位候选门槛
        },
        # 回测成本模型（更贴近真实：手续费 + 滑点 + 按持仓时长的资金费）
        "backtest": {
            "enabled": True,
            "taker_fee_bps": 5.0,      # 单边手续费 0.05%（开平各一次）
            "slippage_bps": 2.0,       # 单侧滑点 0.02%
            "maker_fee_bps": 2.0,      # 挂单手续费 0.02%
            # 平方根冲击系数：下单金额 == 当日成交额时约这么多 bps。
            # 设成 0 就退回"固定滑点"的老口径（不再按订单规模放大）。
            "impact_coeff_bps": 30.0,
            "funding": True,           # 按持仓时长累计资金费
            "funding_period_sec": 28800,  # 8 小时一次结算
        },
        # 出场管理：保本 / 移动止损。默认都关（null），开了才生效。
        #   breakeven_at_r: 浮盈到 X R 时把止损移到入场价
        #   trail_atr_mult: 止损跟随最优价，距离 = X × ATR
        "exit": {
            "breakeven_at_r": None,
            "trail_atr_mult": None,
        },
        # 趋势否决闸门：方向定了之后，若日线趋势明确反对就降级观望。
        # 默认关——完整历史（20 币 × 2000 根日线）显示它把 swing 期望从
        # +0.183R 提到 +0.199R、long 从 +0.119R 提到 +0.165R，但同时砍掉
        # 46% 的单量。等实盘/前向样本验证过再默认开。
        "trend_veto": False,
    },

    # 定时任务
    "schedule": {
        "enabled": False,
        "interval_sec": 300,         # 循环间隔（秒）
        # 默认模式。必须是 swing：完整历史实测（20 币 × 2000 根日线，
        # 真实规则逐笔）intraday 10056 笔期望 -0.094R、swing 5418 笔 +0.184R。
        # 之前的默认值 intraday 是亏钱的，见 README「默认模式为什么是 swing」。
        "mode": DEFAULT_MODE,
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
        # 影子账户：定时把当前信号按多套参数各记一份（只记录不下单），
        # 用未来的真实行情验证"要不要改默认值"。默认开、每 6 轮记一次，
        # 成本很低（复用 20 秒快照缓存），样本会自己慢慢攒起来。
        "shadow": {
            "enabled": True,
            "variants": ["base", "trail_1atr"],
            "every_runs": 6,
            "path": "",              # 留空 = data/shadow.jsonl
        },
        # 持仓接近止损预警：现价进入止损附近 pct% 内时推通知（每仓有冷却）
        "warn_near_stop": {
            "enabled": True,
            "pct": 1.5,              # 距止损小于该百分比时预警
            "cooldown_sec": 1800,    # 同一仓位两次预警的最小间隔
        },
        # 全市场轮询：enabled 打开后忽略 symbols，改用 universe.filters 筛选出的
        # 全市场合约；每轮只深挖 batch_size 个，游标跨轮推进，转完一圈才回头。
        # 不这么做的话几十上百个合约 × 每轮 300 秒根本跑不完，还必被限流。
        "universe": {
            "enabled": False,
            "batch_size": 20,        # 每轮深挖多少个合约
            "workers": 4,            # 并发分析数，调高容易撞火币限流（429）
            "filters": {},           # 覆盖顶层 universe 的过滤条件（可选）
        },
    },

    "storage": {
        "db_file": "data/signals.db",
        "keep_days": 30,
    },

    # 自动备份：DB 里有信号历史/模拟持仓/回测记录/利润簿，损坏无法恢复
    "backup": {
        "enabled": True,
        "dir": "data/backups",
        "hours": 24,      # 多久备份一次
        "keep": 14,       # 最多保留多少份
    },

    # 参数方案：命名打包"权重+阈值+模式"，可回测对比后一键应用
    "profiles": {},
    "active_profile": "",

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


def _section(out: dict, key: str, default: dict) -> dict:
    """取出一个"保证是字典"的配置段。

    setdefault 只在缺失时填默认值，对 `{"analysis": "oops"}` 这种用户手打错的
    值毫无作用——后面一调 .get() 就 AttributeError，启动直接崩。
    这个函数的职责恰恰是"不让服务崩掉"，所以这里必须做类型兜底。
    """
    v = out.get(key)
    if not isinstance(v, dict):
        if v is not None:
            print(f"[config] {key} 不是字典（{type(v).__name__}），已重置为默认")
        out[key] = copy.deepcopy(default)
    return out[key]


def _coerce_list(val, name: str, default: list) -> list:
    """列表兜底：字符串必须当整体收下。

    直接 `[s for s in val]` 会把 "BTC-USDT" 拆成 8 个单字符"合约"，
    而且不报错——定时任务会拿着 B-USDT、T-USDT… 傻跑一辈子。
    """
    if val is None:
        return list(default)
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, (list, tuple)):
        print(f"[config] {name} 不是列表（{type(val).__name__}），已重置为默认")
        return list(default)
    return [str(s).strip() for s in val if str(s).strip()]


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """类型强转 + 范围钳制。用户手改 config.json 最容易把数字写成字符串，
    这里在启动时兜底：非法值回落默认并打一行告警，不让整个服务崩掉。

    返回一个新的 dict，不改原 cfg。
    """
    out = copy.deepcopy(cfg)
    acc = _section(out, "account", DEFAULTS["account"])
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

    an = _section(out, "analysis", DEFAULTS["analysis"])
    an["trend_veto"] = bool(an.get("trend_veto", False))
    an["score_threshold"] = _coerce_num(an.get("score_threshold"),
                                        "analysis.score_threshold", 0, 100, 30.0)
    an["cache_ttl_sec"] = _coerce_num(an.get("cache_ttl_sec"),
                                      "analysis.cache_ttl_sec", 1, 3600, 20.0)
    an["snapshot_ttl_sec"] = _coerce_num(an.get("snapshot_ttl_sec"),
                                         "analysis.snapshot_ttl_sec", 1, 3600, 20.0)
    # 下面两个会被 Advisor 直接 int() 一次，没校验过就是启动即崩
    # 默认取接口上限 2000：300 根日线只有 0.8 年、15min 只有 3 天，样本不足
    # 会让任何回测结论都不可信（reversion 就是被 300 根的短样本骗过的）。
    # 实测每个周期取 2000 根与取 300 根的抓取耗时几乎一样——请求并行且受网络
    # 往返限制，多出来的数据量不是瓶颈。
    an["kline_size"] = _coerce_int(an.get("kline_size"), "analysis.kline_size",
                                   30, 2000, 2000)
    an["plan_cache_max"] = _coerce_int(an.get("plan_cache_max"),
                                       "analysis.plan_cache_max", 1, 100000, 128)
    tf = an.get("tf_periods")
    if not isinstance(tf, list) or not tf:
        an["tf_periods"] = list(DEFAULTS["analysis"]["tf_periods"])
    fw = an.get("factor_weights")
    if fw is None:
        an["factor_weights"] = {}
    elif not isinstance(fw, dict):
        print("[config] analysis.factor_weights 不是字典，已重置为空")
        an["factor_weights"] = {}
    rc = an.get("resonance")
    if not isinstance(rc, dict):        an["resonance"] = {"enabled": False, "weight": 0.06}
    else:
        rc["enabled"] = bool(rc.get("enabled", False))
        rc["weight"] = _coerce_num(rc.get("weight"), "analysis.resonance.weight",
                                   0.0, 1.0, 0.06)
    lc = an.get("liquidity")
    if not isinstance(lc, dict):
        an["liquidity"] = copy.deepcopy(DEFAULTS["analysis"]["liquidity"])
    else:
        lc["min_turnover_24h"] = _coerce_num(
            lc.get("min_turnover_24h"), "analysis.liquidity.min_turnover_24h",
            0.0, 1e12, DEFAULT_MIN_TURNOVER)
        lc["max_spread_bps"] = _coerce_num(
            lc.get("max_spread_bps"), "analysis.liquidity.max_spread_bps",
            0.0, 10000.0, DEFAULT_MAX_SPREAD_BPS)
        lc["enforce"] = bool(lc.get("enforce", False))

    an["risk_mult_long"] = _coerce_num(
        an.get("risk_mult_long"), "analysis.risk_mult_long", 0.1, 3.0, 1.0)
    an["risk_mult_short"] = _coerce_num(
        an.get("risk_mult_short"), "analysis.risk_mult_short", 0.1, 3.0, 1.0)

    td = an.get("trend_dampen")
    if not isinstance(td, dict):
        an["trend_dampen"] = {"enabled": False, "dampen_at": 0.6, "k": 1.0}
    else:
        td["enabled"] = bool(td.get("enabled", False))
        td["dampen_at"] = _coerce_num(td.get("dampen_at"),
                                      "analysis.trend_dampen.dampen_at",
                                      0.1, 1.0, 0.6)
        td["k"] = _coerce_num(td.get("k"), "analysis.trend_dampen.k",
                              0.05, 20.0, 1.0)
        an["trend_dampen"] = td

    sb = an.get("score_band")
    if not isinstance(sb, dict):
        an["score_band"] = {"enabled": False, "min": 40.0, "max": 55.0}
    else:
        sb["enabled"] = bool(sb.get("enabled", False))
        sb["min"] = _coerce_num(sb.get("min"), "analysis.score_band.min",
                                0.0, 100.0, 40.0)
        sb["max"] = _coerce_num(sb.get("max"), "analysis.score_band.max",
                                0.0, 100.0, 55.0)
        if sb["min"] > sb["max"]:
            sb["min"], sb["max"] = sb["max"], sb["min"]
        an["score_band"] = sb

    ec = an.get("exit")
    if not isinstance(ec, dict):
        an["exit"] = {"breakeven_at_r": None, "trail_atr_mult": None}
    else:
        # None 表示关闭；给了数字就夹到合理区间
        an["exit"] = {
            "breakeven_at_r": (None if ec.get("breakeven_at_r") in (None, "")
                               else _coerce_num(ec.get("breakeven_at_r"),
                                                "analysis.exit.breakeven_at_r",
                                                0.1, 10.0, 1.0)),
            "trail_atr_mult": (None if ec.get("trail_atr_mult") in (None, "")
                               else _coerce_num(ec.get("trail_atr_mult"),
                                                "analysis.exit.trail_atr_mult",
                                                0.1, 10.0, 1.0)),
        }

    gc = an.get("gate")
    if not isinstance(gc, dict):
        an["gate"] = copy.deepcopy(DEFAULTS["analysis"]["gate"])
    else:
        if gc.get("measure") not in ("tp1", "weighted", "max"):
            if gc.get("measure") is not None:
                print(f"[config] analysis.gate.measure={gc.get('measure')!r} 不认，"
                      f"改用 tp1")
            gc["measure"] = "tp1"
        gc["min_rr"] = _coerce_num(gc.get("min_rr"), "analysis.gate.min_rr",
                                   0.0, 20.0, 1.0)
        gc["min_target_rr"] = _coerce_num(gc.get("min_target_rr"),
                                          "analysis.gate.min_target_rr", 0.0, 20.0, 0.8)
        gw = gc.get("weights")
        if not isinstance(gw, list) or not any(
                isinstance(x, (int, float)) for x in gw):
            gc["weights"] = [0.5, 0.3, 0.2]

    out["http_timeout_sec"] = _coerce_num(out.get("http_timeout_sec"),
                                          "http_timeout_sec", 1, 120, 12.0)
    out["http_retries"] = _coerce_int(out.get("http_retries"), "http_retries", 0, 5, 2)

    out["symbols"] = _coerce_list(out.get("symbols"), "symbols", DEFAULTS["symbols"])
    if not isinstance(out.get("profiles"), dict):
        out["profiles"] = {}
    if not isinstance(out.get("active_profile"), str):
        out["active_profile"] = ""

    sc = _section(out, "schedule", DEFAULTS["schedule"])
    sc["interval_sec"] = _coerce_int(sc.get("interval_sec"), "schedule.interval_sec",
                                     30, 86400, 300)
    sc["symbols"] = _coerce_list(sc.get("symbols"), "schedule.symbols",
                                 DEFAULTS["schedule"]["symbols"])
    # reversion 无正期望（完整历史回测为负），不放进定时任务可选模式：
    # 它仍可用于 backtest/手动 analyze，但不该被排程进实盘扫描。
    sc["mode"] = sc.get("mode") if sc.get("mode") in SCHEDULABLE_MODES else DEFAULT_MODE
    uni = _section(sc, "universe", DEFAULTS["schedule"]["universe"])
    uni["batch_size"] = _coerce_int(uni.get("batch_size"),
                                    "schedule.universe.batch_size", 1, 500, 20)
    uni["workers"] = _coerce_int(uni.get("workers"),
                                 "schedule.universe.workers", 1, 32, 4)
    _section(uni, "filters", {})
    ver = _section(sc, "verify", DEFAULTS["schedule"]["verify"])
    ver["max_per_run"] = _coerce_int(ver.get("max_per_run"),
                                     "schedule.verify.max_per_run", 1, 5000, 50)
    sh = _section(sc, "shadow", DEFAULTS["schedule"]["shadow"])
    sh["enabled"] = bool(sh.get("enabled", True))
    sh["variants"] = _coerce_list(sh.get("variants"), "schedule.shadow.variants",
                                  DEFAULTS["schedule"]["shadow"]["variants"])
    sh["every_runs"] = _coerce_int(sh.get("every_runs"),
                                   "schedule.shadow.every_runs", 1, 1000, 6)
    if not isinstance(sh.get("path"), str):
        sh["path"] = ""
    ded = _section(sc, "dedupe", DEFAULTS["schedule"]["dedupe"])
    ded["cooldown_sec"] = _coerce_num(ded.get("cooldown_sec"),
                                      "schedule.dedupe.cooldown_sec", 0, 86400, 3600.0)
    ded["score_delta"] = _coerce_num(ded.get("score_delta"),
                                     "schedule.dedupe.score_delta", 0, 100, 5.0)

    st = _section(out, "storage", DEFAULTS["storage"])
    st["keep_days"] = _coerce_int(st.get("keep_days"), "storage.keep_days", 1, 3650, 30)
    bk = _section(out, "backup", DEFAULTS["backup"])
    bk["keep"] = _coerce_int(bk.get("keep"), "backup.keep", 1, 999, 14)
    bk["hours"] = _coerce_num(bk.get("hours"), "backup.hours", 0.1, 8760, 24.0)
    _section(out, "notify", DEFAULTS["notify"])
    _section(out, "universe", DEFAULTS["universe"])
    _section(out, "auth", DEFAULTS["auth"])
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
    """原子写 config.json：先写临时文件再替换，崩溃/并发写不会把用户配置打回默认。

    直接 open(..., "w") 里写入，写一半崩掉就只剩一个残缺 JSON ——
    下次启动 load_config 会读出一行 "读取失败，使用默认配置"，
    用户的通知 token、账户参数、全部方案静默丢失。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, CONFIG_PATH)


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
