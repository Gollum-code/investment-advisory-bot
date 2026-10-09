"""HTTP 服务：REST API + SSE 实时推送 + 静态前端。全部用标准库。"""

from __future__ import annotations

import json
import mimetypes
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, netutil, symbols as sym_mod
from .advisor import Advisor
from .config import load_config, save_config
from .scheduler import Scheduler
from .signals import FACTOR_DEFAULTS, MODES
from .profiles import ProfileStore, cfg_with_profile
from .store import SignalStore

WEB_DIR = Path(__file__).resolve().parent / "web"


def market_breadth(plans: list[dict]) -> dict:
    """把一批合约的计划聚合成市场情绪：多空 vs 观望、平均分、做多/做空名单。

    排除 direction=wait 的信号再算比例，避免大量观望把情绪稀释成"中性"。
    """
    longs = [p for p in plans if p.get("direction") == "long"]
    shorts = [p for p in plans if p.get("direction") == "short"]
    waits = [p for p in plans if p.get("direction") == "wait"]
    scores = [p.get("score") or 0 for p in plans]
    n_ls = len(longs) + len(shorts)
    if n_ls:
        bull = round(len(longs) / n_ls, 3)
        bear = round(len(shorts) / n_ls, 3)
    else:
        bull = bear = 0.0
    sentiment = ("偏多" if bull >= 0.6 else ("偏空" if bear >= 0.6 else "中性"))
    return {
        "scanned": len(plans),
        "longs": len(longs), "shorts": len(shorts), "waits": len(waits),
        "bull_ratio": bull, "bear_ratio": bear,
        "avg_abs_score": round(sum(abs(s) for s in scores) / len(scores), 1) if scores else 0.0,
        "sentiment": sentiment,
        "scores": {p.get("symbol"): p.get("score") for p in plans},
    }


