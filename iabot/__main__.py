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
from urllib.parse import quote

from . import __version__, netutil
from .advisor import Advisor
from .config import ROOT, ensure_example, load_config
from .server import build_server, make_app
from .signals import MODES
from .symbols import to_contract

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
        print(f"[已保存] 信号已写入历史库" + (f"（第 {rowid} 条）" if rowid else ""),
              file=sys.stderr)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(render_plan(plan, verbose=not args.brief))
    return 0


def cmd_scan(args) -> int:
    cfg = load_config()
    adv = Advisor(cfg)
    acc = _account_from(args) or None
    syms = [to_contract(s) for s in args.symbols.split(",") if s.strip()][:20]
    if not syms:
        print("[错误] 没有可扫描的合约", file=sys.stderr)
        return 2
    plans, errs = [], []
    for sym in syms:
        t0 = time.time()
        try:
            p = adv.plan_payload(sym, mode=args.mode, force=True, account=acc)
            plans.append(p)
            ms = (time.time() - t0) * 1000
            print(f"  [ok] {sym:<12} {p['direction']:<5} {p['score']:+7.1f}  "
                  f"{ms:.0f}ms", file=sys.stderr)
        except Exception as exc:
            errs.append(f"{sym}: {exc}")
            print(f"  [!!] {sym:<12} {type(exc).__name__}: {exc}", file=sys.stderr)
    plans.sort(key=lambda p: -abs(p.get("score") or 0))
    if getattr(args, "save", False):
        saved = _save_plans(cfg, plans)
        print(f"[已保存] {saved} 条有方向的信号写入历史库", file=sys.stderr)
    if args.json:
        print(json.dumps({"plans": plans, "errors": errs}, ensure_ascii=False, indent=2))
        return 0
    print()
    for p in plans:
        print(render_plan(p, verbose=False))
    if errs:
        print("失败:", "; ".join(errs), file=sys.stderr)
    return 0


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
            print(f"        ^ 当前使用")

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
                    snap.klines[p] = adv.client.kline(sym, p, need)
                except Exception:
                    pass
    except Exception as exc:
        print(f"[错误] 拉取历史失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    t0 = time.time()
    res = backtest(snap, cfg, mode=args.mode, step=args.step,
                   warmup=args.warmup, max_points=2000)
    ms = (time.time() - t0) * 1000
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print("=" * 66)
    print(f"  回测 {sym}  |  {MODES.get(res.mode, {}).get('label', res.mode)}"
          f"  |  {res.bars} 根 {main_period} K线  |  {ms:.0f}ms")
    print(f"  建仓点       : {res.trades}（观望/被闸门拦下不计）")
    print(f"  胜 / 负      : {res.wins} / {res.losses}"
          f"（{res.timeouts} 根窗口内未了结）")
    print(f"  胜率         : {res.win_rate * 100:.1f}%")
    print(f"  期望值       : {res.expectancy_r:+.3f} R/笔"
          f"（已扣成本，正 = 长期按规则做有优势）")
    print(f"  平均盈亏 R   : 胜 {res.avg_win_r:.2f} / 负 {res.avg_loss_r:.2f}")
    print(f"  成本明细     : 手续费 {res.avg_fee_r:.3f} + 滑点 {res.avg_slippage_r:.3f}"
          f" + 资金费 {res.avg_funding_r:.3f} = {res.avg_cost_r:.3f} R/笔")
    print(f"  平均持仓     : {res.avg_holding_h:.1f} 小时")
    print(f"  最大浮盈/浮亏: {res.max_mfe:+.2f}% / {res.max_mae:+.2f}%")
    print(f"  累计 R       : {res.total_r:+.1f}（按 1R 单笔计算，曲线见 --json）")
    print("=" * 66)
    return 0


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

COMMANDS = ("serve", "analyze", "scan", "check", "backtest")


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
    p_an.add_argument("--mode", choices=list(MODES), default="intraday",
                      help="intraday=日内（默认），swing=波段")
    p_an.add_argument("--equity", type=float, help="账户权益 USDT（默认取配置）")
    p_an.add_argument("--leverage", type=int, help="杠杆倍数（默认取配置）")
    p_an.add_argument("--risk", type=float, help="单笔风险百分比（默认取配置）")
    p_an.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_an.add_argument("--brief", action="store_true", help="不打印因子明细")
    p_an.add_argument("--save", action="store_true",
                      help="把本次信号写入历史库（默认 data/signals.db）")

    p_sc = sub.add_parser("scan", help="批量扫描多个合约")
    p_sc.add_argument("symbols", help="逗号分隔，如 BTC-USDT,ETH-USDT,SOL-USDT")
    p_sc.add_argument("--mode", choices=list(MODES), default="intraday")
    p_sc.add_argument("--equity", type=float)
    p_sc.add_argument("--leverage", type=int)
    p_sc.add_argument("--risk", type=float)
    p_sc.add_argument("--json", action="store_true")
    p_sc.add_argument("--save", action="store_true",
                      help="把有方向的信号写入历史库（默认 data/signals.db）")

    p_ck = sub.add_parser("check", help="网络 / 证书 / 行情接口自检")
    p_ck.add_argument("--symbol", help="用哪个合约做行情测试，默认取配置里第一个")

    p_bk = sub.add_parser("backtest", help="历史回测：用真实 K 线评估策略胜率与期望值")
    p_bk.add_argument("symbol", help="合约代码，如 BTC-USDT")
    p_bk.add_argument("--mode", choices=list(MODES), default="intraday")
    p_bk.add_argument("--step", type=int, default=10,
                      help="每隔多少根主周期 K 线构造一个建仓点（默认 10）")
    p_bk.add_argument("--warmup", type=int, default=60, help="跳过前多少根（指标预热）")
    p_bk.add_argument("--bars", type=int, default=300,
                      help="取多少根主周期 K 线做历史（日内 15min / 波断 1day）")
    p_bk.add_argument("--json", action="store_true", help="输出原始 JSON")

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
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())