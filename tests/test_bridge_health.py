"""Rejected browser events are diagnostics, not a stopped bridge service."""
import gc
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import launcher
import tray_app
from standalone_bridge import BrowserServer, Config, StandaloneBridge


class BridgeHealthTests(unittest.TestCase):
    def test_rejected_event_does_not_poison_transport_health(self):
        app = SimpleNamespace(ingest_event=Mock(return_value=("valid", True)))
        browser = BrowserServer(app)

        class Connection:
            def __iter__(self):
                return iter([
                    json.dumps({"type": "chat_event", "payload": {}}),
                    json.dumps({"type": "heartbeat", "diagnostics": {"imsdk_hooked": True}}),
                    json.dumps({"type": "chat_event", "payload": {
                        "account": "shop", "buyer_id": "buyer", "content": "hello",
                    }}),
                ])

        with patch.object(browser, "allowed", return_value=True), patch.object(browser, "send") as send:
            browser.handler(Connection())
        self.assertEqual(browser.error, "")
        self.assertEqual(browser.rejected_events, 1)
        self.assertIn("missing", browser.event_error)
        self.assertEqual(browser.total_events, 1)
        app.ingest_event.assert_called_once()
        self.assertEqual(send.call_args.args[1]["type"], "chat_ack")

    def test_rejected_nickname_is_diagnostic_and_not_accepted(self):
        browser = BrowserServer(SimpleNamespace(ingest_event=Mock()))

        class Connection:
            def __iter__(self):
                return iter([json.dumps({"type": "chat_event", "payload": {
                    "type": "nickname_update", "account": "shop", "buyer_id": "12345",
                    "nickname": "12345",
                }})])

        with patch.object(browser, "allowed", return_value=True):
            browser.handler(Connection())
        self.assertEqual(browser.error, "")
        self.assertEqual(browser.rejected_events, 1)
        self.assertIn("nickname", browser.event_error)
        browser.app.ingest_event.assert_not_called()

    def test_transport_exception_still_reports_failure(self):
        browser = BrowserServer(SimpleNamespace())

        class Connection:
            def __iter__(self):
                raise RuntimeError("broken transport")

        with patch.object(browser, "allowed", return_value=True):
            browser.handler(Connection())
        self.assertEqual(browser.error, "broken transport")

    def test_real_status_preserves_rejected_event_evidence_without_failing_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("standalone_bridge.ROOT", root):
                app = StandaloneBridge(Config(root / "config.json", {
                    "api_port": 42111, "workbench_port": 18776,
                }))
                app.client_support.status = Mock(return_value={})
                app.browser.event_error = "browser event missing account, buyer_id, content"
                app.browser.rejected_events = 1
                status = app.status(include_brain=False)
                self.assertTrue(status["ok"])
                self.assertEqual(status["browser"]["rejected_events"], 1)
                self.assertIn("missing", status["browser"]["event_error"])
                app.browser.error = "websocket server failed"
                self.assertFalse(app.status(include_brain=False)["ok"])
            gc.collect()


class TrayReachabilityTests(unittest.TestCase):
    def setUp(self):
        tray_app._BRIDGE_PROBE_STATE.clear()

    def test_live_unhealthy_api_is_degraded_instead_of_stopped_or_not_listening(self):
        payload = {"ok": False, "version": "1.6.25", "browser": {"error": "websocket server failed"}}
        with patch.object(launcher, "load_config_silent", return_value={"api_port": 42111}), \
                patch.object(launcher, "fetch_status_with_reason", return_value=(payload, "")), \
                patch.object(tray_app, "_managed_qianniu_count", return_value=1), \
                patch.object(tray_app, "_pid_file_running", return_value=True):
            status = tray_app.build_status(Path("unused"))
        self.assertEqual(status.state, "degraded")
        self.assertEqual(status.components["bridge"].state, "degraded")
        self.assertIn("websocket server failed", status.components["bridge"].detail)
        self.assertNotIn("未监听", status.components["bridge"].detail)

    def test_real_api_failure_is_not_hidden_by_previous_success_grace_period(self):
        config = {"api_port": 42111}
        with patch.object(launcher, "fetch_status_with_reason", return_value=({"ok": True}, "")):
            self.assertTrue(tray_app._probe_bridge(config)[0])
        with patch.object(launcher, "fetch_status_with_reason", return_value=({"ok": False, "api": {"error": "api failed"}}, "")):
            healthy, detail, _ = tray_app._probe_bridge(config)
        self.assertFalse(healthy)
        self.assertIn("api failed", detail)
