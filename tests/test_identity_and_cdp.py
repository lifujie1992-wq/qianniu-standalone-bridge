"""1.6.31 单测：账号持久化/重构、空账号事件不丢弃、400 去重出队、授权修复原因、页面分类。

对应任务书：
  A(1) 账号持久化与恢复、A(3) 空账号事件不丢弃、A(5) 400 重复回执出队、
  B1 授权修复各步失败原因文字、B2 新版聊天页归类。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hook_diagnostics as hd
import standalone_bridge as sb


class IdentityPersistenceTests(unittest.TestCase):
    def _bridge(self, root: Path):
        app = object.__new__(sb.StandaloneBridge)
        app._accounts_root = root  # type: ignore[attr-defined]
        return app

    def test_remember_and_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self._bridge(root)
            with mock.patch.object(sb, "ROOT", root):
                app.known_accounts = []
                app.remember_account("店铺A:客服1", "event")
                app.remember_account("店铺A:客服1", "event")  # 幂等
                app.remember_account("店铺B:客服2", "page_identity")
                loaded = app.load_accounts()
            self.assertEqual(loaded, ["店铺A:客服1", "店铺B:客服2"])
            payload = json.loads((root / "state" / "accounts.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["accounts"], ["店铺A:客服1", "店铺B:客服2"])

    def test_empty_account_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self._bridge(root)
            with mock.patch.object(sb, "ROOT", root):
                app.known_accounts = []
                app.remember_account("   ")
            self.assertEqual(app.known_accounts, [])
            self.assertFalse((root / "state" / "accounts.json").exists())

    def test_corrupt_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = self._bridge(root)
            (root / "state").mkdir(parents=True)
            (root / "state" / "accounts.json").write_text("{not json", encoding="utf-8")
            with mock.patch.object(sb, "ROOT", root):
                self.assertEqual(app.load_accounts(), [])


class DuplicateRejectionTests(unittest.TestCase):
    def test_detects_duplicate_400(self):
        self.assertTrue(sb.is_duplicate_rejection(
            "大脑 HTTP 400: {\"error\": \"duplicate terminal replay\"}"
        ))
        self.assertTrue(sb.is_duplicate_rejection("HTTP 400 Bad Request: 重复事件"))
        self.assertFalse(sb.is_duplicate_rejection("大脑 HTTP 500: boom"))
        self.assertFalse(sb.is_duplicate_rejection("HTTP 400: bad payload"))


class PageKindRuleTests(unittest.TestCase):
    def test_new_chat_url_is_chat_other(self):
        self.assertEqual(hd.page_kind("https://alires-webui/reception/index.html"), "chat_other")
        self.assertEqual(hd.page_kind("https://alires-webui/x/chat.html"), "chat_other")

    def test_title_fallback(self):
        self.assertEqual(
            hd.page_kind("https://alires-webui/dx-h5/index.html", "千牛消息聊天"),
            "chat_other",
        )

    def test_dx_control_stays_other(self):
        self.assertEqual(
            hd.page_kind("https://alires-webui/dx-h5/index.html", "DxControl"),
            "other",
        )


class AuthorizeRepairTests(unittest.TestCase):
    class Config:
        def __init__(self):
            self.values = {
                "imsdk_force_hook_enabled": True,
                "imsdk_hook_grace_seconds": 0,
                "imsdk_hook_remind_interval_minutes": 15,
                "imsdk_hook_quiet_when_no_buyer_minutes": 30,
                # 默认策略禁止重启千牛；这组用例专门测"允许重启"的分支
                "cdp_restart_enabled": True,
            }

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value

    class Browser:
        connected = 0

        def diagnostics_by_connection(self):
            return []

    def make(self, cdp):
        app = type("App", (), {})()
        app.config = self.Config()
        app.browser = self.Browser()
        app.db = type("DB", (), {"has_recent_buyer_activity": lambda self, m: True})()
        app.stop_event = None
        app.cdp = cdp
        import hook_recovery as hr
        return hr.HookRecovery(app, now_fn=lambda: 1000.0, prompt_fn=None,
                               notify_fn=lambda *a: None, logger=None)

    def test_authorize_success_records_cdp_ready(self):
        class Cdp:
            def restart_with_debug_port(self):
                return {"ok": True, "port": 9333, "pages": 3, "injected": 1}

        recovery = self.make(Cdp())
        result = recovery.authorize_repair()
        self.assertTrue(result["ok"])
        self.assertIn("cdp_ready port=9333", recovery.last_action_result)

    def test_authorize_failure_keeps_specific_reason(self):
        class Cdp:
            def restart_with_debug_port(self):
                return {"ok": False, "reason": "cdp_port_not_open"}

        recovery = self.make(Cdp())
        result = recovery.authorize_repair()
        self.assertFalse(result["ok"])
        self.assertEqual(recovery.last_action_result, "cdp_port_not_open")
        self.assertEqual(recovery.human_action, "authorize_cdp")

    def test_authorize_exception_becomes_restart_failed(self):
        class Cdp:
            def restart_with_debug_port(self):
                raise RuntimeError("access denied")

        recovery = self.make(Cdp())
        recovery.authorize_repair()
        self.assertIn("restart_failed: access denied", recovery.last_action_result)

    def test_authorize_only_once(self):
        class Cdp:
            def __init__(self):
                self.calls = 0

            def restart_with_debug_port(self):
                self.calls += 1
                return {"ok": False, "reason": "no_target_found"}

        cdp = Cdp()
        recovery = self.make(cdp)
        recovery.authorize_repair()
        recovery.authorize_repair()
        self.assertEqual(cdp.calls, 1)
        self.assertIn("already", recovery.last_action_result)


class ChatTargetSelectionTests(unittest.TestCase):
    def test_prefers_recent_then_title(self):
        injector = object.__new__(sb.CdpInjector)
        injector._CHAT_URL_HINTS = ("web_chat-packer", "/chat", "reception")
        injector._CHAT_TITLE_HINTS = ("聊天", "接待")
        targets = [
            {"url": "https://alires-webui/dx-h5/index.html", "title": "DxControl"},
            {"url": "https://alires-webui/reception/index.html", "title": "接待台"},
        ]
        picked = injector.chat_targets(targets)
        self.assertEqual(len(picked), 1)
        self.assertIn("reception", picked[0]["url"])
        # 只有标题命中时也要挑出来
        picked = injector.chat_targets([
            {"url": "https://alires-webui/newui/index.html", "title": "千牛消息聊天"},
        ])
        self.assertEqual(len(picked), 1)


class EmptyAccountEventTests(unittest.TestCase):
    def test_transport_accepts_event_without_account(self):
        """B3 相关：空账号事件必须进入 ingest（不再被 transport 拒绝）。"""
        source = Path(sb.__file__).read_text(encoding="utf-8")
        self.assertIn('required = ("buyer_id", "nickname" if nickname_update else "content")', source)
        self.assertIn("account_missing_events", source)


if __name__ == "__main__":
    unittest.main()


class HistoryBackfillTests(unittest.TestCase):
    def test_recent_accounts_reads_history_without_content(self):
        # 注意：Windows 下 sqlite 文件句柄会让 TemporaryDirectory 清理失败（本机已知问题），
        # 这里用 mkdtemp 并在测试末尾跳过清理。
        import shutil, sqlite3, tempfile
        directory = Path(tempfile.mkdtemp(prefix="qn-accounts-"))
        try:
            path = directory / "state.sqlite3"
            db = sb.StateDB(path)
            with sqlite3.connect(path) as raw, raw:
                for idx, account in enumerate(["店铺A:客服1", "店铺A:客服1", "店铺B:客服2", ""]):
                    payload = json.dumps({"account": account, "buyer_id": f"b{idx}",
                                          "content": "x", "role": "user"}, ensure_ascii=False)
                    raw.execute(
                        "INSERT INTO events(event_id,payload,revision,status,created_at,updated_at)"
                        " VALUES(?,?,0,'delivered',?,?)",
                        (f"e{idx}", payload, 1000 + idx, 1000 + idx),
                    )
            accounts = db.recent_accounts(5)
        finally:
            try:
                shutil.rmtree(directory, ignore_errors=True)
            except OSError:
                pass
        self.assertEqual(accounts, ["店铺B:客服2", "店铺A:客服1"])


class ExecutePriorityTests(unittest.TestCase):
    def test_chat_page_first_and_failed_page_does_not_abort(self):
        """多页面时：没 imsdk 的页面先答"失败"，仍要用聊天页的结果。"""
        import queue as _queue
        from types import SimpleNamespace

        server = object.__new__(sb.BrowserServer)
        server.connection_lock = __import__("threading").RLock()
        server.pending = {}
        server.connections = {}
        server.connection_diagnostics = {}

        class Conn:
            def __init__(self, name):
                self.name = name

            def send(self, payload):
                queue_obj = server.pending[payload["request_id"]]
                if self.name == "other":
                    queue_obj.put({"ok": False, "error": "Qianniu imsdk is unavailable"})
                else:
                    queue_obj.put({"ok": True, "value": "chat-page-value"})

        other, chat = Conn("other"), Conn("chat")
        server.connections = {other: __import__("threading").Lock(), chat: __import__("threading").Lock()}
        server.connection_diagnostics = {
            other: {"page_kind": "other", "imsdk_hooked": False},
            chat: {"page_kind": "chat_recent", "imsdk_hooked": True},
        }
        server.send = lambda connection, payload: connection.send(payload)
        value = server.execute("1+1", timeout=2.0)
        self.assertEqual(value, "chat-page-value")


class DeliveryChunkTests(unittest.TestCase):
    def test_chunks_respect_byte_budget(self):
        worker = object.__new__(sb.DeliveryWorker)
        rows = [{"payload": {"content": "x" * 1000}} for _ in range(10)]
        chunks = worker.chunk_rows(rows, 3000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            size = sum(len(json.dumps(row["payload"], ensure_ascii=False).encode("utf-8")) for row in chunk)
            self.assertLessEqual(size, 3000)
        self.assertEqual(sum(len(chunk) for chunk in chunks), 10)

    def test_single_huge_row_still_chunks(self):
        worker = object.__new__(sb.DeliveryWorker)
        rows = [{"payload": {"content": "y" * 5000}}, {"payload": {"content": "z"}}]
        chunks = worker.chunk_rows(rows, 1000)
        self.assertEqual(len(chunks), 2)


class RestartPolicyTests(unittest.TestCase):
    class Config:
        def __init__(self, restart_enabled):
            self.values = {"cdp_restart_enabled": restart_enabled}

        def get(self, key, default=None):
            return self.values.get(key, default)

    def make(self, restart_enabled):
        import hook_recovery as hr

        class Cdp:
            calls = 0

            def restart_with_debug_port(self):
                Cdp.calls += 1
                return {"ok": True, "port": 9222, "pages": 1, "injected": 1}

        app = type("App", (), {})()
        app.config = self.Config(restart_enabled)
        app.browser = type("B", (), {"connected": 0,
                                     "diagnostics_by_connection": lambda self: []})()
        app.db = type("DB", (), {"has_recent_buyer_activity": lambda self, m: True})()
        app.stop_event = None
        app.cdp = Cdp()
        return hr.HookRecovery(app, now_fn=lambda: 1.0, prompt_fn=None,
                               notify_fn=lambda *a: None, logger=None)

    def test_default_policy_blocks_qianniu_restart(self):
        recovery = self.make(False)
        result = recovery.authorize_repair()
        self.assertFalse(result["ok"])
        self.assertIn("restart_disabled_by_policy", recovery.last_action_result)
        self.assertEqual(recovery.human_action, "open_reception_window")

    def test_explicit_opt_in_allows_restart(self):
        recovery = self.make(True)
        result = recovery.authorize_repair()
        self.assertTrue(result["ok"])


class OrdersCapableTests(unittest.TestCase):
    def test_hooked_non_chat_page_still_orders_capable(self):
        """dx-h5 里的同源 iframe 挂上 imsdk 也算能取订单。"""
        rows = [{"page_kind": "other", "imsdk_hooked": True}]
        self.assertTrue(hd.orders_capable(rows))
        self.assertEqual(hd.unavailable_reason(rows), "")

    def test_not_hooked_anywhere(self):
        rows = [{"page_kind": "other", "imsdk_hooked": False}]
        self.assertFalse(hd.orders_capable(rows))
        self.assertEqual(hd.unavailable_reason(rows), "chat_page_missing")


class FrameHookExpressionTests(unittest.TestCase):
    def test_expression_is_valid_and_mentions_guards(self):
        import subprocess
        import hook_recovery as hr
        expr = hr.build_frame_hook_expression("console.log('x');")
        self.assertIn("console.log('x')", expr)
        self.assertIn("cross_origin", expr)
        self.assertIn("data-qn-frame-bridge", expr)
        result = subprocess.run(["node", "--check", "-"], input=expr, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_frames_respects_switch(self):
        import hook_recovery as hr
        app = type("App", (), {})()
        app.config = type("C", (), {"get": lambda self, key, default=None: False})()
        app.browser = type("B", (), {"connected": 1, "execute": staticmethod(lambda *a, **k: None)})()
        recovery = hr.HookRecovery(app, now_fn=lambda: 1.0, prompt_fn=None,
                                   notify_fn=lambda *a: None, logger=None)
        self.assertFalse(recovery.hook_frames()["ok"])