class App:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.advisor = Advisor(cfg)
        self.store = SignalStore((cfg.get("storage") or {}).get("db_file", "data/signals.db"),
                                 int((cfg.get("storage") or {}).get("keep_days", 30)))
        self.scheduler = Scheduler(self.advisor, self.store, cfg)
        self.started_at = time.time()
        self.auth_token = (cfg.get("auth") or {}).get("token") or ""
        self.log_file = cfg.get("log_file") or ""

    def authorized(self, hdrs, token_hint: str | None = None) -> bool:
        """Bearer token / X-Iabot-Token 请求头 == 配置里的 token 才算通过。

        token_hint 给 SSE 用：EventSource 不能带请求头，只能把 token 放 query。
        """
        if not self.auth_token:
            return True
        token = (hdrs.get("Authorization") or "")[len("Bearer "):]
        if token == self.auth_token:
            return True
        if hdrs.get("X-Iabot-Token") == self.auth_token:
            return True
        return token_hint == self.auth_token

    def start(self) -> None:
        try:
            self.store.purge_old()
        except Exception:
            pass
        if (self.cfg.get("schedule") or {}).get("enabled"):
            self.scheduler.start()
        # 后台把行情域名先探一遍：串行探测遇上连不通的域名要等一个超时，
        # 不预热的话第一个 /api/analyze 会白白卡十几秒。
        threading.Thread(target=self._warmup, name="iabot-warmup", daemon=True).start()

    def _warmup(self) -> None:
        try:
            self.advisor.health()
        except Exception:
            pass

    def stop(self) -> None:
        self.scheduler.stop()
        self.store.close()

    def _account(self, q: dict) -> dict | None:
        acc = {}
        if q.get("equity"):
            acc["equity_usdt"] = float(q["equity"])
        if q.get("leverage"):
            acc["preferred_leverage"] = int(q["leverage"])
            acc["max_leverage"] = max(int(q["leverage"]), int(
                (self.cfg.get("account") or {}).get("max_leverage", 10)))
        if q.get("risk_pct"):
            acc["risk_pct_per_trade"] = float(q["risk_pct"])
        return acc or None


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"iabot/{__version__}"
        protocol_version = "HTTP/1.1"

        # ---------------- 基础工具 ----------------
        def log_message(self, fmt, *args):
            # 访问日志：默认静音；配置了 log_file 才写（按天滚动，避免无限膨胀）
            f = app.log_file
            if not f:
                return
            try:
                line = "%s - %s" % (self.address_string(),
                                    fmt % args) + "\n"
                with open(f, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception:
                pass

        def handle_one_request(self):
            # 浏览器关标签页 / 前端断开 SSE 时连接会被直接掐断，
            # 这会抛 ConnectionAbortedError，默认实现会打一大坨堆栈。
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def _json(self, obj, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _err(self, msg: str, code: int = 400, **extra):
            self._json({"ok": False, "error": msg, **extra}, code)

        def _static(self, rel: str):
            p = (WEB_DIR / rel.lstrip("/")).resolve()
            if not str(p).startswith(str(WEB_DIR.resolve())) or not p.is_file():
                return self._err("not found", 404)
            ctype, _ = mimetypes.guess_type(str(p))
            data = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype or 'application/octet-stream'}"
                             + ("; charset=utf-8" if ctype and ctype.startswith("text") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        # ---------------- GET ----------------
        def do_GET(self):
            u = urlparse(self.path)
            path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
            if path.startswith("/api/"):
                # SSE 只能通过 query 带 token（EventSource 发不了请求头）
                hint = q.get("token") if path == "/api/stream" else None
                if not app.authorized(self.headers, hint):
                    return self._err("需要访问令牌（Authorization: Bearer <token>）", 401)
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path in ("/app.js", "/style.css", "/favicon.ico"):
                    return self._static(path.lstrip("/"))
                if path.startswith("/api/"):
                    return self._api_get(path, q)
                return self._err("not found", 404)
            except ValueError as exc:
                # 查询参数解析失败（比如 limit=abc）是客户端的问题，不该打 500
                return self._err(f"参数错误: {exc}", 400)
            except BrokenPipeError:
                pass
            except Exception as exc:
                traceback.print_exc()
                self._err(f"{type(exc).__name__}: {exc}", 500)

        def _api_get(self, path: str, q: dict):
            if path == "/api/meta":
                return self._json({
                    "ok": True, "version": __version__,
                    "modes": [{"key": k, "label": v["label"]} for k, v in MODES.items()],
                    "symbols": sym_mod.catalog_payload(),
                    "watchlist": app.cfg.get("symbols") or [],
                    "periods": ["1min", "5min", "15min", "30min", "60min", "4hour", "1day"],
                    "auth_required": bool(app.auth_token),
                    "defaults": {
                        "account": app.cfg.get("account"),
                        "analysis": app.cfg.get("analysis"),
                        "schedule": app.cfg.get("schedule"),
                    },
                    "cert": netutil.cert_info(),
                    "uptime_sec": round(time.time() - app.started_at, 1),
                })

            if path == "/api/health":
                return self._json({"ok": True, "bases": app.advisor.health()})

            if path == "/api/analyze":
                sym = q.get("symbol") or "BTC-USDT"
                mode = q.get("mode") or "intraday"
                force = q.get("force") == "1"
                return self._json({"ok": True, **app.advisor.full_payload(
                    sym, mode=mode, force=force, account=app._account(q))})

            if path == "/api/plan":
                sym = q.get("symbol") or "BTC-USDT"
                return self._json({"ok": True, "plan": app.advisor.plan_payload(
                    sym, mode=q.get("mode") or "intraday",
                    force=q.get("force") == "1", account=app._account(q))})

            if path == "/api/scan":
                raw = q.get("symbols") or "BTC-USDT"
                syms = [s for s in raw.split(",") if s.strip()][:20]
                mode = q.get("mode") or "intraday"
                only_sig = q.get("only_signal") == "1"
                out, errs = [], []
                for s in syms:
                    try:
                        p = app.advisor.plan_payload(s, mode=mode,
                                                     account=app._account(q))
                        if not only_sig or p.get("direction") != "wait":
                            out.append(p)
                    except Exception as exc:
                        errs.append(f"{s}: {type(exc).__name__}: {exc}")
                out.sort(key=lambda p: -abs(p.get("score") or 0))
                return self._json({"ok": True, "plans": out, "errors": errs,
                                   "breadth": market_breadth(out),
                                   "ts": time.time()})

            if path == "/api/breadth":
                raw = q.get("symbols") or ",".join(app.cfg.get("symbols") or ["BTC-USDT"])
                syms = [s for s in raw.split(",") if s.strip()][:30]
                mode = q.get("mode") or "intraday"
                plans, errs = [], []
                for s in syms:
                    try:
                        plans.append(app.advisor.plan_payload(s, mode=mode))
                    except Exception as exc:
                        errs.append(f"{s}: {type(exc).__name__}: {exc}")
                return self._json({"ok": True, "breadth": market_breadth(plans),
                                   "errors": errs, "ts": time.time()})

            if path == "/api/history":
                lim = min(500, int(q.get("limit") or 100))
                rows = app.store.query(symbol=q.get("symbol"), mode=q.get("mode"),
                                       direction=q.get("direction"), limit=lim,
                                       with_meta=True)
                return self._json({"ok": True, "rows": rows, "count": len(rows)})

            if path == "/api/history/export":
                lim = min(5000, int(q.get("limit") or 5000))
                text = app.store.dump_jsonl(symbol=q.get("symbol"), mode=q.get("mode"),
                                            direction=q.get("direction"), limit=lim)
                data = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="signals.jsonl"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/api/stats":
                return self._json({"ok": True, "stats": app.store.stats(
                    int(q.get("hours") or 24))})

            if path == "/api/win-stats":
                days = q.get("days")
                return self._json({"ok": True, "win": app.store.win_stats(
                    days=int(days) if days else None),
                    "outcomes": app.store.outcome_counts()})

            if path == "/api/weights":
                an = app.cfg.get("analysis") or {}
                cur = an.get("factor_weights") or {}
                merged = {name: float(cur.get(name, w)) for name, w in FACTOR_DEFAULTS.items()}
                return self._json({"ok": True, "defaults": FACTOR_DEFAULTS,
                                   "weights": merged, "customized": bool(cur),
                                   "score_threshold": float(an.get("score_threshold") or 30),
                                   "resonance_enabled": bool((an.get("resonance") or {}).get("enabled"))})

            if path == "/api/profiles":
                ps = ProfileStore(app.cfg)
                return self._json({"ok": True, "profiles": ps.list(),
                                   "active": ps.active, "current": ps.current_params()})

            if path == "/api/positions":
                from .paper import pnl_of
                sym = q.get("symbol")
                want = q.get("status") or None
                rows = app.store.positions(status=want, symbol=sym)
                # 用当前价补浮动盈亏（现价拿不到就只返回原始记录）
                marks = {}
                for r in rows:
                    marks.setdefault(r["symbol"], None)
                for s in list(marks):
                    try:
                        snap = app.advisor.fetch(s, force=False)
                        marks[s] = snap.price
                    except Exception:
                        marks[s] = None
                for r in rows:
                    r["pnl"] = pnl_of(r, marks.get(r["symbol"]))
                    r["mark_price"] = marks.get(r["symbol"])
                return self._json({"ok": True, "positions": rows, "count": len(rows)})

            if path == "/api/report":
                from .report import render_report
                sym = q.get("symbol") or "BTC-USDT"
                mode = q.get("mode") or "intraday"
                try:
                    payload = app.advisor.full_payload(sym, mode=mode)
                except Exception as exc:
                    return self._err(f"分析失败: {exc}", 502)
                plan = payload["plan"]
                snap = payload["snapshot"]
                candles = (snap.get("klines") or {}).get("15min" if mode == "intraday" else "1day") or []
                win = app.store.win_stats(days=30)
                html_text = render_report(plan, snap, candles, symbol=sym, mode=mode,
                                          win_stats=win)
                data = html_text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 f'inline; filename="report-{sym}.html"')
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/api/schedule":
                return self._json({"ok": True, "schedule": app.scheduler.status()})

            if path == "/api/config":
                return self._json({"ok": True, "config": app.cfg})

            if path == "/api/stream":
                return self._sse()

            return self._err("unknown api", 404)

        # ---------------- SSE ----------------
        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            q: queue.Queue = queue.Queue(maxsize=200)

            def push(event: dict):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

            app.scheduler.subscribe(push)
            try:
                self._sse_write({"type": "hello", "ts": time.time(), "version": __version__})
                last_ping = time.time()
                while True:
                    try:
                        ev = q.get(timeout=2.0)
                        self._sse_write(ev)
                    except queue.Empty:
                        if time.time() - last_ping > 15:
                            last_ping = time.time()
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.scheduler.unsubscribe(push)

        def _sse_write(self, ev: dict):
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        # ---------------- POST handlers ----------------
        def _handle_profiles(self, body: dict):
            ps = ProfileStore(app.cfg)
            action = (body.get("action") or "").strip()
            name = (body.get("name") or "").strip()
            if action == "save":
                p = ps.save(name,
                            factor_weights=body.get("factor_weights") or {},
                            score_threshold=body.get("score_threshold") or 30,
                            mode=body.get("mode") or "intraday",
                            note=body.get("note") or "")
                # 手动保存当前参数 -> 缓存里的旧计划按旧权重算的，全部作废
                app.advisor.invalidate()
                save_config(app.cfg)
                return self._json({"ok": True, "profile": p})
            if action == "delete":
                ok = ps.delete(name)
                save_config(app.cfg)
                return self._json({"ok": True, "deleted": ok})
            if action == "apply":
                p = ps.apply(name)
                app.advisor.invalidate()
                save_config(app.cfg)
                return self._json({"ok": True, "profile": p,
                                   "applied": True})
            return self._err("action 必须是 save/delete/apply", 400)

        def _profiles_backtest(self, body: dict):
            from .backtest import backtest
            from .symbols import to_contract as _to_contract

            ps = ProfileStore(app.cfg)
            name = (body.get("name") or "").strip() or None
            if name:
                profile = ps.get(name)
                if not profile:
                    return self._err(f"方案不存在: {name}", 404)
            else:
                profile = None  # 用当前运行时参数
            symbol = _to_contract(body.get("symbol") or "BTC-USDT")
            mode = body.get("mode") or (profile or {}).get("mode") or "intraday"
            bars = max(120, min(500, int(body.get("bars") or 300)))
            step = max(1, min(50, int(body.get("step") or 10)))
            prof_cfg = cfg_with_profile(app.cfg, profile)
            periods = (app.cfg.get("analysis") or {}).get("tf_periods") or ["60min", "1day"]
            try:
                snap = app.advisor.client.snapshot(symbol, periods=periods,
                                                   kline_size=bars)
            except Exception as exc:
                return self._err(f"拉取历史失败: {exc}", 502)
            import time as _t
            t0 = _t.time()
            res = backtest(snap, prof_cfg, mode=mode, step=step, max_points=2000)
            return self._json({"ok": True, "name": name, "profile": profile,
                               "result": res.to_dict(), "ms": round((_t.time() - t0) * 1000)})

        def _handle_position(self, body: dict):
            from .paper import pnl_of
            from .symbols import to_contract as _tc

            action = (body.get("action") or "").strip()
            if action == "open":
                sym = _tc(body.get("symbol") or "BTC-USDT")
                direction = body.get("direction") or "long"
                entry = float(body.get("entry_price") or 0)
                size = float(body.get("size_coin") or 0)
                if entry <= 0 or size <= 0:
                    return self._err("entry_price 与 size_coin 必须 > 0", 400)
                lev = float(body.get("leverage") or 1.0)
                pos_id = app.store.open_position(
                    symbol=sym, direction=direction, entry_price=entry,
                    size_coin=size, leverage=lev,
                    stop=body.get("stop"), tp1=body.get("tp1"),
                    tp2=body.get("tp2"), tp3=body.get("tp3"),
                    note=body.get("note") or "")
                return self._json({"ok": True, "id": pos_id,
                                   "position": app.store.position(pos_id)})
            if action == "close":
                pos_id = int(body.get("id") or 0)
                price = float(body.get("close_price") or 0)
                if price <= 0:
                    return self._err("close_price 必须 > 0", 400)
                app.store.close_position(pos_id, price=price,
                                        reason=body.get("reason") or "manual")
                return self._json({"ok": True, "position": app.store.position(pos_id)})
            if action == "delete":
                pos_id = int(body.get("id") or 0)
                ok = app.store.delete_position(pos_id)
                return self._json({"ok": True, "deleted": ok})
            return self._err("action 必须是 open/close/delete", 400)

        # ---------------- POST ----------------
        def do_POST(self):
            u = urlparse(self.path)
            path = u.path
            if path.startswith("/api/") and not app.authorized(self.headers):
                return self._err("需要访问令牌（Authorization: Bearer <token>）", 401)
            body = self._body()
            try:
                if path == "/api/schedule":
                    st = app.scheduler.configure(
                        enabled=body.get("enabled"),
                        interval_sec=body.get("interval_sec"),
                        symbols=body.get("symbols"),
                        mode=body.get("mode"),
                        only_on_signal=body.get("only_on_signal"),
                    )
                    app.cfg.setdefault("schedule", {}).update({
                        "enabled": st["enabled"], "interval_sec": st["interval_sec"],
                        "symbols": st["symbols"], "mode": st["mode"],
                        "only_on_signal": st["only_on_signal"]})
                    save_config(app.cfg)
                    return self._json({"ok": True, "schedule": st})

                if path == "/api/schedule/run":
                    results = app.scheduler.run_now()
                    return self._json({"ok": True, "count": len(results),
                                       "plans": results})

                if path == "/api/notify/test":
                    n = app.scheduler.notifier.send("[iabot] 测试通知",
                                                    "这是一条测试消息。\n配置正确的话你应该已经收到了。")
                    return self._json({"ok": True, "sent": n,
                                       "status": app.scheduler.notifier.status()})

                if path == "/api/profiles":
                    return self._handle_profiles(body)

                if path == "/api/profiles/backtest":
                    return self._profiles_backtest(body)

                if path == "/api/positions":
                    return self._handle_position(body)

                if path == "/api/config":
                    acc = body.get("account")
                    if isinstance(acc, dict):
                        app.cfg.setdefault("account", {}).update(acc)
                    an = body.get("analysis")
                    if isinstance(an, dict):
                        app.cfg.setdefault("analysis", {}).update(an)
                    save_config(app.cfg)
                    return self._json({"ok": True, "config": app.cfg})

                if path == "/api/history/clear":
                    app.store.clear()
                    return self._json({"ok": True})

                if path == "/api/history/purge":
                    n = app.store.purge(int(body.get("days") or 30))
                    return self._json({"ok": True, "deleted": n})

                return self._err("unknown api", 404)
            except ValueError as exc:
                return self._err(f"参数错误: {exc}", 400)
            except BrokenPipeError:
                pass
            except Exception as exc:
                traceback.print_exc()
                self._err(f"{type(exc).__name__}: {exc}", 500)

    return Handler


def build_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    srv.daemon_threads = True
    return srv


def make_app(cfg: dict | None = None) -> App:
    return App(cfg or load_config())
