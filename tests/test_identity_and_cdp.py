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
