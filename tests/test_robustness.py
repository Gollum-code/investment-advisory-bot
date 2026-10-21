"""集成层健壮性的回归测试：配置兜底、线程安全、边界校验、缓存上限。"""

import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from iabot.advisor import Advisor
from iabot.config import DEFAULTS, DEFAULT_MODE, validate_config
from iabot.market import MarketClient
from iabot.scheduler import Scheduler
from iabot.server import build_server, make_app
from iabot.store import SignalStore


def _cfg():
    cfg = copy.deepcopy(DEFAULTS)
    cfg["open_browser"] = False
    cfg["rest_bases"] = []
    return cfg


class TestConfigHardening(unittest.TestCase):
    """validate_config 存在的意义就是"手改配置不让服务崩"。"""

    def _v(self, mutate):
        cfg = copy.deepcopy(DEFAULTS)
        mutate(cfg)
        return validate_config(cfg)

    def test_non_dict_sections_do_not_crash(self):
        # setdefault 对已存在的错类型值毫无防护，后面一 .get() 就 AttributeError
        for key, bad in (("analysis", "x"), ("account", []), ("schedule", 0),
                         ("storage", "s"), ("universe", 1), ("backup", 3),
                         ("notify", ""), ("auth", [])):
            with self.subTest(key=key):
                out = self._v(lambda c, k=key, b=bad: c.update({k: b}))
                self.assertIsInstance(out[key], dict)

    def test_nested_sections_do_not_crash(self):
        for path in (("schedule", "universe"), ("schedule", "verify"),
                     ("schedule", "dedupe"), ("analysis", "gate"),
                     ("analysis", "resonance")):
            with self.subTest(path=path):
                def mut(c, p=path):
                    c[p[0]][p[1]] = "broken"
                out = self._v(mut)
                self.assertIsInstance(out[path[0]][path[1]], dict)

    def test_numeric_fields_are_coerced(self):
        out = self._v(lambda c: c["analysis"].update({"kline_size": "abc",
                                                      "plan_cache_max": None}))
        # 非法值回落到接口上限 2000（不是 300：300 根的样本不足以验证策略）
        self.assertEqual(out["analysis"]["kline_size"], 2000)
        self.assertEqual(out["analysis"]["plan_cache_max"], 128)

    def test_kline_size_default_is_api_max(self):
        # 默认值直接决定回测样本长度，锁死在接口上限，防止又被调回 300
        self.assertEqual(DEFAULTS["analysis"]["kline_size"], 2000)
        out = self._v(lambda c: c["analysis"].update({"kline_size": 99999}))
        self.assertEqual(out["analysis"]["kline_size"], 2000)

    def test_snapshot_default_kline_size_is_api_max(self):
        # snapshot 自己的默认值也曾是 300：Advisor 之外的调用方（含测试与脚本）
        # 不显式传 kline_size 时，会悄悄退回 300 根的短样本
        from iabot.market import MarketClient
        import inspect
        sig = inspect.signature(MarketClient.snapshot)
        self.assertEqual(sig.parameters["kline_size"].default, 2000)

    def test_schedule_symbols_string_is_not_split_into_chars(self):
        """"BTC-USDT" 曾被 [s for s in s] 拆成 8 个单字符"合约"，
        然后被定时任务当合约名单傻跑一辈子。"""
        out = self._v(lambda c: c["schedule"].update({"symbols": "BTC-USDT"}))
        self.assertEqual(out["schedule"]["symbols"], ["BTC-USDT"])

    def test_schedule_symbols_single_value_list_is_kept(self):
        out = self._v(lambda c: c["schedule"].update({"symbols": ["ETH-USDT"]}))
        self.assertEqual(out["schedule"]["symbols"], ["ETH-USDT"])

    def test_top_level_symbols_string_is_not_split(self):
        out = self._v(lambda c: c.update({"symbols": "SOL-USDT"}))
        self.assertEqual(out["symbols"], ["SOL-USDT"])

    def test_bad_mode_falls_back(self):
        out = self._v(lambda c: c["schedule"].update({"mode": "scalp"}))
        # 非法模式回落到 DEFAULT_MODE（swing），不能再落回负期望的 intraday。
        self.assertEqual(out["schedule"]["mode"], DEFAULT_MODE)

    def test_verify_and_universe_numbers(self):
        def mut(c):
            c["schedule"]["verify"]["max_per_run"] = "x"
            c["schedule"]["universe"]["batch_size"] = None
            c["schedule"]["universe"]["workers"] = "many"
        out = self._v(mut)
        self.assertEqual(out["schedule"]["verify"]["max_per_run"], 50)
        self.assertEqual(out["schedule"]["universe"]["batch_size"], 20)
        self.assertEqual(out["schedule"]["universe"]["workers"], 4)

    def test_valid_config_emits_no_warning(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            validate_config(copy.deepcopy(DEFAULTS))
        self.assertEqual(buf.getvalue(), "", "正常配置不该刷告警")


class TestSchedulerThreadLifecycle(unittest.TestCase):
    """stop() 若 join 超时就把线程引用丢掉，下次 start 会起第二个循环。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SignalStore(str(Path(self.tmp.name) / "s.db"))
        self.cfg = _cfg()
        self.cfg["schedule"]["run_on_start"] = True      # 一 start 就真跑一轮

    def tearDown(self):
        self.scheduler._stop.set()
        time.sleep(0.4)
        self.store.close()
        self.tmp.cleanup()

    def _make(self):
        self.cfg["schedule"]["universe"]["enabled"] = False   # 用固定符号列表
        adv = Advisor(self.cfg)
        outer = self

        class Slow:
            def snapshot(self, symbol, *, periods, kline_size):
                time.sleep(1.5)
                from iabot.market import Snapshot
                return Snapshot(symbol=symbol, price=100.0)

            def kline(self, symbol, period, size=300):
                return []

            def health(self):
                return []

        adv.client = Slow()
        self.scheduler = Scheduler(adv, outer.store, outer.cfg)
        return self.scheduler

    @staticmethod
    def _count():
        return sum(1 for t in threading.enumerate() if t.name == "iabot-sched")

    def test_stop_timeout_keeps_thread_reference(self):
        sch = self._make()
        before = self._count()
        sch.start()
        time.sleep(0.4)            # 一轮正在跑，stop 必然超时
        sch.stop(timeout=0.1)
        self.assertTrue(sch.running, "线程还活着却报 running=False，下次 start 会重复起")
        self.assertIsNotNone(sch._thread)
        # 再 start 不得产生第二个循环
        self.assertFalse(sch.start())
        self.assertEqual(self._count(), before + 1)

    def test_start_is_noop_when_alive(self):
        sch = self._make()
        sch.start()
        time.sleep(0.2)
        self.assertFalse(sch.start())
        self.assertFalse(sch.start())
        sch.stop(timeout=0.1)

    def test_stop_when_idle_clears_reference(self):
        sch = self._make()
        sch.start()
        sch.stop(timeout=2.0)
        time.sleep(0.3)
        self.assertFalse(sch.running)
        self.assertIsNone(sch._thread)


class TestApiHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 必须隔离 CONFIG_PATH：POST /api/schedule 和 /api/config 会调
        # save_config(app.cfg)，而 app.cfg 是测试夹具（rest_bases=[]、临时 db）。
        # 不隔离的话测试会把用户真实的 config.json 覆盖成测试值——
        # 行情域名被清空、数据库指向已删除的临时目录，服务直接起不来。
        from iabot import config as _cfgmod
        self._real_cfg_path = _cfgmod.CONFIG_PATH
        _cfgmod.CONFIG_PATH = Path(self.tmp.name) / "config.json"
        self.addCleanup(self._restore_cfg_path)
        cfg = _cfg()
        cfg["storage"] = {"db_file": str(Path(self.tmp.name) / "s.db"),
                          "keep_days": 30}
        self.app = make_app(cfg)
        self.srv = build_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

    def _restore_cfg_path(self):
        from iabot import config as _cfgmod
        _cfgmod.CONFIG_PATH = self._real_cfg_path

    def test_real_config_json_is_not_touched(self):
        """守住上面那条：跑完一遍 POST 后，真实 config.json 不能被改。"""
        from iabot import config as _cfgmod
        self.assertNotEqual(_cfgmod.CONFIG_PATH, self._real_cfg_path)
        post = self._post("/api/schedule", json.dumps(
            {"symbols": ["BTC-USDT"], "mode": "swing"}).encode())
        self.assertEqual(post[0], 200)
        # 写的是临时目录
        self.assertTrue((Path(self.tmp.name) / "config.json").exists())

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.app.stop()
        self.tmp.cleanup()

    def _post(self, path, raw):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=raw,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _get(self, path):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_non_object_body_is_400_not_500(self):
        """json.loads('"x"')/[1,2]/123 都合法，下游一律按 dict 用。
        不挡就是 AttributeError -> 500 + 堆栈，而这是客户端的错误。"""
        for raw in (b'"hello"', b"[1,2,3]", b"123", b"true"):
            with self.subTest(raw=raw):
                code, d = self._post("/api/schedule", raw)
                self.assertEqual(code, 400)
                self.assertFalse(d["ok"])

    def test_schedule_rejects_string_symbols(self):
        """configure 里 [s for s in symbols] 会把字符串拆成单字符，
        且还会写进 config.json —— 必须 400 挡在前面。"""
        code, d = self._post("/api/schedule",
                             json.dumps({"symbols": "BTC-USDT"}).encode())
        self.assertEqual(code, 400)
        self.assertFalse(d["ok"])
        self.assertNotEqual(self.app.scheduler.symbols, list("BTC-USDT"))

    def test_schedule_rejects_bad_mode(self):
        code, _ = self._post("/api/schedule", json.dumps({"mode": "scalp"}).encode())
        self.assertEqual(code, 400)

    def test_schedule_accepts_valid_payload(self):
        code, d = self._post(
            "/api/schedule",
            json.dumps({"symbols": ["BTC-USDT"], "mode": "swing"}).encode())
        self.assertEqual(code, 200)
        self.assertEqual(d["schedule"]["symbols"], ["BTC-USDT"])

    def test_scan_top_is_clamped(self):
        """?top=1000000 会展开成全交易所 400 个合约，占死 HTTP 线程并必然 429。"""
        from iabot.server import _universe_override
        self.assertEqual(_universe_override({"top": "1000000"})["max_symbols"], 200)
        self.assertEqual(_universe_override({"top": "5"})["max_symbols"], 5)
        self.assertEqual(_universe_override({"top": "0"})["max_symbols"], 1)
        # 不给就不覆盖配置
        self.assertNotIn("max_symbols", _universe_override({}))

    def test_config_post_validates_and_invalidates(self):
        """手改的 score_threshold="abc" 以前直接落盘，运行时才炸。"""
        adv = self.app.advisor
        adv._plan_cache["sentinel"] = (time.time(), 0.0, object())
        code, _ = self._post("/api/config",
                             json.dumps({"analysis": {"score_threshold": "abc"}}
                                        ).encode())
        self.assertEqual(code, 200)
        self.assertEqual(self.app.cfg["analysis"]["score_threshold"], 30.0)
        self.assertNotIn("sentinel", adv._plan_cache, "缓存没被失效")

    def test_config_post_rejects_non_dict_account(self):
        code, _ = self._post("/api/config", json.dumps({"account": "x"}).encode())
        self.assertEqual(code, 200)          # 被 isinstance 挡下，不写入
        self.assertIsInstance(self.app.cfg["account"], dict)


class TestCfgConcurrency(unittest.TestCase):
    """app.cfg 被 HTTP 线程与后台线程共享，增删与遍历并发会抛
    RuntimeError: dictionary changed size during iteration。"""

    def test_profile_insert_while_listing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = _cfg()
        cfg["storage"] = {"db_file": str(Path(tmp.name) / "s.db"), "keep_days": 30}
        app = make_app(cfg)
        self.addCleanup(app.stop)
        from iabot.profiles import ProfileStore

        ps = ProfileStore(app.cfg)
        for i in range(400):
            ps.profiles[f"p{i}"] = {"factor_weights": {}, "score_threshold": 30,
                                    "mode": "intraday", "note": ""}
        errors = []
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                with app.cfg_lock:
                    ps.save(f"w{i}", factor_weights={"momentum": 0.3},
                            score_threshold=30, mode="intraday")
                    ps.delete(f"w{i}")
                i += 1

        def reader():
            try:
                while not stop.is_set():
                    with app.cfg_lock:
                        ps.list()
            except Exception as exc:
                errors.append(repr(exc))
                return

        ts = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in ts:
            t.start()
        time.sleep(1.0)
        stop.set()
        for t in ts:
            t.join(timeout=3)
        self.assertEqual(errors, [])


class TestMarketCacheBound(unittest.TestCase):
    def test_cache_is_evicted(self):
        """K 线缓存键含根数，而持仓年龄变化会让根数一直变，
        只靠 TTL 不回收——长跑服务里能涨到几百 MB。"""
        c = MarketClient([], cache_max=64)
        try:
            for i in range(300):
                c._cached(f"kline:X-USDT:60min:{i}", lambda i=i: [i])
            self.assertLessEqual(len(c._cache), 64)
        finally:
            c.pool = None


class TestEmptyKlineIsNotSilent(unittest.TestCase):
    """抓回空 K 线必须留痕，否则回测会拿残缺快照算出正常数字。"""

    def test_empty_response_recorded_as_error(self):
        from iabot.market import MarketClient
        c = MarketClient(["https://api.hbdm.com"])
        orig = c.kline

        def fake(symbol, period, size=300):
            return [] if period == "60min" else orig(symbol, period, size)

        c.kline = fake
        try:
            snap = c.snapshot("BTC-USDT", with_depth=False, with_extra=False)
        except Exception:
            self.skipTest("网络不可用")
        errs = [e for e in snap.errors if e.startswith("kline:")]
        self.assertTrue(any("60min" in e for e in errs),
                        f"空响应没有被记录: {errs}")
        self.assertNotIn("60min", snap.klines)

    def test_backtest_flags_it(self):
        from iabot.backtest import backtest
        from iabot.config import DEFAULTS as D
        from iabot.market import Snapshot
        snap = Snapshot(symbol="X-USDT", price=100.0,
                        klines={"1day": [], "60min": []})
        snap.errors.append("kline:60min: empty response")
        r = backtest(snap, D, mode="swing", step=10, warmup=60)
        self.assertTrue(r.insufficient_data)
        self.assertIn("60min", r.missing_periods)
        self.assertEqual(r.trades, 0)


class TestKlineSizeClamp(unittest.TestCase):
    """接口对 >2000 的 size 返回空数组（不是报错），必须钳住。"""

    def test_oversize_request_is_clamped(self):
        from iabot.market import MarketClient
        c = MarketClient(["https://api.hbdm.com"])
        seen = {}

        def fake_get(path):
            seen["path"] = path
            return {"data": []}

        c._get = fake_get
        c.kline("BTC-USDT", "1min", 4000)
        self.assertIn("size=2000", seen["path"])
        self.assertNotIn("size=4000", seen["path"])

    def test_normal_size_untouched(self):
        from iabot.market import MarketClient
        c = MarketClient(["https://api.hbdm.com"])
        seen = {}

        def fake_get(path):
            seen["path"] = path
            return {"data": []}

        c._get = fake_get
        c.kline("BTC-USDT", "1day", 500)
        self.assertIn("size=500", seen["path"])


if __name__ == "__main__":
    unittest.main()