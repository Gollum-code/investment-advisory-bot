"""信号通知：Webhook（通用）/ Telegram / Bark（iOS）。

只用标准库 urllib。设计原则：

- 通知是"状态变化"驱动的，不是每轮都发：方向翻转、或强信号、止损/目标命中
  才推，避免一轮一个币刷屏（配合 store 的去重）。
- 失败不影响主流程：任何异常都吞掉，只记录 last_error。
- 不打印 token / URL 到日志。
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from typing import Any

from .netutil import ssl_context


def _fmt_num(v, d=2):
    return "--" if v is None else f"{v:,.{d}f}"


def render_signal(plan: dict) -> str:
    """把一个信号渲染成纯文本通知内容（Webhook text / Telegram text 共用）。"""
    sym = plan.get("symbol", "?")
    direction = plan.get("direction")
    score = plan.get("score") or 0
    cn = {"long": "做多", "short": "做空", "wait": "观望"}.get(direction, direction or "?")
    lines = [f"{sym} {cn}（评分 {score:+.1f}，置信度{plan.get('confidence', '--')}）"]
    price = plan.get("price")
    d = 1 if (price or 0) > 1000 else (4 if (price or 0) > 1 else 8)
    if direction in ("long", "short"):
        lines.append(f"入场 {_fmt_num(plan.get('entry_low'), d)} – {_fmt_num(plan.get('entry_high'), d)}"
                     f" | 止损 {_fmt_num(plan.get('stop'), d)}（{_fmt_num(plan.get('stop_pct'))}%）"
                     f" | 盈亏比 {_fmt_num(plan.get('rr'))}")
        for t in (plan.get("targets") or [])[:3]:
            lines.append(f"  {t.get('label')} {_fmt_num(t.get('price'), d)}"
                         f"（RR {_fmt_num(t.get('rr'))}）")
        s = plan.get("sizing") or {}
        if s.get("applicable"):
            lines.append(f"仓位 {s.get('contracts')} 张 / 保证金 {_fmt_num(s.get('margin_usdt'))} U"
                         f" / 止损亏 {_fmt_num(s.get('loss_if_stopped_usdt'))} U")
    note = plan.get("entry_note")
    if note:
        lines.append(f"说明：{note}")
    return "\n".join(lines)


class Notifier:
    """按配置把信号文本发到各渠道。notify 配置形如：

    "notify": {
      "enabled": true,
      "min_abs_score": 45,          # 只推 |score| 达到这个的信号
      "on_direction_change": true,  # 观望<->做多/做空 翻转时也推
      "webhook": {"url": "https://example.com/hook", "headers": {...}},
      "telegram": {"token": "xxx", "chat_id": "123"},
      "bark": {"url": "https://api.day.app/KEY"}
    }
    """

    def __init__(self, cfg: dict | None = None) -> None:
        n = (cfg or {}).get("notify") or {}
        self.enabled = bool(n.get("enabled", False))
        self.min_abs_score = float(n.get("min_abs_score", 45.0))
        self.on_direction_change = bool(n.get("on_direction_change", True))
        self._webhook = n.get("webhook") or {}
        self._telegram = n.get("telegram") or {}
        self._bark = n.get("bark") or {}
        self.timeout = float(n.get("timeout_sec", 8.0))
        self.verify_tls = bool((cfg or {}).get("tls_verify", True))
        self.last_error: str | None = None
        self.sent_count = 0
        self._lock = threading.Lock()

    # 是否有任一渠道可用
    @property
    def configured(self) -> bool:
        return bool(self._webhook.get("url") or
                    (self._telegram.get("token") and self._telegram.get("chat_id")) or
                    self._bark.get("url"))

    def _post(self, url: str, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs = {"Content-Type": "application/json; charset=utf-8",
                "User-Agent": "iabot/0.1"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout,
                                    context=ssl_context(self.verify_tls)) as resp:
            resp.read()

    def _get(self, url: str) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "iabot/0.1"})
        with urllib.request.urlopen(req, timeout=self.timeout,
                                    context=ssl_context(self.verify_tls)) as resp:
            resp.read()

    def send(self, title: str, text: str) -> int:
        """发到所有已配置渠道，返回成功条数。任何渠道失败都不抛出。"""
        if not self.enabled:
            return 0
        ok = 0
        errors: list[str] = []

        if self._webhook.get("url"):
            try:
                self._post(self._webhook["url"],
                           {"title": title, "text": text, "content": text},
                           self._webhook.get("headers"))
                ok += 1
            except Exception as exc:
                errors.append(f"webhook: {type(exc).__name__}: {exc}")

        tg = self._telegram
        if tg.get("token") and tg.get("chat_id"):
            try:
                url = ("https://api.telegram.org/bot"
                       + urllib.parse.quote(str(tg["token"]), safe="")
                       + "/sendMessage")
                self._post(url, {"chat_id": tg["chat_id"], "text": f"{title}\n{text}",
                                 "disable_web_page_preview": True})
                ok += 1
            except Exception as exc:
                errors.append(f"telegram: {type(exc).__name__}: {exc}")

        if self._bark.get("url"):
            try:
                base = self._bark["url"].rstrip("/")
                url = f"{base}/{urllib.parse.quote(title, safe='')}/{urllib.parse.quote(text, safe='')}"
                self._get(url)
                ok += 1
            except Exception as exc:
                errors.append(f"bark: {type(exc).__name__}: {exc}")

        with self._lock:
            self.last_error = "; ".join(errors) if errors else None
            self.sent_count += ok
        return ok

    def notify_signal(self, plan: dict, *, prev_direction: str | None = None) -> int:
        """按阈值/状态变化决定是否推送一个信号。"""
        if not self.enabled or not self.configured:
            return 0
        direction = plan.get("direction")
        score = plan.get("score") or 0
        changed = (prev_direction is not None and direction != prev_direction
                   and direction != "wait" and prev_direction != "wait")
        strong = direction in ("long", "short") and abs(score) >= self.min_abs_score
        if not (strong or (self.on_direction_change and changed)):
            return 0
        title = f"[iabot] {plan.get('symbol')} {score:+.0f}"
        return self.send(title, render_signal(plan))

    def notify_outcome(self, symbol: str, outcome: str, *, mfe_pct=None, mae_pct=None) -> int:
        label = {"tp1": "命中 TP1", "tp2": "命中 TP2", "tp3": "命中 TP3",
                 "stopped": "触发止损", "timeout": "到期未了结"}.get(outcome, outcome)
        text = f"{symbol} 信号结果：{label}"
        if mfe_pct is not None:
            text += f"\n最大浮盈 {_fmt_num(mfe_pct)}% / 最大浮亏 {_fmt_num(mae_pct)}%"
        return self.send(f"[iabot] {symbol} {label}", text)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "configured": self.configured,
                "min_abs_score": self.min_abs_score,
                "channels": {
                    "webhook": bool(self._webhook.get("url")),
                    "telegram": bool(self._telegram.get("token") and self._telegram.get("chat_id")),
                    "bark": bool(self._bark.get("url")),
                },
                "sent_count": self.sent_count,
                "last_error": self.last_error,
            }