"""命令行入口：python -m iabot

子命令：
    serve                      启动网页控制台（默认）
    analyze BTC-USDT           命令行输出单个合约的行情与交易计划
    scan BTC-USDT,ETH-USDT     批量扫描并排序
    check                      网络 / 证书 / 行情接口自检
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser

from . import __version__, netutil
from .advisor import Advisor
from .config import DEFAULT_MODE, ensure_example, load_config
from .server import build_server, make_app
from .signals import MODES
from .symbols import to_contract
from .universe import is_all_token, merge_spec, select

# --------------------------------------------------------------------------
# 纯文本输出
# --------------------------------------------------------------------------

def _num(v, d=2):
    return "--" if v is None else f"{v:,.{d}f}"


def _d(v):
    """字段可能取到了但值是 None，打印成 -- 更好看。"""
    return "--" if v is None else v


def _headline(plan: dict) -> str:
    """一行结论，和前端顶部那条横幅口径一致。"""
    sym = plan.get("symbol", "?")
    score = plan.get("score") or 0.0
    direction = plan.get("direction")
    if direction == "long":
        return f"{sym} 做多（{score:+.0f}，置信度{plan.get('confidence', '--')}）"
    if direction == "short":
        return f"{sym} 做空（{score:+.0f}，置信度{plan.get('confidence', '--')}）"
    return f"{sym} 观望（{score:+.0f}）"


def render_plan(plan: dict, *, verbose: bool = True) -> str:
    """把一个计划渲染成终端可读的多行文本。"""
    sym = plan.get("symbol", "?")
    mode = MODES.get(plan.get("mode"), {}).get("label", plan.get("mode", ""))
    d = 1 if (plan.get("price") or 0) > 1000 else (4 if (plan.get("price") or 0) > 1 else 8)
    line = "=" * 66
    out = [line, f"  {sym}  |  {mode}  |  评分 {plan.get('score', 0):+.1f}  |  "
                 f"价格 {_num(plan.get('price'), d)}", line]

    m = plan.get("market") or {}
    if m:
        out.append(f"  市场状态: {_d(m.get('regime'))}   ADX {_d(m.get('adx'))}   "
                   f"ATR({_d(m.get('atr_period'))}) {_d(m.get('atr_pct'))}%   "
                   f"24h {_d(m.get('change_24h_pct'))}%")
    out.append(f"  >>> {_headline(plan)}")

    direction = plan.get("direction")
    if direction and direction != "wait":
        out.append("")
        out.append(f"  方向      : {'做多 LONG' if direction == 'long' else '做空 SHORT'}"
                   f"（置信度 {_d(plan.get('confidence'))}）")
        out.append(f"  入场区间  : {_num(plan.get('entry_low'), d)} – {_num(plan.get('entry_high'), d)}")
        out.append(f"  止损      : {_num(plan.get('stop'), d)}"
                   f"（{_num(plan.get('stop_pct'), 2)}%）")
        out.append(f"  盈亏比    : {_num(plan.get('rr'), 2)}")
        for t in plan.get("targets") or []:
            out.append(f"    {t['label']:<4}: {_num(t['price'], d)}  RR {_num(t.get('rr'), 2)}"
                       f"   {t.get('source', '')}")
        out.append(f"  失效条件  : {plan.get('invalidation', '--')}")
    gate = plan.get("gate") or {}
    if gate.get("blocked"):
        cn = "做多" if gate.get("would_be") == "long" else "做空"
        why = ("前方没有像样的空间（价格卡在关键位中间）"
               if gate.get("reason") == "no_target"
               else f"第一目标盈亏比只有 {_num(gate.get('rr'), 2)}，低于 {_num(gate.get('need'), 1)}")
        out.append("")
        out.append(f"  !! 评分 {gate.get('score', 0):+.1f} 其实指向{cn}，但{why} → 转为观望。")
        out.append("     评分只决定方向多强，盈亏比决定这个位置值不值得进。")

    if plan.get("entry_note"):
        out.append("")
        out.append(f"  说明      : {plan['entry_note']}")

    s = plan.get("sizing") or {}
    out.append("")
    if s.get("applicable"):
        out.append(f"  仓位      : {s['contracts']} 张（{_num(s.get('position_coin'), 6)} 币）"
                   f"  名义 {_num(s.get('notional_usdt'))} U")
        out.append(f"  保证金    : {_num(s.get('margin_usdt'))} U"
                   f"（账户 {_num(s.get('margin_pct_of_equity'), 1)}%）"
                   f"  {s.get('leverage')}x")
        if s.get("liquidation_price"):
            cap = "（已因强平风险自动下调）" if s.get("leverage_capped") else ""
            out.append(f"  强平价    : {_num(s.get('liquidation_price'), d)}{cap}")
        out.append(f"  止损亏损  : {_num(s.get('loss_if_stopped_usdt'))} U"
                   f"（{_num(s.get('loss_pct_of_equity'), 2)}%）"
                   f"  手续费约 {_num(s.get('fee_estimate_usdt'), 3)} U")
        for t in s.get("targets") or []:
            out.append(f"    若到 {t['label']} {_num(t['price'], d)}: "
                       f"+{_num(t.get('profit_usdt'))} U"
                       f"（账户 {_num(t.get('profit_pct_of_equity'), 1)}%）")
    else:
        out.append(f"  仓位      : 不给（{s.get('note', '--')}）")

    if verbose and plan.get("factors"):
        out.append("")
        out.append("  因子明细:")
        for f in plan["factors"]:
            out.append(f"    {f['label']:<8} {f['score']:+.2f} × {f['weight']:.2f} = "
                       f"{f['contribution']:+.3f}   {f['detail']}")

    levels = plan.get("levels") or {}
    if verbose and (levels.get("resistance") or levels.get("support")):
        out.append("")
        out.append("  关键价位:")
        for l in (levels.get("resistance") or [])[:4]:
            out.append(f"    阻力 {_num(l['price'], d):>12}  权重 {l['weight']:.2f}  {l['source']}")
        for l in (levels.get("support") or [])[:4]:
            out.append(f"    支撑 {_num(l['price'], d):>12}  权重 {l['weight']:.2f}  {l['source']}")

    for w in plan.get("warnings") or []:
        out.append(f"  ! {w}")
    for n in plan.get("notes") or []:
        out.append(f"  - {n}")
    out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def _account_from(args) -> dict:
    acc = {}
    if getattr(args, "equity", None):
        acc["equity_usdt"] = float(args.equity)
    if getattr(args, "leverage", None):
        acc["preferred_leverage"] = int(args.leverage)
    if getattr(args, "risk", None):
        acc["risk_pct_per_trade"] = float(args.risk)
    return acc


def _save_plans(cfg: dict, plans: list[dict]) -> int:
    """把一批计划写进信号历史库（只写有明确方向的），返回条数。"""
    from .store import SignalStore
    st = SignalStore((cfg.get("storage") or {}).get("db_file", "data/signals.db"),
                     int((cfg.get("storage") or {}).get("keep_days", 30)))
    n = 0
    try:
        for p in plans:
            if p.get("direction") in ("long", "short"):
                if st.save(p, only_on_signal=True) is not None:
                    n += 1
    finally:
        st.close()
    return n


def cmd_analyze(args) -> int:
    cfg = load_config()
    adv = Advisor(cfg)
    acc = _account_from(args) or None
    try:
        plan = adv.plan_payload(args.symbol, mode=args.mode, force=True, account=acc)
    except Exception as exc:
        print(f"[错误] 拉取失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "save", False):
        from .store import SignalStore
        st = SignalStore((cfg.get("storage") or {}).get("db_file", "data/signals.db"),
                         int((cfg.get("storage") or {}).get("keep_days", 30)))
        try:
            rowid = st.save(plan)
        finally:
            st.close()
        print("[已保存] 信号已写入历史库" + (f"（第 {rowid} 条）" if rowid else ""),
              file=sys.stderr)
    if getattr(args, "paper", False):
        _open_paper_from_plan(cfg, plan)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(render_plan(plan, verbose=not args.brief))
    return 0


def _open_paper_from_plan(cfg: dict, plan: dict) -> None:
    """按分析结果开一笔模拟仓（入场用现价，止损/止盈来自计划）。"""
    from .store import SignalStore
    d = plan.get("direction")
    if d not in ("long", "short"):
        print("[模拟仓] 当前是观望/被闸门拦下，未开仓。", file=sys.stderr)
        return
    s = plan.get("sizing") or {}
    price = plan.get("price")
    coin = s.get("position_coin")
    if not price or not coin:
        print("[模拟仓] 缺少价格或仓位大小，未开仓。", file=sys.stderr)
        return
    tps = plan.get("targets") or []
    st = SignalStore((cfg.get("storage") or {}).get("db_file", "data/signals.db"),
                     int((cfg.get("storage") or {}).get("keep_days", 30)))
    try:
        if s.get("liquidation_price") and s.get("leverage_capped"):
            print(f"[模拟仓] 提示：杠杆已因强平风险从 {cfg.get('account', {}).get('preferred_leverage')}x "
                  f"降到 {s.get('leverage')}x", file=sys.stderr)
        pid = st.open_position(
            symbol=plan.get("symbol"), direction=d, entry_price=price,
            size_coin=coin, leverage=s.get("leverage") or 1,
            stop=plan.get("stop"),
            tp1=(tps[0]["price"] if len(tps) > 0 else None),
            tp2=(tps[1]["price"] if len(tps) > 1 else None),
            tp3=(tps[2]["price"] if len(tps) > 2 else None),
            note=f"{plan.get('profile') or '手动'} 分析开仓")
    finally:
        st.close()
    print(f"[模拟仓] 已开 {d} {plan.get('symbol')} @ {price}"
          f"（#{pid}，止损 {plan.get('stop')}）", file=sys.stderr)


def _fmt_amt(v) -> str:
    """雷达表里的价格/成交额：够大就带千分位，够小就多给几位小数。"""
    if v is None:
        return "--"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "--"
    if abs(n) >= 1000:
        return f"{n:,.0f}"
    if abs(n) >= 1:
        return f"{n:,.2f}"
    # 不用 %g：PEPE 这类亚分币种会被写成 4.19e-06，不如直接摊开小数位
    txt = f"{n:.10f}".rstrip("0").rstrip(".")
    return txt or "0"


def cmd_scan(args) -> int:
    cfg = load_config()
    adv = Advisor(cfg)
    acc = _account_from(args) or None
    tokens = [s for s in (args.symbols or "").split(",") if s.strip()]
    if not tokens:
        print("[错误] 没有可扫描的合约（想扫全市场就写 all）", file=sys.stderr)
        return 2

    override: dict = {}
    if args.top:
        override["max_symbols"] = args.top
    if args.min_turnover is not None:
        override["min_turnover_usdt"] = args.min_turnover
    if args.all_assets:
        override["asset_class"] = "all"
    if args.sort:
        override["sort"] = args.sort

    if getattr(args, "radar", False):
        rows = adv.radar()
        spec = merge_spec({**cfg, "universe": {**(cfg.get("universe") or {}), **override}})
        rows = select(rows, spec)
        print(f"全市场雷达：交易所共 {len(adv.client.all_tickers())} 个 USDT 永续，"
              f"筛选后 {len(rows)} 个\n")
        print(f"{'合约':<14}{'现价':>14}{'24h涨跌':>10}{'24h成交额':>16}  类别")
        for r in rows:
            chg = r.get("change_pct")
            print(f"{r['symbol']:<14}{_fmt_amt(r.get('price')):>14}"
                  f"{('%+.2f%%' % chg) if chg is not None else '--':>10}"
                  f"{_fmt_amt(r.get('turnover')):>16}  {r.get('asset_class')}")
        return 0

    # `all` 在这里被展开成具体合约列表；显式写出的合约永远优先保留
    syms = adv.expand_symbols(tokens, override=override or None)
    if not syms:
        print("[错误] 没有可扫描的合约（检查 --top / --min-turnover 是否筛得太狠）",
              file=sys.stderr)
        return 2

    workers = max(1, int(getattr(args, "workers", 4) or 4))
    if len(tokens) == 1 and is_all_token(tokens[0]):
        print(f"[全市场] 候选 {len(syms)} 个，并发 {workers} 分析中…", file=sys.stderr)
    t0 = time.time()

    def on_result(sym: str, plan: dict | None, err: str | None) -> None:
        if err:
            print(f"  [!!] {sym:<12} {err}", file=sys.stderr)
        elif plan:
            print(f"  [ok] {sym:<12} {plan['direction']:<5} "
                  f"{plan['score']:+7.1f}", file=sys.stderr)

    plans, errs = adv.scan(syms, mode=args.mode, account=acc,
                           workers=workers, on_result=on_result)
    if getattr(args, "save", False):
        saved = _save_plans(cfg, plans)
        print(f"[已保存] {saved} 条有方向的信号写入历史库", file=sys.stderr)
    print(f"[耗时] {time.time() - t0:.1f}s / {len(syms)} 个合约", file=sys.stderr)
    if args.json:
        print(json.dumps({"plans": plans, "errors": errs}, ensure_ascii=False, indent=2))
        return 0
    print()
    print(_scan_breadth(plans))
    for p in plans:
        print(render_plan(p, verbose=False))
    if errs:
        print(f"失败 {len(errs)} 个:", "; ".join(errs[:10]), file=sys.stderr)
    return 0


def _scan_breadth(plans: list[dict]) -> str:
    """批量扫描结果顶部的市场情绪一行（与网页口径一致）。"""
    longs = sum(1 for p in plans if p.get("direction") == "long")
    shorts = sum(1 for p in plans if p.get("direction") == "short")
    waits = sum(1 for p in plans if p.get("direction") == "wait")
    n_ls = longs + shorts
    if n_ls:
        senti = "偏多" if longs / n_ls >= 0.6 else ("偏空" if shorts / n_ls >= 0.6 else "中性")
    else:
        senti = "中性"
    scores = [p.get("score") or 0 for p in plans]
    avg = sum(abs(s) for s in scores) / len(scores) if scores else 0
    return (f"市场情绪: {senti}   {longs}多 / {shorts}空 / {waits}观望"
            f"   平均|评分| {avg:.1f}")


def cmd_check(args) -> int:
    cfg = load_config()
    ok = True
    cert = netutil.cert_info()
    print(f"[证书] 根证书 {cert['ca_count']} 个，校验={'开启' if cert['verifying'] else '关闭'} "
          f"来源={cert.get('bundle') or '系统默认'}")
    if cert["ca_count"] == 0:
        ok = False
        print("  ! 未找到根证书，HTTPS 无法验证（可在 config.json 设 tls_verify=false 临时绕过）")

    adv = Advisor(cfg)
    print("[域名] 健康探测（按顺序取第一个可用的）")
    for row in adv.health():
        flag = "OK " if row.get("ok") else "FAIL"
        ms = f"{row.get('ms', 0):.0f}ms" if row.get("ms") else "--"
        note = "" if row.get("ok") else f"  {row.get('error', '')[:80]}"
        print(f"  [{flag}] {row.get('base', '?'):<32} {ms}{note}")
        if row.get("current"):
            print("        ^ 当前使用")

    sym = args.symbol or (cfg.get("symbols") or ["BTC-USDT"])[0]
    try:
        t0 = time.time()
        snap = adv.client.snapshot(to_contract(sym), periods=cfg["analysis"]["tf_periods"],
                                   kline_size=120)
        ms = (time.time() - t0) * 1000
        if snap.price:
            print(f"[行情] {snap.symbol} 最新价 {snap.price}，{ms:.0f}ms，"
                  f"K线 {len(snap.klines)} 个周期，盘口 {len(snap.depth.get('bids', []))} 档")
        else:
            ok = False
            print(f"[行情] {snap.symbol} 没拿到价格：{snap.errors}")
        for e in snap.errors:
            print(f"  ! 扩展数据异常: {e}")
    except Exception as exc:
        ok = False
        print(f"[行情] 失败: {type(exc).__name__}: {exc}")

    # ---- 配置体检 ----
    print("[配置] 基本设置")
    auth = (cfg.get("auth") or {}).get("token")
    ntf = cfg.get("notify") or {}
    keep = (cfg.get("storage") or {}).get("keep_days", 30)
    host = cfg.get("host", "127.0.0.1")
    print(f"  监听 {host}  鉴权 {'Bearer 令牌' if auth else '本机免登录'}")
    if host not in ("127.0.0.1", "localhost", "::1") and not auth:
        print("  ! 非本机监听且未设 auth.token，局域网可访问")
    print(f"  历史保留 {keep} 天（自动清理）")
    print(f"  通知 {'开启' if ntf.get('enabled') else '关闭'}"
          f"（min|评分|>= {ntf.get('min_abs_score', 45)}）")
    nch = {k: ntf.get(k) for k in ("webhook", "telegram", "bark") if (ntf.get(k) or {}).get("url") or (ntf.get(k) or {}).get("token")}
    print(f"  通知渠道 {', '.join(nch) if nch else '未配置'}")
    if ntf.get("enabled") and not nch:
        print("  ! 通知已开启但未配置任何渠道，不会发出消息")

    print()
    print("诊断结果:", "全部通过 [OK]" if ok else "存在告警 [!]（见上方说明）")
    return 0 if ok else 1
def cmd_backtest(args) -> int:
    from .backtest import backtest
    from .market import period_seconds
    cfg = load_config()
    adv = Advisor(cfg)
    main_period = "15min" if args.mode == "intraday" else "1day"
    sym = to_contract(args.symbol)
    # 各周期按主周期时间跨度换算根数：15min 300 根只覆盖 3 天，回测一年日线
    # 就需要 365*24*4=35000 根 15min。给个上限，宁可早段少几个因子也不要缺数据。
    def _bars(period):
        if period == main_period:
            return args.bars
        ratio = period_seconds(main_period) / period_seconds(period)
        return min(4000, max(60, int(args.bars * ratio)))
    periods = cfg.get("analysis", {}).get("tf_periods") or ["1min", "5min", "15min", "60min", "1day"]
    try:
        snap = adv.client.snapshot(sym, periods=periods, kline_size=max(args.bars, 120))
        # 主周期 K 线数不够时按换算好的各自根数补齐
        for p in periods:
            if p == main_period:
                continue
            need = _bars(p)
            if len(snap.klines.get(p) or []) < need:
                try:
                    rows = adv.client.kline(sym, p, need)
                    # 只补不删：抓回空数组时保留原有的数据，别把好数据覆盖成空
                    if rows:
                        snap.klines[p] = rows
                except Exception:
                    pass
        # 回测要用"每个建仓点当时"的资金费率，这里补一段足够长的历史费率序列
        # （默认 90 条只覆盖约 30 天，长回测需要更多）。
        need_fh = min(2000, max(90, int(args.bars * 3)))
        try:
            snap.funding_history = adv.client.funding_history(sym, need_fh)
        except Exception:
            pass
    except Exception as exc:
        print(f"[错误] 拉取历史失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    t0 = time.time()
    bt_acc = _account_from(args)

    # ---- 走动样本外检验模式 ----
    if getattr(args, "wf", 0):
        from .walkforward import walk_forward
        out = walk_forward(snap, cfg, mode=args.mode, folds=args.wf,
                           step=args.step, warmup=args.warmup)
        ms = (time.time() - t0) * 1000
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("ok") else 1
        if not out.get("ok"):
            print(f"  数据不足：{out.get('reason', '未知')}"
                  f"（{out.get('bars', 0)} 根）")
            return 1
        print("=" * 66)
        print(f"  走动样本外 | {sym} | {MODES.get(args.mode, {}).get('label', args.mode)}"
              f" | {out['bars']} 根 | {args.wf} 折 | {ms:.0f}ms")
        print(f"  {'折':<3}{'起点':<12}{'单数':>6}{'期望R':>10}{'胜率':>8}  区间")
        import time as _t
        for f in out["folds"]:
            d = _t.strftime("%Y-%m-%d", _t.gmtime(f["start_ts"]))
            ci = f["expectancy_ci"]
            cis = f"[{ci[0]:+.2f},{ci[1]:+.2f}]" if ci else "--"
            print(f"  {f['index']:<3}{d:<12}{f['trades']:>6}{f['expectancy_r']:>+10.3f}"
                  f"{f['win_rate']*100:>7.1f}%  {cis}")
        p = out["pooled"]
        pci = p["expectancy_ci"]
        print(f"  正收益折    : {out['positive_folds']}/{out['usable_folds']}"
              f"（{out['positive_fold_frac']*100:.0f}%）")
        if pci:
            print(f"  合并期望    : {p['expectancy_r']:+.3f} R/笔（{p['trades']} 笔，"
                  f"95% 区间 [{pci[0]:+.3f},{pci[1]:+.3f}]）")
        else:
            print(f"  合并期望    : {p['expectancy_r']:+.3f} R/笔（{p['trades']} 笔，"
                  f"样本不足以给区间）")
        print(f"  结论        : {out['verdict']}")
        print("=" * 66)
        return 0

    res = backtest(snap, cfg, mode=args.mode, step=args.step,
                   warmup=args.warmup, max_points=2000,
                   equity_usdt=bt_acc.get("equity_usdt"),
                   risk_pct=bt_acc.get("risk_pct_per_trade"))
    ms = (time.time() - t0) * 1000
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print("=" * 66)
    if res.insufficient_data:
        # 残缺/不足的样本必须显式报出来，不能让人把 0 笔当成"策略不交易"
        why = "主周期 K 线不足"
        if res.missing_periods:
            why = f"这些周期没抓全: {'、'.join(res.missing_periods)}"
        print(f"  数据不足：{why}。")
        print(f"  这段回测没有产生任何可评估的结果（{res.bars} 根 {main_period}）。")
        print("=" * 66)
        return 1
    print(f"  回测 {sym}  |  {MODES.get(res.mode, {}).get('label', res.mode)}"
          f"  |  {res.bars} 根 {main_period} K线  |  {ms:.0f}ms")
    print(f"  建仓点       : {res.trades}（观望/被闸门拦下不计）")
    print(f"  胜 / 负      : {res.wins} / {res.losses}"
          f"（{res.timeouts} 根窗口内未了结）")
    print(f"  胜率         : {res.win_rate * 100:.1f}%")
    print(f"  期望值       : {res.expectancy_r:+.3f} R/笔"
          f"（已扣成本，正 = 长期按规则做有优势）")
    if res.expectancy_ci:
        lo, hi = res.expectancy_ci
        verdict = "显著" if res.significant else (
            "样本不足，不能判定" if len(res.trade_rs or []) < 30 else "不显著（跨 0）")
        print(f"  95% 区间     : [{lo:+.3f}, {hi:+.3f}] R/笔  t={res.t_stat:+.2f}"
              f"  -> {verdict}")
    else:
        print("  95% 区间     : 样本不足，无法估计")
    print(f"  平均盈亏 R   : 胜 {res.avg_win_r:.2f} / 负 {res.avg_loss_r:.2f}")
    print(f"  成本明细     : 手续费 {res.avg_fee_r:.3f} + 滑点 {res.avg_slippage_r:.3f}"
          f" + 资金费 {res.avg_funding_r:.3f} = {res.avg_cost_r:.3f} R/笔")
    print(f"  平均持仓     : {res.avg_holding_h:.1f} 小时")
    print(f"  最大浮盈/浮亏: {res.max_mfe:+.2f}% / {res.max_mae:+.2f}%")
    print(f"  累计 R       : {res.total_r:+.1f}（按 1R 单笔计算，曲线见 --json）")
    if res.equity_usdt:
        pnl_usdt = res.total_r * res.risk_amount_usdt
        print(f"  金额换算     : 初始 {res.equity_usdt:.0f} U / 单笔风险 {res.risk_amount_usdt:.1f} U"
              f" -> 期末约 {res.equity_usdt + pnl_usdt:.0f} U（{(pnl_usdt / res.equity_usdt) * 100:+.1f}%）")
    print("=" * 66)
    if getattr(args, "save", False):
        from .btlog import build_params, summarize_result
        from .store import SignalStore
        rid = st0 = None
        try:
            st0 = SignalStore((cfg.get("storage") or {}).get("db_file", "data/signals.db"),
                              int((cfg.get("storage") or {}).get("keep_days", 30)))
            params = build_params(cfg, symbol=sym, mode=args.mode,
                                  bars=args.bars, step=args.step, warmup=args.warmup,
                                  equity_usdt=res.equity_usdt, risk_pct=res.risk_pct)
            rid = st0.save_backtest_run(
                symbol=sym, mode=args.mode, params=params,
                result=summarize_result(res.to_dict()),
                profile=cfg.get("active_profile") or "", elapsed_ms=int(ms))
        finally:
            if st0:
                st0.close()
        print(f"[已保存] 回测记录已写入历史库（第 {rid} 条）", file=sys.stderr)
    return 0


def cmd_watch(args) -> int:
    """终端监控模式：循环扫描关注列表，信号变化时提示，并刷新模拟仓浮盈。

    Ctrl+C 退出。只在方向/评分显著变化时输出，避免每轮刷屏。
    """
    cfg = load_config()
    acc = _account_from(args) or None
    syms = [to_contract(s) for s in args.symbols.split(",") if s.strip()] \
        if args.symbols else [to_contract(s) for s in (cfg.get("symbols") or [])]
    if not syms:
        print("[错误] 没有可监控的合约（--symbols 或配置 symbols）", file=sys.stderr)
        return 2

    from .store import SignalStore
    st = SignalStore((cfg.get("storage") or {}).get("db_file", "data/signals.db"),
                     int((cfg.get("storage") or {}).get("keep_days", 30)))
    print(f"监控中: {', '.join(syms)}")
    print(f"模式 {args.mode} | 间隔 {args.interval}s | 每轮刷新模拟仓浮盈"
          f" | Ctrl+C 退出")
    print("-" * 62)

    # 每轮重建 Advisor，避免复用过期缓存；也让 force 生效
    prev: dict[str, tuple[str, float]] = {}   # symbol -> (direction, score)
    round_no = 0
    try:
        while True:
            round_no += 1
            adv = Advisor(cfg)
            changed = []
            for sym in syms:
                try:
                    plan = adv.plan_payload(sym, mode=args.mode, force=True, account=acc)
                except Exception as exc:
                    print(f"  [{time.strftime('%H:%M:%S')}] {sym} 分析失败: "
                          f"{type(exc).__name__}", file=sys.stderr)
                    continue
                d = plan.get("direction")
                sc = plan.get("score") or 0
                key = prev.get(sym)
                if key is None:
                    # 首轮只打印有方向的
                    if d in ("long", "short"):
                        changed.append((sym, plan, "新信号"))
                elif key[0] != d or abs(sc - key[1]) >= args.score_delta:
                    tag = "方向翻转" if key[0] != d else "评分明显变化"
                    changed.append((sym, plan, tag))
                prev[sym] = (d, sc)
                # 只有有方向的才落库（复用去重逻辑）
                if args.save and d in ("long", "short"):
                    try:
                        st.save(plan, only_on_signal=True)
                    except Exception:
                        pass

            # 输出变化
            ts = time.strftime("%H:%M:%S")
            for sym, plan, tag in changed:
                d = plan.get("direction")
                sc = plan.get("score") or 0
                cn = {"long": "做多", "short": "做空"}.get(d, "观望")
                rr = plan.get("rr")
                print(f"[{ts}] {sym:<12} {cn} ({sc:+.1f}) {tag}"
                      + (f" RR {rr:.2f}" if rr else ""))
            if not changed and round_no % args.every == 0:
                # 静默轮次，按间隔报个平安
                print(f"[{ts}] 第 {round_no} 轮：无变化"
                      f"（{len(syms)} 个合约 | 每 {args.every} 轮汇报一次）")

            # 刷新模拟仓浮盈
            if round_no % args.every == 0:
                opens = st.positions(status="open")
                if opens:
                    try:
                        from .paper import pnl_of
                        adv2 = Advisor(cfg)
                        for p in opens:
                            snap = adv2.fetch(p["symbol"], force=False)
                            pn = pnl_of(p, snap.price)
                            liq = " ⚠强平风险" if pn.get("liquidation_risk") else ""
                            print(f"    持仓 {p['symbol']:<12} "
                                  f"{pn['unrealized_pnl_usdt']:+.2f} U "
                                  f"({pn['pnl_of_margin_pct']:+.1f}% 保证金){liq}")
                    except Exception:
                        pass
            time.sleep(max(10, args.interval))
    except KeyboardInterrupt:
        print(f"\n已停止（共 {round_no} 轮）。")
    finally:
        st.close()
    return 0


def cmd_backup(args) -> int:
    """手动备份 / 查看 / 恢复信号数据库。"""
    from pathlib import Path
    from .store import SignalStore
    cfg = load_config()
    bcfg = cfg.get("backup") or {}
    bdir = Path(bcfg.get("dir") or "data/backups")
    st = SignalStore((cfg.get("storage") or {}).get("db_file", "data/signals.db"),
                     int((cfg.get("storage") or {}).get("keep_days", 30)))
    try:
        if args.list:
            st.close()
            if not bdir.is_dir():
                print("还没有任何备份。")
                return 0
            files = sorted(bdir.glob("signals-*.db"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if not files:
                print("还没有任何备份。")
                return 0
            print(f"备份目录 {bdir}（共 {len(files)} 份）:")
            for f in files:
                size_mb = f.stat().st_size / 1024 / 1024
                ts = time.strftime('%Y-%m-%d %H:%M',
                                   time.localtime(f.stat().st_mtime))
                print(f"  {f.name}   {size_mb:.2f} MB   {ts}")
            return 0
        path = st.backup_to(str(bdir / f"signals-{time.strftime('%Y%m%d-%H%M%S')}.db"))
        overview = st.stats_overview()
        print(f"已备份到 {path}")
        print(f"  信号 {overview['signals']} 条 | 持仓 {overview['positions']} 条 | "
              f"回测记录 {overview['backtest_runs']} 条")
        return 0
    finally:
        st.close()


def cmd_backtests(args) -> int:
    """列出历史回测记录，并排对比最近两次的参数差异。"""
    from .store import SignalStore
    st = SignalStore((load_config().get("storage") or {}).get("db_file", "data/signals.db"),
                     int((load_config().get("storage") or {}).get("keep_days", 30)))
    try:
        rows = st.query_backtest_runs(symbol=args.symbol, mode=args.mode,
                                      limit=args.limit)
    finally:
        st.close()
    if not rows:
        print("还没有回测记录。跑一次 backtest --save 就会记下来。")
        return 0
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"最近 {len(rows)} 条回测记录：")
    print()
    for r in rows:
        res = r.get("result") or {}
        prof = f" [{r['profile']}]" if r.get("profile") else ""
        print(f"  #{r['id']:<4} {r['symbol']:<12} {r['mode']:<9}{prof}")
        print(f"        {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['created_at']))}"
              f"   胜率 {res.get('win_rate', 0) * 100:.1f}%"
              f"   期望 {res.get('expectancy_r', 0):+.3f} R"
              f"   {res.get('trades', 0)} 笔"
              f"   期末 {res.get('final_equity_usdt', 0):.0f} U")
    # 最近两次参数差异提示
    if len(rows) >= 2:
        from .btlog import diff_params
        diffs = diff_params(rows[1].get("params") or {}, rows[0].get("params") or {})
        if diffs:
            print()
            print("  与上一条相比改了：")
            for d in diffs:
                print(f"    - {d}")
    return 0


def cmd_notify_test(args) -> int:
    """发送一条测试通知，验证 webhook / telegram / bark 配置是否可用。"""
    from .notify import Notifier
    cfg = load_config()
    n = Notifier(cfg)
    print(f"通知开关: {'开启' if n.enabled else '关闭'}")
    print(f"已配置渠道: "
          f"{'webhook ' if n._webhook.get('url') else ''}"
          f"{'telegram ' if (n._telegram.get('token') and n._telegram.get('chat_id')) else ''}"
          f"{'bark' if n._bark.get('url') else ''}".strip() or "(无)")
    if not n.enabled:
        print("  ! notify.enabled 还没打开，先在 config.json 里设为 true", file=sys.stderr)
        return 1
    if not n.configured:
        print("  ! 没有配置任何渠道（webhook.url / telegram.token+chat_id / bark.url）",
              file=sys.stderr)
        return 1
    sent = n.send("[iabot] 测试通知",
                  "这是一条测试消息。\n如果你收到了，说明通知配置正确。")
    if sent:
        print(f"已发送: {sent} 个渠道")
        return 0
    print(f"  ! 发送失败: {n.last_error}", file=sys.stderr)
    return 1


def cmd_serve(args) -> int:
    cfg = load_config()
    ensure_example()
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = int(args.port)
    if getattr(args, "symbols", None):
        cfg["symbols"] = [to_contract(s) for s in args.symbols.split(",") if s.strip()]
    if getattr(args, "mode", None):
        cfg.setdefault("schedule", {})["mode"] = args.mode

    stop_evt = threading.Event()
    app = make_app(cfg)
    app.start()
    httpd = build_server(app, cfg["host"], int(cfg["port"]))
    url_host = "127.0.0.1" if cfg["host"] in ("0.0.0.0", "::") else cfg["host"]
    url = f"http://{url_host}:{cfg['port']}/"

    if cfg["host"] not in ("127.0.0.1", "localhost", "::1") and not app.auth_token:
        print("  ! 正在监听非本机地址但未设置访问令牌，局域网内任何人都能操作这个服务"
              + "（可在 config.json 的 auth.token 里设置）", file=sys.stderr)

    print("=" * 62)
    print(f"  火币合约行情分析 · 投资建议  v{__version__}")
    print(f"  控制台   : {url}")
    print(f"  关注合约 : {', '.join(cfg.get('symbols') or [])}")
    print(f"  定时任务 : {'开启' if (cfg.get('schedule') or {}).get('enabled') else '关闭'}"
          f"（间隔 {(cfg.get('schedule') or {}).get('interval_sec')} 秒）")
    print(f"  账户设置 : {cfg.get('account', {}).get('equity_usdt')} U / "
          f"{cfg.get('account', {}).get('preferred_leverage')}x / "
          f"单笔风险 {cfg.get('account', {}).get('risk_pct_per_trade')}%")
    print(f"  访问控制 : {'Bearer 令牌' if app.auth_token else '本机免登录'}")
    print("  首次使用建议先跑: python -m iabot check")
    print("  停止: Ctrl+C")
    print("=" * 62)

    if cfg.get("open_browser", True) and not getattr(args, "no_browser", False):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3},
                         name="http", daemon=True)
    t.start()
    try:
        while not stop_evt.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        app.stop()
        httpd.shutdown()
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------

COMMANDS = ("serve", "analyze", "scan", "check", "backtest", "backtests",
            "backup", "watch", "notify-test")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="iabot", description="火币(HTX) USDT 本位永续合约行情分析与投资建议",
        epilog="不带子命令时默认执行 serve。")
    ap.add_argument("--version", action="version", version=f"iabot {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动网页控制台（默认）")
    p_serve.add_argument("--host", help="监听地址，默认取 config.json")
    p_serve.add_argument("--port", type=int, help="监听端口，默认取 config.json")
    p_serve.add_argument("--no-browser", action="store_true", help="启动时不自动开浏览器")
    p_serve.add_argument("--symbols", help="覆盖关注列表，逗号分隔")
    p_serve.add_argument("--mode", choices=list(MODES), help="定时任务使用的模式")

    p_an = sub.add_parser("analyze", help="分析单个合约")
    p_an.add_argument("symbol", help="合约代码，如 BTC-USDT（也支持 btc / 比特币）")
    p_an.add_argument("--mode", choices=list(MODES), default=DEFAULT_MODE,
                      help="intraday=日内（默认），swing=波段")
    p_an.add_argument("--equity", type=float, help="账户权益 USDT（默认取配置）")
    p_an.add_argument("--leverage", type=int, help="杠杆倍数（默认取配置）")
    p_an.add_argument("--risk", type=float, help="单笔风险百分比（默认取配置）")
    p_an.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_an.add_argument("--brief", action="store_true", help="不打印因子明细")
    p_an.add_argument("--save", action="store_true",
                      help="把本次信号写入历史库（默认 data/signals.db）")
    p_an.add_argument("--paper", action="store_true",
                      help="按本次计划开一笔模拟仓（入场/止损/止盈自动带入）")

    p_sc = sub.add_parser("scan", help="批量扫描多个合约，或全市场（all）")
    p_sc.add_argument("symbols",
                      help="逗号分隔，如 BTC-USDT,ETH-USDT；写 all/全部 则扫描全市场")
    p_sc.add_argument("--mode", choices=list(MODES), default=DEFAULT_MODE)
    p_sc.add_argument("--equity", type=float)
    p_sc.add_argument("--leverage", type=int)
    p_sc.add_argument("--risk", type=float)
    p_sc.add_argument("--json", action="store_true")
    p_sc.add_argument("--save", action="store_true",
                      help="把有方向的信号写入历史库（默认 data/signals.db）")
    p_sc.add_argument("--top", type=int,
                      help="全市场深挖多少个（按成交额取前 N，覆盖配置 universe.max_symbols）")
    p_sc.add_argument("--min-turnover", type=float,
                      help="全市场筛选的 24h 成交额下限（USDT），默认取配置 100000")
    p_sc.add_argument("--all-assets", action="store_true",
                      help="连代币化股票/大宗商品一起扫（默认只扫加密币）")
    p_sc.add_argument("--sort", choices=["turnover", "abs_change", "change"],
                      help="全市场候选排序方式，默认按成交额")
    p_sc.add_argument("--workers", type=int, default=4,
                      help="并发分析数（默认 4，调高容易撞限流）")
    p_sc.add_argument("--radar", action="store_true",
                      help="只打印全市场雷达表（现价/涨跌/成交额，1 个请求），不做深度分析")

    p_ck = sub.add_parser("check", help="网络 / 证书 / 行情接口 / 配置体检")
    p_ck.add_argument("--symbol", help="用哪个合约做行情测试，默认取配置里第一个")

    sub.add_parser("notify-test",
                   help="发一条测试通知，验证 notify 渠道配置是否可用")

    p_bk = sub.add_parser("backtest", help="历史回测：用真实 K 线评估策略胜率与期望值")
    p_bk.add_argument("symbol", help="合约代码，如 BTC-USDT")
    p_bk.add_argument("--mode", choices=list(MODES), default=DEFAULT_MODE)
    p_bk.add_argument("--step", type=int, default=10,
                      help="每隔多少根主周期 K 线构造一个建仓点（默认 10）")
    p_bk.add_argument("--warmup", type=int, default=60, help="跳过前多少根（指标预热）")
    p_bk.add_argument("--bars", type=int, default=2000,
                      help="取多少根主周期 K 线做历史（日内 15min / 波段 1day）。"
                           "接口上限 2000；低于 500 的样本不足以判断策略好坏")
    p_bk.add_argument("--equity", type=float, help="账户权益 USDT（默认取配置，用于金额换算）")
    p_bk.add_argument("--risk", type=float, help="单笔风险 %（默认取配置，用于金额换算）")
    p_bk.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_bk.add_argument("--wf", type=int, metavar="FOLDS", default=0,
                      help="改为走动样本外检验：把历史切 FOLDS 段逐段回测"
                           "（推荐 5）。回答的是\"这个 edge 是各段时间都成立，"
                           "还是只来自某一两段行情\"")
    p_bk.add_argument("--save", action="store_true",
                      help="把本次回测的参数与结果写入实验记录（可事后复现/对比）")

    p_bl = sub.add_parser("backtests", help="列出历史回测记录")
    p_bl.add_argument("--symbol", help="按合约过滤，如 BTC-USDT")
    p_bl.add_argument("--mode", choices=list(MODES), help="按模式过滤")
    p_bl.add_argument("--limit", type=int, default=20, help="显示最近多少条（默认 20）")
    p_bl.add_argument("--json", action="store_true", help="输出原始 JSON")

    p_bu = sub.add_parser("backup", help="备份信号数据库 / 查看已有备份")
    p_bu.add_argument("--list", action="store_true", help="只列出已有备份，不做新备份")

    p_wt = sub.add_parser("watch", help="终端监控：循环扫描，信号变化时提示（Ctrl+C 退出）")
    p_wt.add_argument("--symbols", help="逗号分隔，默认取配置里的关注列表")
    p_wt.add_argument("--mode", choices=list(MODES), default=DEFAULT_MODE)
    p_wt.add_argument("--interval", type=int, default=300, help="每轮间隔秒（默认 300）")
    p_wt.add_argument("--score-delta", type=float, default=8.0,
                      help="评分变化超过这个值才算变化（默认 8）")
    p_wt.add_argument("--every", type=int, default=6, help="每多少轮汇报一次静默状态")
    p_wt.add_argument("--equity", type=float)
    p_wt.add_argument("--risk", type=float)
    p_wt.add_argument("--save", action="store_true", help="把有方向的信号写入历史库")

    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 兼容 `python -m iabot --check / --version` 这种老写法
    if argv and argv[0] in ("--check", "-c"):
        argv[0] = "check"
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        # 传了裸合约代码就直接分析，例如 python -m iabot BTC-USDT
        argv = ["analyze"] + argv

    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        args = ap.parse_args(["serve"] + argv)

    if args.cmd == "analyze":
        return cmd_analyze(args)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    if args.cmd == "notify-test":
        return cmd_notify_test(args)
    if args.cmd == "backtests":
        return cmd_backtests(args)
    if args.cmd == "backup":
        return cmd_backup(args)
    if args.cmd == "watch":
        return cmd_watch(args)
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())