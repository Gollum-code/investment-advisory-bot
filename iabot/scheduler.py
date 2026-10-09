"""定时任务：后台线程按固定间隔循环分析一批合约，落库并推送给订阅者。"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

from .advisor import Advisor
from .outcomes import evaluate_outcome, window_atr_period
from .store import SignalStore


class Scheduler:
    """一个轻量的循环任务器。

    跑在一个后台线程里，按 interval_sec 依次分析 symbols（可分批间隔）。
    每轮先回填上一轮信号的结果（先碰止损还是先到目标），再分析新的一批；
    新信号落库前会做去重，避免同一个状态刷屏，然后通过订阅者回调推送给前端（SSE）。
    """

    def __init__(self, advisor: Advisor, store: SignalStore,
                 cfg: dict[str, Any]) -> None:
        sc = cfg.get("schedule") or {}
        self.advisor = advisor
        self.store = store
        self.cfg = cfg
        self.enabled: bool = bool(sc.get("enabled", False))
        self.interval: int = max(30, int(sc.get("interval_sec") or 300))
        self.symbols: list[str] = list(sc.get("symbols") or ["BTC-USDT"])
        self.mode: str = sc.get("mode") or "intraday"
        self.only_on_signal: bool = bool(sc.get("only_on_signal", False))
        # 落库去重：方向没变且评分变化不大时，间隔期内不重复记录
        self.dedupe: dict = dict(sc.get("dedupe") or {}) if sc.get("dedupe") else {
            "cooldown_sec": 3600, "score_delta": 5.0}
        # 结果回填
        v = sc.get("verify") or {}
        self.verify_enabled: bool = bool(v.get("enabled", True))
        self.verify_min_age: float = float(v.get("min_age_sec") or 900)
        self.verify_max: int = int(v.get("max_per_run") or 50)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._subs: list[Callable[[dict], None]] = []
        self._sub_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.last_run: float | None = None
        self.next_run: float | None = None
        self.run_count = 0
        self.last_error: str | None = None
        self.log: deque[dict] = deque(maxlen=200)

    # ------------------------------------------------------------------
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        with self._sub_lock:
            self._subs.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        with self._sub_lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def _emit(self, event: dict) -> None:
        self.log.append(event)
        with self._sub_lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="iabot-sched", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def kick(self) -> None:
        """立刻唤醒跑一轮（不等间隔到点）。"""
        self._wake.set()

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        if (self.cfg.get("schedule") or {}).get("run_on_start", True):
            self._run_once()
        while not self._stop.is_set():
            self.next_run = time.time() + self.interval
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            self._run_once()
        self.next_run = None

    def _verify_pending(self) -> int:
        """回填上一轮留下、尚未判定结局的信号（用后续 K 线判断先到止损还是先到目标）。"""
        if not self.verify_enabled:
            return 0
        pending = self.store.pending(before=time.time() - self.verify_min_age,
                                     limit=self.verify_max)
        if not pending:
            return 0
        done = 0
        for row_id, plan in pending:
            sym = plan.get("symbol")
            if not sym:
                continue
            period = window_atr_period(plan.get("mode") or self.mode)
            try:
                candles = self.advisor.client.kline(sym, period, 200)
            except Exception as exc:
                self.last_error = f"verify {sym}: {type(exc).__name__}: {exc}"
                continue
            res = evaluate_outcome(plan, candles or [])
            if not res:
                continue  # 还没有后续 K 线，留到下一轮
            self.store.set_outcome(row_id, res["outcome"], ts=res["ts"],
                                   price=res["price"], rr=res["rr"],
                                   mfe=res["mfe_pct"], mae=res["mae_pct"])
            done += 1
            self._emit({"type": "outcome", "ts": res["ts"],
                        "symbol": sym, "outcome": res["outcome"],
                        "mfe_pct": res["mfe_pct"], "mae_pct": res["mae_pct"]})
        return done

    def _run_once(self) -> list[dict]:
        if not self._run_lock.acquire(blocking=False):
            self._emit({"type": "skip", "ts": time.time(), "reason": "上一轮还没跑完"})
            return []
        try:
            t0 = time.time()
            verified = self._verify_pending()
            self._emit({"type": "run_start", "ts": t0, "symbols": list(self.symbols),
                        "mode": self.mode, "verified": verified})
            results: list[dict] = []
            for sym in list(self.symbols):
                if self._stop.is_set():
                    break
                try:
                    plan = self.advisor.plan_payload(sym, mode=self.mode, force=True)
                    self.store.save(plan, only_on_signal=self.only_on_signal,
                                    dedupe=self.dedupe)
                    results.append(plan)
                    self._emit({"type": "plan", "ts": plan.get("generated_at"), "plan": plan})
                except Exception as exc:
                    self.last_error = f"{sym}: {type(exc).__name__}: {exc}"
                    self._emit({"type": "error", "ts": time.time(), "symbol": sym,
                                "error": self.last_error})
            self.run_count += 1
            self.last_run = time.time()
            self._emit({"type": "run_done", "ts": self.last_run,
                        "elapsed": round(self.last_run - t0, 2), "count": len(results),
                        "verified": verified,
                        "signals": [{"symbol": p["symbol"], "direction": p["direction"],
                                     "score": p["score"]} for p in results]})
            return results
        finally:
            self._run_lock.release()

    def run_now(self) -> list[dict]:
        """同步跑一轮（前端点"立即执行"时用）。"""
        self._stop.clear()
        return self._run_once()

    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "interval_sec": self.interval,
            "mode": self.mode,
            "symbols": list(self.symbols),
            "only_on_signal": self.only_on_signal,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "last_error": self.last_error,
            "log": list(self.log)[-40:],
        }

    def configure(self, *, enabled: bool | None = None, interval_sec: int | None = None,
                  symbols: list[str] | None = None, mode: str | None = None,
                  only_on_signal: bool | None = None) -> dict:
        if interval_sec is not None:
            self.interval = max(30, int(interval_sec))
        if symbols is not None:
            self.symbols = [s for s in symbols if s]
        if mode is not None:
            self.mode = mode
        if only_on_signal is not None:
            self.only_on_signal = only_on_signal

        want = self.enabled if enabled is None else bool(enabled)
        was_running = self.running
        if want and not was_running:
            self.enabled = True
            self.start()
        elif not want and was_running:
            self.enabled = False
            self.stop()
        else:
            self.enabled = want
        # 刚 start() 的话，_loop 里的 run_on_start 已经跑过一轮了，再 kick 会连跑两轮
        if was_running or not (self.cfg.get("schedule") or {}).get("run_on_start", True):
            self.kick()
        return self.status()
