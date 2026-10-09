"""网络工具：TLS 兜底 + JSON 请求 + 多域名自动切换。

两个 Windows 上很常见的坑，这里都处理掉了：

1. 某些 Python 环境（MSYS2 / Conda 精简包）没有任何根证书，
   `ssl.create_default_context()` 的 x509 计数为 0，所有 HTTPS 直接
   报 CERTIFICATE_VERIFY_FAILED。这里按 系统默认 -> certifi ->
   Windows 证书存储 -> 磁盘上能找到的 ca-bundle 逐级兜底。

2. 火币合约的 api.hbdm.com 在国内部分网络被拦截（DNS 污染 / 连接超时），
   但 api.btcgateway.pro 是同一套接口的备用域名。这里做健康探测 + 自动故障转移。
"""

from __future__ import annotations

import base64
import glob
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_lock = threading.Lock()
_contexts: dict[bool, ssl.SSLContext] = {}
_bundle_used: str | None = None


def _drives() -> list[str]:
    return [f"{c}:" for c in "CDEFG" if os.path.exists(f"{c}:\\")]


def _bundle_candidates() -> list[str]:
    paths: list[str] = []
    for env in ("IABOT_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        val = os.environ.get(env)
        if val:
            paths.append(val)

    rel = (
        "etc/ssl/cert.pem",
        "etc/ssl/certs/ca-bundle.crt",
        "etc/ssl/certs/ca-certificates.crt",
        "ssl/cert.pem",
        "etc/pki/tls/certs/ca-bundle.crt",
    )
    for base in {sys.prefix, getattr(sys, "base_prefix", sys.prefix), sys.exec_prefix}:
        paths += [os.path.join(base, r) for r in rel]

    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
        paths += [
            os.path.join(root, "mingw64/etc/ssl/certs/ca-bundle.crt"),
            os.path.join(root, "usr/ssl/certs/ca-bundle.crt"),
        ]

    sub = (
        r"Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt",
        r"Program Files\Git\usr\ssl\certs\ca-bundle.crt",
        r"Program Files (x86)\Git\mingw64\etc\ssl\certs\ca-bundle.crt",
        r"Git\mingw64\etc\ssl\certs\ca-bundle.crt",
        r"Git\usr\ssl\certs\ca-bundle.crt",
        r"msys64\usr\ssl\certs\ca-bundle.crt",
        r"msys64\ucrt64\etc\ssl\certs\ca-bundle.crt",
        r"msys64\ucrt64\etc\ssl\cert.pem",
        r"msys64\mingw64\etc\ssl\certs\ca-bundle.crt",
    )
    for d in _drives():
        paths += [os.path.join(d + "\\", s) for s in sub]

    patterns = (
        r"Users\*\AppData\Local\Programs\Python\Python3*\Lib\site-packages\certifi\cacert.pem",
        r"Python*\Lib\site-packages\certifi\cacert.pem",
        r"python*\Lib\site-packages\certifi\cacert.pem",
        r"Program Files\Python*\Lib\site-packages\certifi\cacert.pem",
    )
    for d in _drives():
        for pat in patterns:
            try:
                paths += glob.glob(os.path.join(d + "\\", pat))
            except Exception:
                pass
    return paths


def _load_bundle(path: str):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 1000:
            return None
        ctx = ssl.create_default_context(cafile=path)
        if ctx.cert_store_stats().get("x509", 0) > 0:
            return ctx
    except Exception:
        return None
    return None


def _pem_from_windows_store() -> str:
    enum = getattr(ssl, "enum_certificates", None)
    if enum is None:
        return ""
    chunks: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            for cert, encoding, _trust in enum(store):
                if encoding != "x509":
                    continue
                b64 = base64.b64encode(cert).decode("ascii")
                body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
                chunks.append(f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n")
        except Exception:
            continue
    return "".join(chunks)


def ssl_context(verify: bool = True) -> ssl.SSLContext:
    global _bundle_used
    with _lock:
        if verify in _contexts:
            return _contexts[verify]

        ctx = ssl.create_default_context()
        if ctx.cert_store_stats().get("x509", 0) == 0:
            try:
                import certifi  # type: ignore

                ctx = ssl.create_default_context(cafile=certifi.where())
                _bundle_used = certifi.where()
            except Exception:
                pass
        if ctx.cert_store_stats().get("x509", 0) == 0:
            pem = _pem_from_windows_store()
            if pem:
                ctx = ssl.create_default_context(cadata=pem)
                _bundle_used = "<windows-cert-store>"
        if ctx.cert_store_stats().get("x509", 0) == 0:
            for cand in _bundle_candidates():
                loaded = _load_bundle(cand)
                if loaded is not None:
                    ctx = loaded
                    _bundle_used = cand
                    break
        if not verify or ctx.cert_store_stats().get("x509", 0) == 0:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        _contexts[verify] = ctx
        return ctx


def cert_info() -> dict:
    ctx = ssl_context(True)
    stats = ctx.cert_store_stats()
    return {
        "ca_count": stats.get("x509", 0),
        "verifying": ctx.verify_mode != ssl.CERT_NONE,
        "bundle": _bundle_used,
    }


def http_json(
    url: str,
    *,
    data: dict | None = None,
    headers: dict | None = None,
    timeout: float = 15.0,
    verify: bool = True,
) -> dict:
    """GET / POST JSON。data 不为 None 时走 POST。"""
    body = None
    hdrs = {"User-Agent": "iabot/0.1", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context(verify)) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def encode_path(path: str) -> str:
    """把 path 里的非 ASCII 字符做百分号编码。

    火币有些合约的代码本身就是中文（如 `牛来-USDT`、`哈基米-USDT`），
    直接拼进 URL 会让 urllib 抛 UnicodeEncodeError。纯 ASCII 的 path
    原样返回，既有行为零变化。
    """
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        return urllib.parse.quote(path, safe="/?&=%:+,$-_.~")


class BasePool:
    """行情域名池：探测出可用的 base，之后固定用它；挂了自动换下一个。"""

    def __init__(self, bases: list[str], *, timeout: float = 12.0,
                 retries: int = 2, verify: bool = True) -> None:
        self._bases = [b.rstrip("/") for b in bases if b]
        self._timeout = timeout
        self._retries = max(1, retries)
        self._verify = verify
        self._current: str | None = None
        self._lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._probe_cache: dict[str, tuple[bool, float, str]] = {}
        self._errors: list[str] = []

    @property
    def current(self) -> str | None:
        return self._current

    @property
    def last_errors(self) -> list[str]:
        return list(self._errors)

    def _probe(self, base: str) -> tuple[bool, float, str]:
        url = f"{base}/linear-swap-ex/market/detail/merged?contract_code=BTC-USDT"
        t0 = time.time()
        try:
            # 健康探测只是打个极小的 GET。4 秒还答不上来的域名不适合当主力，
            # 而且每个超时都会变成用户等待。真正的行情请求仍用完整超时。
            d = http_json(url, timeout=min(self._timeout, 4.0), verify=self._verify)
            if str(d.get("status", "")).lower() == "ok" and d.get("tick"):
                return True, (time.time() - t0) * 1000, ""
            return False, (time.time() - t0) * 1000, f"返回异常: {str(d)[:120]}"
        except Exception as exc:
            return False, (time.time() - t0) * 1000, f"{type(exc).__name__}: {exc}"

    def probe_all(self) -> None:
        """把所有域名探一遍（只探一次，结果缓存）。

        必须并行：串行探测时，一个连不上的域名会把首次请求整整拖慢一个超时
        （实测国内网络下 api.hbdm.vip 要 8 秒才超时）。
        """
        with self._probe_lock:
            missing = [b for b in self._bases if b not in self._probe_cache]
            if not missing:
                return
            with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
                futs = {pool.submit(self._probe, b): b for b in missing}
                for fut in as_completed(futs):
                    base = futs[fut]
                    try:
                        self._probe_cache[base] = fut.result()
                    except Exception as exc:
                        self._probe_cache[base] = (False, 0.0, f"{type(exc).__name__}: {exc}")

    def candidates(self, force: bool = False) -> list[str]:
        """按探测结果排序的可用域名列表。"""
        order: list[str] = []
        if self._current and not force:
            order.append(self._current)
        if force:
            self._probe_cache.clear()
        self.probe_all()
        for b in self._bases:
            if b in order:
                continue
            ok, _ms, _err = self._probe_cache.get(b, (False, 0.0, ""))
            if ok:
                order.append(b)
        for b in self._bases:          # 全挂时仍然按原顺序试一遍
            if b not in order:
                order.append(b)
        return order

    def health(self) -> list[dict]:
        self.probe_all()
        out = []
        for b in self._bases:
            ok, ms, err = self._probe_cache.get(b, (False, 0.0, ""))
            out.append({"base": b, "ok": ok, "ms": round(ms), "error": err,
                        "current": b == self._current})
        return out

    def get(self, path: str, *, timeout: float | None = None) -> dict:
        """按可用性顺序尝试各域名。path 以 / 开头。"""
        timeout = timeout or self._timeout
        errors: list[str] = []
        for base in self.candidates():
            url = base + encode_path(path)
            for attempt in range(self._retries):
                try:
                    d = http_json(url, timeout=timeout, verify=self._verify)
                    with self._lock:
                        self._current = base
                        self._probe_cache[base] = (True, 0.0, "")
                    self._errors = errors
                    return d
                except Exception as exc:
                    errors.append(f"{base} [{attempt + 1}/{self._retries}] {type(exc).__name__}: {exc}")
                    if attempt + 1 < self._retries:
                        time.sleep(0.4 * (attempt + 1))
            with self._lock:          # 这个域名不行了，标记掉换下一个
                self._probe_cache[base] = (False, 0.0, errors[-1] if errors else "failed")
                if self._current == base:
                    self._current = None
        self._errors = errors
        raise RuntimeError("所有行情域名均不可用:\n  " + "\n  ".join(errors[-6:]))
