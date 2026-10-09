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
from .signals import MODES
from .store import SignalStore

WEB_DIR = Path(__file__).resolve().parent / "web"


class App:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.advisor = Advisor(cfg)
        self.store = SignalStore((cfg.get("storage") or {}).get("db_file", "data/signals.db"),
                                 int((cfg.get("storage") or {}).get("keep_days", 30)))
        self.scheduler = Scheduler(self.advisor, self.store, cfg)
        self.started_at = time.time()

    def start(self) -> None:
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
            pass  # 静音访问日志

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
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path in ("/app.js", "/style.css", "/favicon.ico"):
                    return self._static(path.lstrip("/"))
                if path.startswith("/api/"):
                    return self._api_get(path, q)
                return self._err("not found", 404)
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
                                   "ts": time.time()})

            if path == "/api/history":
                lim = min(500, int(q.get("limit") or 100))
                rows = app.store.query(symbol=q.get("symbol"), mode=q.get("mode"),
                                       direction=q.get("direction"), limit=lim)
                return self._json({"ok": True, "rows": rows, "count": len(rows)})

            if path == "/api/stats":
                return self._json({"ok": True, "stats": app.store.stats(
                    int(q.get("hours") or 24))})

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

        # ---------------- POST ----------------
        def do_POST(self):
            u = urlparse(self.path)
            path = u.path
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
