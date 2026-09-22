import hashlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from standalone_bridge import (  # noqa: E402
    AppBizSendAdapter,
    ApiHandler,
    BrainConnector,
    BrowserServer,
    Config,
    ContextEnricher,
    CdpInjector,
    DeliveryWorker,
    NativeAdapter,
    StandaloneBridge,
    StateDB,
    UpdaterGuard,
    WorkbenchHandler,
    BUILD_HASH,
    VERSION,
    WORKBENCH_SESSION_LIMIT,
    brain_event_suppression_reason,
    canonical_event_id,
    json_text,
    normalize_native_frame,
    outbound_safety_result,
    preferred_buyer_nick,
    suggested_handoff_reason,
)
from docked_workbench import DockedWorkbench, Rect, choose_dock_rect  # noqa: E402
import launcher  # noqa: E402
import qianniu_app  # noqa: E402
import client_updater  # noqa: E402
import config_dialog  # noqa: E402
from config_defaults import (  # noqa: E402
    CONFIG_DEFAULTS_REVISION,
    CONFIG_DEFAULTS_REVISION_KEY,
    INTERNAL_SAFETY_DEFAULTS,
    OPERATIONAL_DEFAULTS,
    apply_operational_defaults,
)
from device_identity import (  # noqa: E402
    BRAIN_WORKSTATION_TOKEN_KEY,
    DeviceIdentityError,
    brain_workstation_token,
    prepare_device_identity,
    stable_device_id,
)


class FakeHttpResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class ConfigDefaultsTests(unittest.TestCase):
    def test_first_run_enables_all_customer_facing_capabilities(self):
        config = config_dialog.build_default_config()
        for key in OPERATIONAL_DEFAULTS:
            self.assertIs(config[key], True, key)
        for key, expected in INTERNAL_SAFETY_DEFAULTS.items():
            self.assertIs(config[key], expected, key)
        self.assertEqual(config[CONFIG_DEFAULTS_REVISION_KEY], CONFIG_DEFAULTS_REVISION)

    def test_migration_enables_existing_false_operational_values_once(self):
        config = {key: False for key in OPERATIONAL_DEFAULTS}
        self.assertTrue(apply_operational_defaults(config))
        self.assertTrue(all(config[key] is True for key in OPERATIONAL_DEFAULTS))
        config["send_enabled"] = False
        self.assertFalse(apply_operational_defaults(config))
        self.assertIs(config["send_enabled"], False)

    def test_direct_bridge_load_persists_operational_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = {
                "ws_host": "127.0.0.1",
                "api_host": "127.0.0.1",
                "api_token": "api-token",
                "browser_token": "browser-token",
                "gateway_url": "http://127.0.0.1:18767",
                "workbench_host": "127.0.0.1",
                "workbench_port": 18767,
                "workbench_token": "workbench-token",
                "send_enabled": False,
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            loaded = Config.load(path)
            self.assertIs(loaded.get("send_enabled"), True)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(persisted["send_enabled"], True)
            self.assertEqual(
                persisted[CONFIG_DEFAULTS_REVISION_KEY],
                CONFIG_DEFAULTS_REVISION,
            )


class SetupAndUpdateTests(unittest.TestCase):
    def test_brain_setup_only_requires_url_and_workstation_token(self):
        self.assertEqual(
            config_dialog.validate_brain_settings("https://brain.example", "seat-token"),
            "",
        )
        self.assertIn(
            "令牌",
            config_dialog.validate_brain_settings("https://brain.example", ""),
        )
        self.assertIn(
            "http",
            config_dialog.validate_brain_settings("brain.example", "seat-token"),
        )

    def test_update_manifest_requires_https_installer_and_valid_checksum(self):
        config = {
            "update_manifest_url": "https://brain.example/client-update.json",
            "brain_agent_token": "seat-token",
            "brain_agent_id": "seat-a",
        }
        response = FakeHttpResponse(200, {
            "version": "9.9.9",
            "installer_url": "https://downloads.example/setup.exe",
            "sha256": "a" * 64,
            "size": 123,
        })
        with patch.object(client_updater.urllib.request, "urlopen", return_value=response):
            release = client_updater.fetch_update_manifest(config)
        self.assertTrue(client_updater.update_available(release))
        response = FakeHttpResponse(200, {
            "version": "9.9.9",
            "installer_url": "http://downloads.example/setup.exe",
            "sha256": "a" * 64,
        })
        with patch.object(client_updater.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                client_updater.fetch_update_manifest(config)

    def test_installer_is_per_user_and_has_complete_uninstall_and_update_shortcut(self):
        source = (ROOT / "installer" / "QianniuAIService.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", source)
        self.assertIn('Parameters: "--configure"', source)
        self.assertIn('Parameters: "--update"', source)
        self.assertIn('Type: filesandordirs; Name: "{app}"', source)
        self.assertIn("[UninstallRun]", source)

    def test_startup_update_check_is_silent_on_network_failure(self):
        with patch.object(
            client_updater,
            "fetch_update_manifest",
            side_effect=RuntimeError("offline"),
        ), patch.object(launcher, "message_box") as message_box:
            self.assertFalse(launcher.offer_update({}, quiet=True))
        message_box.assert_not_called()

    def test_detected_update_is_only_installed_after_user_confirmation(self):
        release = {"version": "9.9.9", "notes": "更新说明"}
        with patch.object(client_updater, "fetch_update_manifest", return_value=release), \
                patch.object(client_updater, "update_available", return_value=True), \
                patch.object(client_updater, "download_installer") as download, \
                patch.object(launcher, "message_box", return_value=0):
            self.assertFalse(launcher.offer_update({}, quiet=True))
        download.assert_not_called()

    def test_confirmed_update_downloads_stops_and_launches_installer(self):
        release = {"version": "9.9.9", "notes": "更新说明"}
        installer = Path("new-setup.exe")
        with patch.object(client_updater, "fetch_update_manifest", return_value=release), \
                patch.object(client_updater, "update_available", return_value=True), \
                patch.object(client_updater, "download_installer", return_value=installer), \
                patch.object(client_updater, "launch_installer_after_exit") as launch, \
                patch.object(launcher, "stop_all") as stop, \
                patch.object(launcher, "message_box", return_value=launcher.IDYES):
            self.assertTrue(launcher.offer_update({}, quiet=True))
        stop.assert_called_once()
        launch.assert_called_once_with(installer)

    def test_download_rejects_a_checksum_mismatch(self):
        response = io.BytesIO(b"installer bytes")
        with patch.object(client_updater.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                client_updater.download_installer({
                    "version": "9.9.9",
                    "installer_url": "https://downloads.example/setup.exe",
                    "sha256": "0" * 64,
                    "size": 0,
                })


class DeviceIdentityTests(unittest.TestCase):
    MACHINE_GUID = " 01234567-89AB-CDEF-0123-456789ABCDEF "
    EXPECTED_ID = "device-4685eabd516ed056106120fae482d3e802ece5627747355a61a47ded5b1fb44b"

    def test_same_machine_guid_always_generates_same_device_id(self):
        first = stable_device_id(self.MACHINE_GUID)
        second = stable_device_id("01234567-89ab-cdef-0123-456789abcdef")
        self.assertEqual(first, self.EXPECTED_ID)
        self.assertEqual(second, self.EXPECTED_ID)

    def test_different_install_directories_generate_same_device_id(self):
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "install-a" / "config.json"
            second_path = Path(directory) / "install-b" / "config.json"
            first_config = {}
            second_config = {}
            first = prepare_device_identity(
                first_config, first_path, machine_guid=self.MACHINE_GUID
            )
            second = prepare_device_identity(
                second_config, second_path, machine_guid=self.MACHINE_GUID
            )
        self.assertEqual(first.device_id, second.device_id)
        self.assertEqual(first_config["device_id"], second_config["device_id"])

    def test_successful_migration_saves_new_id_after_server_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = {
                "device_id": "legacy-device",
                "brain_server_url": "http://brain.example/",
                BRAIN_WORKSTATION_TOKEN_KEY: "shared-seat-token",
            }
            path.write_text(json.dumps(config), encoding="utf-8")
            captured = {}

            def opener(request, timeout):
                captured["url"] = request.full_url
                captured["headers"] = {
                    key.lower(): value for key, value in request.header_items()
                }
                captured["body"] = json.loads(request.data.decode("utf-8"))
                captured["timeout"] = timeout
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8"))["device_id"],
                    "legacy-device",
                )
                return FakeHttpResponse(200, {"ok": True})

            result = prepare_device_identity(
                config,
                path,
                machine_guid=self.MACHINE_GUID,
                opener=opener,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(result.migrated)
        self.assertEqual(saved["device_id"], self.EXPECTED_ID)
        self.assertEqual(captured["url"], "http://brain.example/api/bridge/v1/device/migrate")
        self.assertEqual(captured["headers"]["x-agent-token"], "shared-seat-token")
        self.assertEqual(captured["headers"]["x-device-id"], "legacy-device")
        self.assertEqual(captured["headers"]["content-type"], "application/json")
        self.assertEqual(captured["body"], {"new_device_id": self.EXPECTED_ID})

    def test_failed_migration_preserves_old_id_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = {
                "device_id": "legacy-device",
                "brain_server_url": "http://brain.example",
                BRAIN_WORKSTATION_TOKEN_KEY: "shared-seat-token",
            }
            original = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            path.write_bytes(original)
            with self.assertRaises(DeviceIdentityError):
                prepare_device_identity(
                    config,
                    path,
                    machine_guid=self.MACHINE_GUID,
                    opener=lambda *_args, **_kwargs: FakeHttpResponse(
                        200, {"ok": False, "error": "binding mismatch"}
                    ),
                )
            self.assertEqual(config["device_id"], "legacy-device")
            self.assertEqual(path.read_bytes(), original)

    def test_workbench_and_bridge_share_one_brain_workstation_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config(path, {
                BRAIN_WORKSTATION_TOKEN_KEY: "shared-seat-token",
                "gateway_agent_token": "local-gateway-token",
                "api_token": "local-api-token",
                "brain_server_url": "http://brain.example",
                "brain_enabled": True,
                "brain_agent_id": "agent-test",
            })
            connector = BrainConnector(SimpleNamespace(config=config))
            self.assertEqual(connector._headers()["X-Agent-Token"], "shared-seat-token")
            config.update_brain_settings({
                "server_url": "http://brain.example",
                "agent_token": "updated-seat-token",
                "agent_id": "agent-test",
                "enabled": True,
            })
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(brain_workstation_token(config), "updated-seat-token")
        self.assertEqual(connector._headers()["X-Agent-Token"], "updated-seat-token")
        self.assertEqual(saved[BRAIN_WORKSTATION_TOKEN_KEY], "updated-seat-token")
        self.assertEqual(saved["gateway_agent_token"], "local-gateway-token")
        self.assertEqual(saved["api_token"], "local-api-token")

    def test_stable_id_does_not_repeat_migration_or_rewrite_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = {
                "device_id": self.EXPECTED_ID,
                "brain_server_url": "http://brain.example",
                BRAIN_WORKSTATION_TOKEN_KEY: "shared-seat-token",
            }
            original = (json.dumps(config, indent=2) + "\n").encode("utf-8")
            path.write_bytes(original)
            opener = Mock()
            result = prepare_device_identity(
                config,
                path,
                machine_guid=self.MACHINE_GUID,
                opener=opener,
            )
            self.assertFalse(result.changed)
            self.assertFalse(result.migrated)
            opener.assert_not_called()
            self.assertEqual(path.read_bytes(), original)


class BrowserReceiveCompatibilityTests(unittest.TestCase):
    def test_executable_browser_contract(self):
        result = subprocess.run(
            ["node", "--test", str(ROOT / "tests" / "browser_bridge_contract.test.js")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_callback_handler_accepts_all_arguments_and_nested_ccodes(self):
        source = (ROOT / "browser_bridge.js").read_text(encoding="utf-8")
        self.assertIn("function receiveNewMessageHandler()", source)
        self.assertIn("Array.prototype.slice.call(arguments)", source)
        self.assertIn("walkConversationIds(value, mode, 0, [])", source)
        self.assertIn("LOCAL_RETRY_DELAYS_MS = [50, 200, 600, 1500, 3000]", source)

    def test_browser_diagnostics_are_kept_per_connection(self):
        server = BrowserServer(SimpleNamespace())
        first, second = object(), object()
        server.record_diagnostics(first, {"version": "a", "event_callbacks": 183})
        server.record_diagnostics(second, {"version": "b", "event_callbacks": 0})
        snapshots = server.diagnostics_by_connection()
        self.assertEqual(
            {(item["version"], item["event_callbacks"]) for item in snapshots},
            {("a", 183), ("b", 0)},
        )

    def test_open_chat_uses_current_documented_buyer_identity_fields(self):
        server = BrowserServer(SimpleNamespace())
        server.execute = Mock(return_value={"ok": True, "result": None})
        result = server.open_conversation(
            "buyer-nick",
            security_uid="2217298756354",
        )
        expression = server.execute.call_args.args[0]
        self.assertIn('"nick": "cntaobaobuyer-nick"', expression)
        self.assertIn('"securityUID": "2217298756354"', expression)
        self.assertIn('"bizDomain": "taobao"', expression)
        self.assertIn(r'"sceneParam": "{\"toRole\":\"buyer\"}"', expression)
        self.assertEqual(result["ability_nick"], "cntaobaobuyer-nick")

    def test_remote_history_recovers_real_nick_from_numeric_uid(self):
        buyer_id = "2217298756354.1-11789284.1#11001@cntaobao"
        payload = {
            "nickname": "2217298756354",
            "messages": [
                {"role": "user", "nickname": "tb1238562800"},
                {"role": "mall_cs", "nickname": "11789284"},
                {"role": "user", "nickname": "2217298756354"},
            ],
        }
        self.assertEqual(
            preferred_buyer_nick(payload, buyer_id, "2217298756354"),
            "tb1238562800",
        )

    def test_workbench_open_conversation_is_purely_local_and_has_no_fixed_wait(self):
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        focus = source.index("focused = self.app.focus_qianniu()")
        opened = source.index("response = self.app.browser.open_conversation(", focus)
        endpoint = source[source.index('if path == "/api/v1/open-conversation":'):opened]
        self.assertLess(focus, opened)
        self.assertNotIn("resolve_buyer_nick", endpoint)
        self.assertNotIn("wait_until_connected", endpoint)

    def test_open_conversation_uses_default_local_request_timeout(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("body: JSON.stringify(session)}", source)
        self.assertNotIn("timeout: 20000", source)

    def test_conversation_eviction_also_drops_its_passive_fingerprint(self):
        source = (ROOT / "browser_bridge.js").read_text(encoding="utf-8")
        self.assertIn("delete passiveCacheFingerprints[removed]", source)


class LauncherUpgradeSafetyTests(unittest.TestCase):
    def test_passive_upgrade_refuses_while_old_bridge_is_running_without_status(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(launcher, "app_root", return_value=Path(directory)), \
                patch.object(launcher, "load_config_silent", return_value={}), \
                patch.object(launcher, "ensure_device_identity", return_value=True), \
                patch.object(launcher.config_dialog, "needs_setup", return_value=False), \
                patch.object(launcher, "fetch_status", return_value=None), \
                patch.object(launcher, "running_qianniu_processes", return_value=["qianniuagent.exe"]), \
                patch.object(launcher, "message_box"), \
                patch.object(launcher, "start_process") as start_process:
            self.assertEqual(launcher.start_all(), 5)
            start_process.assert_not_called()


class LauncherLifecycleTests(unittest.TestCase):
    def test_running_bridge_receives_saved_brain_configuration(self):
        config = {
            "workbench_port": 18767,
            "workbench_token": "local-workbench-token",
            "brain_enabled": True,
            "brain_server_url": "http://brain.example/",
            "brain_agent_token": "shared-seat-token",
            "brain_agent_id": "brain-agent",
            "brain_agent_name": "seat-a",
            "brain_ai_reply_enabled": True,
            "brain_remote_open_chat_enabled": True,
        }
        with patch.object(
            launcher.urllib.request,
            "urlopen",
            return_value=FakeHttpResponse(200, {"ok": True}),
        ) as urlopen:
            payload = launcher.sync_running_brain_config(config)
        request = urlopen.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertTrue(payload["ok"])
        self.assertEqual(request.full_url, "http://127.0.0.1:18767/api/v1/brain/config")
        self.assertEqual(headers["x-workbench-token"], "local-workbench-token")
        self.assertEqual(sent["agent_token"], "shared-seat-token")
        self.assertTrue(sent["enabled"])

    def test_configure_hot_reloads_brain_when_bridge_is_already_running(self):
        config = {"workbench_token": "local-workbench-token"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(launcher, "app_root", return_value=Path(directory)), \
                patch.object(launcher, "load_config_silent", return_value=dict(config)), \
                patch.object(launcher, "ensure_device_identity", return_value=True), \
                patch.object(launcher.config_dialog, "show_and_save", return_value=config), \
                patch.object(launcher, "fetch_status", return_value={"ok": True}), \
                patch.object(launcher, "sync_running_brain_config") as sync:
            self.assertEqual(launcher.configure(), 0)
        sync.assert_called_once_with(config)

    def test_webui_injection_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "webui.zip"
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr(launcher.CHAT_ENTRY, "<html><body>chat</body></html>")
            injection = launcher.build_injection(
                {"ws_host": "127.0.0.1", "ws_port": 42110, "browser_token": "token"},
                "window.__bridge_test = true;",
            )
            self.assertTrue(launcher.inject_zip(archive, injection))
            first_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertFalse(launcher.inject_zip(archive, injection))
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), first_hash)

    def test_role_process_removes_its_own_pid_file_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "state" / "docked_workbench.pid"
            pid_file.parent.mkdir(parents=True)
            pid_file.write_text(str(qianniu_app.os.getpid()), encoding="ascii")
            with patch.object(qianniu_app, "__file__", str(root / "qianniu_app.py")):
                qianniu_app.clear_role_pid("dock")
            self.assertFalse(pid_file.exists())

    def test_stop_all_keeps_pid_file_when_process_cannot_be_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "state" / "standalone_bridge.pid"
            pid_file.parent.mkdir(parents=True)
            pid_file.write_text("12345", encoding="ascii")
            with patch.object(launcher, "app_root", return_value=root), \
                    patch.object(launcher, "request_bridge_shutdown", return_value=False), \
                    patch.object(launcher, "wait_for_exit", return_value=False), \
                    patch.object(launcher, "kill_tree", return_value=False), \
                    patch.object(launcher, "message_box") as message_box, \
                    patch("docked_workbench.close_existing", return_value=0):
                launcher.stop_all()
            self.assertTrue(pid_file.is_file())
            self.assertEqual(message_box.call_args.args[2], launcher.MB_ICONERROR)

    def test_stop_all_removes_pid_file_after_confirmed_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "state" / "standalone_bridge.pid"
            pid_file.parent.mkdir(parents=True)
            pid_file.write_text("12345", encoding="ascii")
            with patch.object(launcher, "app_root", return_value=root), \
                    patch.object(launcher, "request_bridge_shutdown", return_value=True), \
                    patch.object(launcher, "wait_for_exit", return_value=True), \
                    patch.object(launcher, "kill_tree") as kill_tree, \
                    patch.object(launcher, "message_box") as message_box, \
                    patch("docked_workbench.close_existing", return_value=1):
                launcher.stop_all()
            self.assertFalse(pid_file.exists())
            kill_tree.assert_not_called()
            message_box.assert_not_called()

    def test_failed_start_cleans_the_process_it_spawned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / launcher.PASSIVE_UPGRADE_MARKER).write_text("installed", encoding="ascii")
            process = Mock(pid=12345)
            process.poll.return_value = None
            config = {"config_defaults_revision": 1, "dock_enabled": False}
            with patch.object(launcher, "app_root", return_value=root), \
                    patch.object(launcher, "load_config_silent", return_value=config), \
                    patch.object(launcher, "ensure_device_identity", return_value=True), \
                    patch.object(launcher.config_dialog, "needs_setup", return_value=False), \
                    patch.object(launcher, "fetch_status", return_value=None), \
                    patch.object(launcher, "inject_all_webui"), \
                    patch.object(launcher, "start_process", return_value=process), \
                    patch.object(launcher, "kill_tree", return_value=True) as kill_tree, \
                    patch.object(launcher, "message_box"), \
                    patch.object(launcher.time, "time", side_effect=[0.0, 26.0]):
                self.assertEqual(launcher.start_all(), 5)
            kill_tree.assert_called_once_with(12345)
            self.assertFalse((state / "standalone_bridge.pid").exists())

    def test_start_exception_also_cleans_an_already_spawned_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / launcher.PASSIVE_UPGRADE_MARKER).write_text("installed", encoding="ascii")
            process = Mock(pid=12345)
            process.poll.return_value = None
            config = {"config_defaults_revision": 1, "dock_enabled": False}
            with patch.object(launcher, "app_root", return_value=root), \
                    patch.object(launcher, "load_config_silent", return_value=config), \
                    patch.object(launcher, "ensure_device_identity", return_value=True), \
                    patch.object(launcher.config_dialog, "needs_setup", return_value=False), \
                    patch.object(launcher, "fetch_status", return_value=None), \
                    patch.object(launcher, "inject_all_webui"), \
                    patch.object(launcher, "start_process", return_value=process), \
                    patch.object(launcher, "write_pid", side_effect=OSError("disk full")), \
                    patch.object(launcher, "kill_tree", return_value=True) as kill_tree, \
                    patch.object(launcher, "message_box"):
                self.assertEqual(launcher.start_all(), 5)
            kill_tree.assert_called_once_with(12345)

    def test_shutdown_endpoint_stops_bridge_with_api_token(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {"api_token": "shutdown-token"}),
            stop_event=threading.Event(),
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        server.app = app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/v1/shutdown",
                data=b"{}",
                method="POST",
                headers={"X-Bridge-Token": "shutdown-token"},
            )
            with urllib.request.urlopen(request, timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 202)
            self.assertTrue(payload["ok"])
            self.assertTrue(app.stop_event.wait(1.0))
        finally:
            server.shutdown()
            server.server_close()

    def test_passive_upgrade_refuses_while_qianniu_is_running(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(launcher, "app_root", return_value=Path(directory)), \
                patch.object(launcher, "load_config_silent", return_value={}), \
                patch.object(launcher, "ensure_device_identity", return_value=True), \
                patch.object(launcher.config_dialog, "needs_setup", return_value=False), \
                patch.object(launcher, "fetch_status", return_value=None), \
                patch.object(launcher, "running_qianniu_processes", return_value=["aliworkbench.exe"]), \
                patch.object(launcher, "message_box"), \
                patch.object(launcher, "start_process") as start_process:
            self.assertEqual(launcher.start_all(), 5)
            start_process.assert_not_called()


class DockPlacementTests(unittest.TestCase):
    def test_prefers_right_side_when_monitor_has_room(self):
        result = choose_dock_rect(Rect(300, 100, 1500, 900), Rect(0, 0, 1920, 1040), 286)
        self.assertEqual(result, Rect(1500, 100, 1786, 900))

    def test_falls_back_to_left_without_covering_qianniu(self):
        result = choose_dock_rect(Rect(500, 80, 1820, 980), Rect(0, 0, 1920, 1040), 286)
        self.assertEqual(result, Rect(214, 80, 500, 980))

    def test_dock_edge_is_resource_bounded_and_cleans_its_profile_processes(self):
        source = (ROOT / "docked_workbench.py").read_text(encoding="utf-8")
        self.assertIn('"--disk-cache-size=52428800"', source)
        self.assertIn('"--media-cache-size=10485760"', source)
        self.assertIn("self.stop_profile_edge(profile)", source)
        self.assertIn('"--user-data-dir" in command', source)
        self.assertNotIn('"--force-renderer-accessibility"', source)

    def test_dock_recognizes_edge_process_after_single_instance_reparenting(self):
        dock = DockedWorkbench.__new__(DockedWorkbench)
        dock.edge = None
        reparented = SimpleNamespace(pid=123)
        with patch.object(dock, "profile_edge_processes", return_value=[reparented]):
            self.assertEqual(dock.edge_pids(), {123})

    def test_dock_relaunches_edge_when_its_window_disappears(self):
        dock = DockedWorkbench.__new__(DockedWorkbench)
        dock.config = {"dock_width": 286, "dock_poll_interval_seconds": 0.25}
        dock.stop_event = threading.Event()
        dock.edge = None
        dock.dock_hwnd = 0
        dock._dock_visible = False
        dock._dock_rect = None
        target = SimpleNamespace(
            hwnd=10, pid=10, title="千牛工作台", visible=True, minimized=True,
            rect=Rect(100, 100, 900, 700),
        )
        edge_window = SimpleNamespace(hwnd=20, pid=20, visible=True)
        launches = []

        def launch(initial):
            launches.append(initial)
            dock.edge = SimpleNamespace(pid=30)

        waits = 0

        def wait(_delay):
            nonlocal waits
            waits += 1
            if waits == 3:
                dock.stop_event.set()

        dock.stop_event.wait = wait
        with patch.object(dock, "wait_for_workbench"), \
                patch.object(dock, "qianniu_pids", return_value={10}), \
                patch.object(dock, "launch", side_effect=launch), \
                patch.object(dock, "find_dock_window", side_effect=[edge_window, None, None]), \
                patch.object(dock, "set_dock_visible"), \
                patch.object(dock, "close"), \
                patch("docked_workbench.Win32.windows", return_value=[target]), \
                patch("docked_workbench.Win32.work_area", return_value=Rect(0, 0, 1920, 1040)), \
                patch("docked_workbench.time.time", side_effect=[100.0, 100.0, 102.0]):
            self.assertEqual(dock.run(), 0)

        self.assertEqual(len(launches), 2)


class CanonicalIdentityTests(unittest.TestCase):
    def test_same_platform_message_collapses_across_capture_ids(self):
        first = {
            "platform": "taobao",
            "event_id": "capture-python",
            "msg_id": "4255971304943.PNM",
            "original_msg_id": "4255971304943.PNM",
            "content": "0248",
        }
        second = {
            "platform": "cntaobao",
            "event_id": "capture-browser",
            "msg_id": "4255971304943.PNM",
            "original_msg_id": "4255971304943.PNM",
            "content": "0248",
        }
        expected = "qn-msg-v1|taobao|4255971304943.PNM"
        self.assertEqual(canonical_event_id(first), expected)
        self.assertEqual(canonical_event_id(second), expected)

    def test_sqlite_upsert_keeps_richer_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            base = {
                "platform": "taobao",
                "msg_id": "m-1",
                "original_msg_id": "m-1",
                "content": "hello",
                "buyer_id": "buyer-1",
            }
            rich = dict(base, buyer_nick="buyer", goods_id="goods-1")
            poor = dict(base, buyer_nick="")
            event_id, changed = db.upsert_event(base)
            self.assertTrue(changed)
            self.assertEqual(event_id, "qn-msg-v1|taobao|m-1")
            db.upsert_event(rich)
            db.upsert_event(poor)
            rows = db.pending()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["buyer_nick"], "buyer")
            self.assertEqual(rows[0]["payload"]["goods_id"], "goods-1")

    def test_claimed_or_delivered_message_cannot_be_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            base = {
                "platform": "taobao",
                "msg_id": "4255971304943.PNM",
                "original_msg_id": "4255971304943.PNM",
                "content": "0248",
                "account": "seller",
                "buyer_id": "buyer-1",
                "source": "realtime",
            }
            event_id, changed = db.upsert_event(base)
            self.assertTrue(changed)
            claimed = db.claim_pending()
            self.assertEqual(len(claimed), 1)

            duplicate = dict(base, source="history-poll", buyer_nick="buyer")
            duplicate_id, changed = db.upsert_event(duplicate)
            self.assertEqual(duplicate_id, event_id)
            self.assertFalse(changed)
            self.assertEqual(db.counts()["sending"], 1)

            self.assertTrue(db.mark_delivered(event_id, claimed[0]["revision"]))
            _duplicate_id, changed = db.upsert_event(duplicate)
            self.assertFalse(changed)
            self.assertEqual(db.counts()["pending"], 0)
            self.assertEqual(db.counts()["delivered"], 1)

    def test_delivered_message_can_repair_reversed_login_identity_without_requeue(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            reversed_event = {
                "platform": "taobao",
                "msg_id": "identity-repair-1",
                "original_msg_id": "identity-repair-1",
                "content": "hello",
                "account": "tb136202715",
                "buyer_id": "4054500565.1-11789284.1#11001@cntaobao",
                "buyer_nick": "sbpgklso",
                "role": "user",
            }
            event_id, _changed = db.upsert_event(reversed_event)
            claimed = db.claim_pending()
            self.assertTrue(db.mark_delivered(event_id, claimed[0]["revision"]))

            corrected = dict(
                reversed_event,
                account="sbpgklso",
                buyer_nick="tb136202715",
                role="mall_cs",
                seller_identity_source="loginid",
            )
            corrected_id, changed = db.upsert_event(corrected)

            self.assertEqual(corrected_id, event_id)
            self.assertFalse(changed)
            self.assertEqual(db.counts()["pending"], 0)
            self.assertEqual(db.counts()["delivered"], 1)
            payload = db.event_payload(event_id)
            self.assertEqual(payload["account"], "sbpgklso")
            self.assertEqual(payload["buyer_nick"], "tb136202715")
            self.assertEqual(payload["role"], "mall_cs")
            self.assertEqual(payload["seller_identity_source"], "loginid")


class NativeFrameTests(unittest.TestCase):
    def test_native_callback_frame_normalizes_to_browser_identity(self):
        frame = {
            "req_action": 40,
            "data": {
                "mcode": {"messageId": "native-1", "clientId": "client-1"},
                "sendTime": "1786635957498",
                "fromid": {"targetId": "buyer-1", "nick": "buyer"},
                "toid": {"targetId": "seller-1", "nick": "seller"},
                "loginid": {"targetId": "seller-1", "nick": "seller"},
                "originalData": {"text": "hello"},
                "msgtype": "text",
            },
        }
        events = normalize_native_frame(json.dumps(frame))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "qn-msg-v1|taobao|native-1")
        self.assertEqual(events[0]["role"], "user")
        self.assertEqual(events[0]["buyer_id"], "buyer-1")
        self.assertEqual(events[0]["content"], "hello")


class DeliveryTests(unittest.TestCase):
    def test_observation_mode_does_not_queue_for_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = StandaloneBridge.__new__(StandaloneBridge)
            bridge.config = Config(Path(directory) / "config.json", {"delivery_enabled": False})
            bridge.db = StateDB(Path(directory) / "state.sqlite3")
            bridge.delivery = SimpleNamespace(wakeup=threading.Event())
            event_id, changed = bridge.ingest_event({
                "platform": "taobao",
                "msg_id": "observed-1",
                "original_msg_id": "observed-1",
                "content": "hello",
            })
            self.assertEqual(event_id, "qn-msg-v1|taobao|observed-1")
            self.assertFalse(changed)
            self.assertEqual(bridge.db.pending(), [])

    def test_fake_gateway_ack_completes_exactly_one_delivery(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                received.append({"path": self.path, "headers": self.headers, "payload": payload})
                acks = [
                    {"event_id": item["event_id"], "committed": True}
                    for item in payload["events"]
                ]
                body = json.dumps({"ok": True, "event_acks": acks}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = Config(Path(directory) / "config.json", {
                    "gateway_url": f"http://127.0.0.1:{server.server_port}",
                    "gateway_agent_id": "agent-test",
                    "gateway_agent_token": "token-test",
                    "device_id": "device-test",
                    "delivery_timeout_seconds": 1.0,
                })
                db = StateDB(Path(directory) / "state.sqlite3")
                app = SimpleNamespace(config=config, db=db, stop_event=threading.Event())
                worker = DeliveryWorker(app)
                event = {
                    "platform": "taobao",
                    "msg_id": "gateway-1",
                    "original_msg_id": "gateway-1",
                    "content": "hello",
                    "account": "seller",
                    "buyer_id": "buyer-1",
                }
                event_id, _changed = db.upsert_event(event)
                rows = db.claim_pending()
                committed = worker.post([row["payload"] for row in rows])
                self.assertEqual(committed, {event_id})
                self.assertTrue(db.mark_delivered(event_id, rows[0]["revision"]))
                self.assertEqual(db.counts()["delivered"], 1)
                self.assertEqual(db.claim_pending(), [])

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["path"], "/api/local-seat/v1/events")
            self.assertEqual(received[0]["headers"]["X-Agent-Token"], "token-test")
            self.assertEqual(received[0]["payload"]["events"][0]["event_id"], event_id)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)


class BrainConnectorTests(unittest.TestCase):
    def make_config(self, path, **overrides):
        values = {
            "brain_enabled": True,
            "brain_server_url": "http://127.0.0.1:1",
            "brain_agent_token": "brain-token",
            "brain_agent_id": "brain-agent",
            "brain_agent_name": "test seat",
            "brain_ai_reply_enabled": True,
            "brain_remote_open_chat_enabled": False,
            "device_id": "device-test",
            "brain_request_timeout_seconds": 1.0,
            "brain_command_poll_seconds": 0.0,
        }
        values.update(overrides)
        return Config(path, values)

    def test_legacy_brain_protocol_paths_headers_and_payloads(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def handle_request(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else None
                received.append((self.command, self.path, dict(self.headers), body))
                payload = {"ok": True}
                if self.path == "/api/bridge/v1/register":
                    payload["agent_id"] = "assigned-agent"
                if self.path == "/api/status":
                    payload.update({
                        "brain_mode": "tanyu_shadow",
                        "send_mode": "observe",
                        "own_auto_send_enabled": False,
                        "armed_shop_count": 0,
                        "shadow": {"enabled": True, "running": True},
                    })
                if "/commands?" in self.path:
                    payload["commands"] = []
                if self.path == "/api/bridge/v1/events":
                    payload["event_acks"] = [
                        {"event_id": row["event_id"], "committed": True}
                        for row in body["events"]
                    ]
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = handle_request
            do_POST = handle_request

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                db = StateDB(Path(directory) / "state.sqlite3")
                config = self.make_config(
                    Path(directory) / "config.json",
                    brain_server_url=f"http://127.0.0.1:{server.server_port}",
                )
                app = SimpleNamespace(
                    config=config,
                    db=db,
                    stop_event=threading.Event(),
                    status=lambda include_brain=True: {"ok": True, "platform": "taobao"},
                )
                brain = BrainConnector(app)
                db.upsert_event({
                    "platform": "taobao",
                    "msg_id": "heartbeat-shop",
                    "account": "shop-a",
                    "buyer_id": "buyer.1-seller.1#11001@cntaobao",
                    "buyer_nick": "buyer",
                    "role": "user",
                    "content": "hello",
                    "ts": time.time(),
                })
                brain.register()
                self.assertFalse(brain.event_upload_ready())
                brain.heartbeat()
                self.assertTrue(brain.event_upload_ready())
                brain.refresh_server_status()
                event = {
                    "platform": "taobao", "event_id": "event-1", "content": "hello",
                    "account": "shop-a", "captured_at_ms": 1_700_000_000_000,
                }
                committed = brain.upload_events([
                    {"event_id": "event-1", "revision": "r1", "payload": event}
                ])
                self.assertEqual(committed, {"event-1"})
                self.assertEqual(brain.pull_commands(), [])

            paths = [row[1].split("?", 1)[0] for row in received]
            self.assertEqual(paths, [
                "/api/bridge/v1/register",
                "/api/bridge/v1/heartbeat",
                "/api/status",
                "/api/bridge/v1/events",
                "/api/bridge/v1/commands",
            ])
            self.assertTrue(all(row[2]["X-Agent-Token"] == "brain-token" for row in received))
            self.assertEqual(received[0][2]["X-Agent-Id"], "brain-agent")
            self.assertTrue(all(row[2]["X-Agent-Id"] == "assigned-agent" for row in received[1:]))
            self.assertEqual(received[0][3]["version"], VERSION)
            heartbeat_status = received[1][3]["status"]
            self.assertEqual(heartbeat_status["platform"], "taobao")
            self.assertEqual(heartbeat_status["accounts_seen"], ["shop-a"])
            self.assertEqual(heartbeat_status["shops"][0]["shop_id"], "tb_nick_shop-a")
            self.assertFalse(heartbeat_status["dry_run"])
            uploaded_event = received[3][3]["events"][0]
            self.assertEqual(uploaded_event["type"], "message")
            self.assertEqual(uploaded_event["shop_id"], "tb_nick_shop-a")
            self.assertEqual(uploaded_event["agent_id"], "assigned-agent")
            self.assertEqual(uploaded_event["captured_at"], 1_700_000_000.0)
            self.assertEqual(brain.server_status["brain_mode"], "tanyu_shadow")
            self.assertFalse(brain.server_status["own_auto_send_enabled"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_only_explicitly_enqueued_events_can_reach_brain(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            old_id, _changed = db.upsert_event({
                "platform": "taobao", "msg_id": "old", "content": "old message",
            })
            self.assertEqual(db.brain_event_counts()["pending"], 0)
            new_id, _changed = db.upsert_event({
                "platform": "taobao", "msg_id": "new", "content": "new message",
            })
            self.assertTrue(db.enqueue_brain_event(new_id))
            rows = db.claim_brain_events(0)
            self.assertEqual([row["event_id"] for row in rows], [new_id])
            self.assertNotIn(old_id, [row["event_id"] for row in rows])

    def test_register_backfills_only_current_session_local_events(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            old_id, _changed = db.upsert_event({
                "platform": "taobao", "msg_id": "old", "content": "old message",
            })
            config = self.make_config(Path(directory) / "config.json")
            app = SimpleNamespace(config=config, db=db, stop_event=threading.Event())
            brain = BrainConnector(app)
            new_id, _changed = db.upsert_event({
                "platform": "taobao", "msg_id": "new", "content": "new message",
            })
            projection_id, _changed = db.upsert_local_projection({
                "platform": "taobao", "msg_id": "projection", "content": "draft",
                "source": "brain_shadow", "capture_mode": "brain_projection",
            })
            brain.request = Mock(return_value={"ok": True, "agent_id": "assigned-agent"})
            brain.register()
            rows = db.claim_brain_events(0)
            queued_ids = {row["event_id"] for row in rows}
        self.assertEqual(queued_ids, {new_id})
        self.assertNotIn(old_id, queued_ids)
        self.assertNotIn(projection_id, queued_ids)

    def test_non_retryable_brain_rejection_is_a_terminal_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            config = self.make_config(Path(directory) / "config.json")
            app = SimpleNamespace(config=config, db=db, stop_event=threading.Event())
            brain = BrainConnector(app)
            brain.request = Mock(return_value={
                "event_acks": [{
                    "event_id": "invalid-1",
                    "status": "rejected",
                    "committed": False,
                    "retryable": False,
                }],
            })
            committed = brain.upload_events([{
                "event_id": "invalid-1",
                "revision": "r1",
                "payload": {
                    "event_id": "invalid-1",
                    "account": "shop-a",
                    "content": "",
                },
            }])
            self.assertEqual(committed, {"invalid-1"})

    def test_taobao_system_tps_image_is_not_uploaded_as_a_buyer_message(self):
        system_url = (
            "https://gw.alicdn.com/imgextra/i1/"
            "O1CN01yEODLa1LDPyVDCknd_!!6000000001265-2-tps-807-138.png"
        )
        system_event = {
            "event_id": "system-image-1",
            "msg_id": "system-image-1",
            "original_msg_id": "system-image-1",
            "account": "seller",
            "buyer_id": "buyer",
            "role": "user",
            "content": system_url,
            "image_url": system_url,
            "media_url": system_url,
            "raw_type": "129",
            "template_name": "taobao_image",
        }
        self.assertEqual(
            brain_event_suppression_reason(system_event), "taobao_system_tps_asset"
        )
        self.assertEqual(brain_event_suppression_reason({
            **system_event,
            "content": "https://img.alicdn.com/bao/uploaded/buyer-photo.jpg",
            "image_url": "https://img.alicdn.com/bao/uploaded/buyer-photo.jpg",
            "media_url": "https://img.alicdn.com/bao/uploaded/buyer-photo.jpg",
        }), "")

        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            config = self.make_config(Path(directory) / "config.json")
            fake_brain = Mock()
            fake_brain.configured.return_value = True
            fake_app = SimpleNamespace(
                config=config,
                db=db,
                delivery=SimpleNamespace(wakeup=threading.Event()),
                context=None,
                brain=fake_brain,
            )
            _event_id, changed = StandaloneBridge.ingest_event(fake_app, system_event)
            self.assertTrue(changed)
            self.assertEqual(db.brain_event_counts()["pending"], 0)
            self.assertTrue(db.pending()[0]["payload"]["brain_suppressed"])
            fake_brain.record.assert_called_once()
            db.upsert_local_projection({
                "platform": "taobao",
                "msg_id": "shadow-system-image-1",
                "account": "seller",
                "buyer_id": "buyer",
                "role": "assistant_simulated",
                "content": "图片内容看不清",
                "parent_msg_id": "system-image-1",
                "ts": 1_700_000_001,
                "captured_at_ms": 1_700_000_001_000,
            })
            visible = db.workbench_messages("seller", "buyer")
            self.assertEqual([row["content"] for row in visible], [system_url])

            app = SimpleNamespace(config=config, db=db, stop_event=threading.Event())
            brain = BrainConnector(app)
            brain.request = Mock(side_effect=AssertionError("system asset must stay local"))
            committed = brain.upload_events([{
                "event_id": "system-image-1", "revision": "r1", "payload": system_event,
            }])
            self.assertEqual(committed, {"system-image-1"})
            brain.request.assert_not_called()

    def test_center_ai_draft_is_projected_locally_without_requeueing(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            config = self.make_config(Path(directory) / "config.json")
            app = SimpleNamespace(config=config, db=db, stop_event=threading.Event())
            brain = BrainConnector(app)
            brain.request = Mock(return_value={
                "buyer_id": "buyer.1-seller.1#11001@cntaobao",
                "nickname": "buyer",
                "messages": [{
                    "msg_id": "shadow-draft-1",
                    "role": "assistant_simulated",
                    "content": "center draft",
                    "delivery_status": "simulated",
                    "shadow_status": "succeeded",
                    "parent_msg_id": "buyer-message-1",
                    "ts": 1_700_000_020,
                }],
            })

            imported, parents = brain.sync_session_drafts(
                "seller", "buyer.1-seller.1#11001@cntaobao", "buyer"
            )
            self.assertEqual(imported, 1)
            self.assertEqual(parents, {"buyer-message-1"})
            messages = db.workbench_messages(
                "seller", "buyer.1-seller.1#11001@cntaobao"
            )
            self.assertEqual(messages[0]["role"], "draft")
            self.assertEqual(messages[0]["status"], "ai_draft")
            self.assertEqual(messages[0]["content"], "center draft")
            self.assertEqual(db.brain_event_counts()["pending"], 0)

            imported_again, _parents = brain.sync_session_drafts(
                "seller", "buyer.1-seller.1#11001@cntaobao", "buyer"
            )
            self.assertEqual(imported_again, 0)

    def test_center_handoff_queue_syncs_and_only_clears_brain_owned_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            config = self.make_config(Path(directory) / "config.json")
            app = SimpleNamespace(config=config, db=db, stop_event=threading.Event())
            brain = BrainConnector(app)
            brain.request = Mock(return_value={
                "sessions": [{
                    "account": "shop-a", "buyer_id": "buyer-a", "handoff": True,
                    "handoff_reason": "大脑判断置信度不足",
                }],
                "pages": 1,
            })

            result = brain.sync_brain_handoffs()
            self.assertEqual(result, {"remote": 1, "changed": 1, "cleared": 0})
            control = db.session_control("shop-a", "buyer-a")
            self.assertEqual(control["ai_mode"], "human")
            self.assertEqual(control["handoff_source"], "brain")
            self.assertEqual(control["handoff_reason"], "大脑判断置信度不足")
            self.assertIn("scope=handoff", brain.request.call_args.args[1])

            db.set_session_control("shop-b", "buyer-b", "human", "人工主动接管", "manual")
            brain.request = Mock(return_value={"sessions": [], "pages": 1})
            result = brain.sync_brain_handoffs()
            self.assertEqual(result["cleared"], 1)
            self.assertEqual(db.session_control("shop-a", "buyer-a")["ai_mode"], "ai")
            self.assertEqual(db.session_control("shop-b", "buyer-b")["ai_mode"], "human")

    def test_repeated_brain_command_never_calls_send_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            config = self.make_config(Path(directory) / "config.json")
            app = SimpleNamespace(
                config=config,
                db=db,
                stop_event=threading.Event(),
                send_text=Mock(return_value={
                    "ok": True, "status": "submitted", "request_id": "brain-command-test",
                }),
            )
            brain = BrainConnector(app)
            brain.request = Mock(return_value={"ok": True})
            command = {
                "id": "command-1",
                "type": "send_text",
                "buyer_id": "buyer.1-seller.1#11001@cntaobao",
                "content": "AI reply",
                "lease_token": "lease-1",
            }
            brain.handle_command(command)
            brain.handle_command(dict(command))
            app.send_text.assert_called_once()
            self.assertEqual(db.brain_command_counts()["acknowledged"], 1)

    def test_released_command_uses_new_lease_without_sending_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            config = self.make_config(Path(directory) / "config.json")
            app = SimpleNamespace(
                config=config,
                db=db,
                stop_event=threading.Event(),
                send_text=Mock(return_value={
                    "ok": True, "status": "submitted", "request_id": "brain-command-test",
                }),
            )
            brain = BrainConnector(app)
            reports = []

            def request(method, path, body=None, timeout=None):
                if path.endswith("/result"):
                    reports.append(body)
                    if len(reports) == 1:
                        raise RuntimeError("temporary result ACK failure")
                return {"ok": True}

            brain.request = Mock(side_effect=request)
            command = {
                "id": "command-released",
                "type": "send_text",
                "buyer_id": "buyer.1-seller.1#11001@cntaobao",
                "content": "正常客服回复",
                "lease_token": "lease-1",
                "state": "leased",
            }
            with self.assertRaises(RuntimeError):
                brain.handle_command(command)
            brain.handle_command({
                **command,
                "lease_token": "lease-2",
                "state": "re-leased",
            })
            app.send_text.assert_called_once()
            self.assertEqual(reports[-1]["result"]["lease_token"], "lease-2")
            self.assertEqual(db.brain_command_counts()["acknowledged"], 1)

    def test_brain_safety_handoff_and_shop_scope_block_before_send(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            config = self.make_config(Path(directory) / "config.json")
            send_text = Mock(return_value={"ok": True, "status": "submitted"})
            app = SimpleNamespace(
                config=config,
                db=db,
                stop_event=threading.Event(),
                send_text=send_text,
            )
            brain = BrainConnector(app)
            base = {
                "type": "send_text",
                "account": "shop-a",
                "buyer_id": "buyer.1-seller.1#11001@cntaobao",
            }
            blocked = brain.execute_command({
                **base, "id": "unsafe", "content": "这是测试发送",
            })
            self.assertEqual(blocked["via"], "safety")
            db.set_session_control("shop-a", base["buyer_id"], "human", "售后纠纷")
            blocked = brain.execute_command({
                **base, "id": "handoff", "content": "好的亲，这边帮您看一下",
            })
            self.assertEqual(blocked["via"], "handoff")
            db.set_session_control("shop-a", base["buyer_id"], "ai")
            brain.allowed_shop_ids = ["tb_nick_other-shop"]
            blocked = brain.execute_command({
                **base, "id": "scope", "content": "好的亲，这边帮您看一下",
            })
            self.assertEqual(blocked["via"], "client_shop_scope_guard")
            send_text.assert_not_called()


class AiPolicyTests(unittest.TestCase):
    def test_legacy_safety_and_handoff_patterns_are_preserved(self):
        self.assertEqual(outbound_safety_result("???")["status"], "blocked")
        self.assertEqual(outbound_safety_result("加微信处理")["safety_reason"], "疑似引导站外联系")
        self.assertIsNone(outbound_safety_result("好的亲，这边帮您看一下"))
        self.assertEqual(suggested_handoff_reason({"content": "我要退款"}), "售后纠纷")
        self.assertEqual(suggested_handoff_reason({"raw_type": "video"}), "买家发送视频")

    def test_handoff_control_is_persistent_and_can_resume_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            control = db.set_session_control(
                "shop-a", "buyer-a", "human", "投诉维权", "automatic", 3600,
            )
            self.assertEqual(control["ai_mode"], "human")
            self.assertEqual(db.session_control("shop-a", "buyer-a")["handoff_reason"], "投诉维权")
            db.set_session_control("shop-a", "buyer-a", "ai")
            self.assertEqual(db.session_control("shop-a", "buyer-a")["ai_mode"], "ai")

    def test_automatic_handoff_never_sends_locally_and_is_reported_to_brain(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            brain = Mock()
            brain.configured.return_value = True
            app = StandaloneBridge.__new__(StandaloneBridge)
            app.config = Config(Path(directory) / "config.json", {
                "delivery_enabled": True,
                "brain_handoff_ttl_seconds": 3600.0,
            })
            app.db = db
            app.delivery = SimpleNamespace(wakeup=threading.Event())
            app.context = None
            app.brain = brain
            app.send_text = Mock()
            buyer_id = "buyer.1-seller.1#11001@cntaobao"

            _event_id, changed = app.ingest_event({
                "platform": "taobao",
                "msg_id": "buyer-msg-1",
                "original_msg_id": "buyer-msg-1",
                "account": "shop-a",
                "buyer_id": buyer_id,
                "role": "user",
                "content": "我要退款",
            })

            self.assertTrue(changed)
            app.send_text.assert_not_called()
            self.assertEqual(db.session_control("shop-a", buyer_id)["ai_mode"], "human")
            self.assertEqual(db.brain_event_counts()["pending"], 1)


class ContextEnrichmentTests(unittest.TestCase):
    def test_brain_context_envelope_keeps_current_and_legacy_shapes(self):
        product = {
            "goods_id": "799474439068",
            "goods_name": "测试商品",
            "goods_price": "49.90",
        }
        order = {
            "order_id": "1234567890123456789",
            "items": [product],
        }
        event = BrainConnector.compatible_context_event({
            **product,
            "inquiry_goods": [product],
            "recent_orders": [order],
            "order_info": {"context_received": True},
        })
        self.assertEqual(event["product_id"], product["goods_id"])
        self.assertEqual(event["product_name"], product["goods_name"])
        self.assertEqual(event["order_info"]["products"], [product])
        self.assertEqual(event["order_info"]["orders"], [order])
        self.assertEqual(event["order_context"]["selected_order"], order)
        self.assertEqual(event["local_context_lookup"]["order_count"], 1)

    def test_fetch_uses_qianniu_trade_query_security_buyer_uid(self):
        browser = SimpleNamespace(execute=Mock(return_value={
            "ok": True,
            "items": {"ok": True, "value": {
                "data": {"underInquiryItemList": [{
                    "itemId": "799474439068", "title": "测试商品",
                }]},
            }},
            "orders": {"ok": True, "value": {
                "data": {"orderList": [{
                    "bizOrderId": "1234567890123456789",
                    "itemList": [{"itemId": "799474439068", "title": "测试商品"}],
                }]},
            }},
        }))
        app = SimpleNamespace(
            browser=browser,
            config={"context_enrich_timeout_seconds": 6.0},
        )
        enriched = ContextEnricher(app).fetch("2217298756354")
        expression = browser.execute.call_args.args[0]
        self.assertIn("{encryptId}", expression)
        self.assertIn("{securityBuyerUid: encryptId, bizOrderId: \"\"}", expression)
        self.assertEqual(enriched["order_id"], "1234567890123456789")
        self.assertTrue(enriched["order_info"]["context_received"])
        self.assertEqual(enriched["order_info"]["selected_order"]["order_id"], "1234567890123456789")
        self.assertEqual(enriched["local_context_lookup"]["product_count"], 1)

    def _context_app(self, responses):
        browser = SimpleNamespace(execute=Mock(side_effect=responses))
        return SimpleNamespace(
            browser=browser,
            config={"context_enrich_timeout_seconds": 6.0},
        )

    @staticmethod
    def _order_response(orders):
        return {
            "ok": True,
            "items": {"ok": True, "value": {"data": {"underInquiryItemList": []}}},
            "orders": {"ok": True, "value": {"data": {"orderList": orders}}},
        }

    def test_enrichment_queue_is_bounded_and_applies_backpressure_without_loss(self):
        stop_event = threading.Event()
        enricher = ContextEnricher(SimpleNamespace(
            config={"context_enrich_enabled": True},
            stop_event=stop_event,
        ))
        event = {
            "role": "user",
            "buyer_id": "123456789.1-seller#11001@cntaobao",
        }
        for index in range(enricher.MAX_PENDING):
            enricher.enqueue(f"event-{index}", event)
        producer = threading.Thread(
            target=enricher.enqueue,
            args=("overflow-event", event),
        )
        producer.start()
        time.sleep(0.05)
        self.assertTrue(producer.is_alive())
        self.assertEqual(enricher.pending.qsize(), enricher.MAX_PENDING)
        enricher.pending.get_nowait()
        producer.join(1.0)
        self.assertFalse(producer.is_alive())
        self.assertEqual(enricher.pending.qsize(), enricher.MAX_PENDING)
        self.assertIn("overflow-event", enricher.queued)
        self.assertEqual(enricher.skipped, 0)

    def test_enrichment_worker_reads_the_latest_durable_event_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            event_id, _changed = db.upsert_event({
                "platform": "taobao",
                "msg_id": "latest-context",
                "original_msg_id": "latest-context",
                "account": "shop-a",
                "buyer_id": "123456789.1-seller#11001@cntaobao",
                "role": "user",
                "content": "旧内容",
            })
            stop_event = threading.Event()
            brain = SimpleNamespace(wakeup=threading.Event(), record=Mock())
            app = SimpleNamespace(
                config={"context_enrich_enabled": True},
                db=db,
                brain=brain,
                stop_event=stop_event,
            )
            enricher = ContextEnricher(app)
            enricher.fetch = Mock(return_value={"context_enrich": {"status": "found"}})
            enricher.enqueue(event_id, db.event_payload(event_id))
            updated = db.event_payload(event_id)
            updated["account"] = "shop-b"
            db.replace_event_payload(event_id, updated)
            enricher.start()
            deadline = time.time() + 2.0
            while enricher.requests == 0 and time.time() < deadline:
                time.sleep(0.01)
            stop_event.set()
            enricher.thread.join(1.0)
            self.assertEqual(enricher.fetch.call_args.args[2], "shop-b")

    def test_empty_order_result_retries_before_confirming_empty(self):
        app = self._context_app([
            self._order_response([]),
            self._order_response([]),
            self._order_response([]),
        ])
        enriched = ContextEnricher(app).fetch("2217298756354", "", "shop-a")
        self.assertEqual(app.browser.execute.call_count, 3)
        self.assertEqual(enriched["context_enrich"]["status"], "confirmed_empty")
        self.assertTrue(enriched["context_enrich"]["ok"])
        self.assertEqual(enriched["context_enrich"]["retry_count"], 2)
        self.assertTrue(enriched["order_info"]["no_orders"])

    def test_recent_valid_order_is_cached_against_short_empty(self):
        valid = self._order_response([{
            "bizOrderId": "1234567890123456789",
            "itemList": [{"itemId": "799474439068", "title": "????"}],
        }])
        app = self._context_app([
            valid,
            self._order_response([]),
            self._order_response([]),
            self._order_response([]),
        ])
        enricher = ContextEnricher(app)
        enricher.fetch("2217298756354", "", "shop-a")
        enriched = enricher.fetch("2217298756354", "", "shop-a")
        self.assertEqual(enriched["context_enrich"]["status"], "found")
        self.assertTrue(enriched["context_enrich"]["cache"])
        self.assertEqual(enriched["context_enrich"]["orders_count"], 1)
        self.assertEqual(enriched["order_info"]["orders_from_cache"], True)
        self.assertFalse(enriched["order_info"]["no_orders"])
        self.assertEqual(enriched["order_info"]["selected_order"]["order_id"], "1234567890123456789")

    def test_mtop_error_reports_error_unknown_not_success(self):
        error_response = {"ok": True, "items": {"ok": True, "value": None},
                          "orders": {"ok": False, "error": "mtop boom"}}
        app = self._context_app([error_response, error_response, error_response])
        enriched = ContextEnricher(app).fetch("2217298756354", "", "shop-a")
        self.assertEqual(enriched["context_enrich"]["status"], "error_unknown")
        self.assertFalse(enriched["context_enrich"]["ok"])
        self.assertFalse(enriched["order_info"]["context_received"])
        self.assertNotIn("no_orders", enriched["order_info"])

    def test_order_query_report_fields_are_present(self):
        app = self._context_app([
            self._order_response([{
                "bizOrderId": "1234567890123456789",
                "itemList": [{"itemId": "799474439068", "title": "????"}],
            }]),
        ])
        enriched = ContextEnricher(app).fetch("2217298756354", "", "shop-a")
        status = enriched["context_enrich"]
        for key in ("build_hash", "trace_id", "mtop_api", "ret_code",
                    "raw_order_count", "orders_count", "elapsed_ms", "retry_count"):
            self.assertIn(key, status)
        self.assertEqual(status["mtop_api"], "mtop.taobao.qianniu.cs.trade.query")
        self.assertNotIn("cookie", json.dumps(enriched, ensure_ascii=False).lower())
        self.assertNotIn("sign", json.dumps(enriched, ensure_ascii=False).lower())

    def test_parses_legacy_item_and_order_mtop_shapes(self):
        items = ContextEnricher.parse_items({
            "data": {"footPointItemList": [{
                "itemId": "799474439068",
                "title": "测试商品",
                "price": "49.90",
                "picUrl": "https://img.example/item.jpg",
            }]}
        })
        orders = ContextEnricher.parse_orders({
            "result": {"data": {"orderList": [{
                "bizOrderId": "10001",
                "totalFee": "49.90",
                "expressCompany": "测试快递",
                "invoiceNo": "SF10001",
                "itemList": [{"itemId": "799474439068", "title": "测试商品"}],
            }]}}
        })
        self.assertEqual(items[0]["goods_id"], "799474439068")
        self.assertEqual(items[0]["goods_url"], "https://item.taobao.com/item.htm?id=799474439068")
        self.assertEqual(orders[0]["order_id"], "10001")
        self.assertEqual(orders[0]["express_order_number"], "SF10001")

    def test_enrichment_updates_the_pending_brain_payload_without_requeueing_local(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            event_id, _changed = db.upsert_event({
                "platform": "taobao",
                "msg_id": "context-1",
                "content": "这个什么时候发货",
                "account": "seller",
                "buyer_id": "123456789.1-seller#11001@cntaobao",
            })
            db.enqueue_brain_event(event_id)
            self.assertTrue(db.enrich_event(event_id, {
                "order_id": "10001", "express_order_number": "SF10001",
            }))
            rows = db.claim_brain_events(0)
            self.assertEqual(rows[0]["payload"]["order_id"], "10001")
            self.assertEqual(rows[0]["payload"]["express_order_number"], "SF10001")
            self.assertEqual(db.counts()["pending"], 1)

    def test_late_context_requeues_same_brain_message_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            event_id, _changed = db.upsert_event({
                "platform": "taobao",
                "msg_id": "late-context-1",
                "content": "什么时候发货",
                "account": "seller",
                "buyer_id": "123456789.1-seller#11001@cntaobao",
            })
            db.enqueue_brain_event(event_id)
            first = db.claim_brain_events(0)
            db.finish_brain_events(first, {event_id})
            self.assertEqual(db.brain_event_counts()["delivered"], 1)

            self.assertTrue(db.enrich_event(event_id, {
                "goods_id": "799474439068",
                "order_info": {"goods_id": "799474439068"},
            }))
            self.assertEqual(db.brain_event_counts()["pending"], 1)
            replay = db.claim_brain_events(0)
            self.assertEqual(replay[0]["event_id"], event_id)
            self.assertEqual(replay[0]["payload"]["goods_id"], "799474439068")
            self.assertNotEqual(replay[0]["revision"], first[0]["revision"])


class WorkbenchTests(unittest.TestCase):
    def test_session_snapshot_is_cached_and_invalidated_by_new_events(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            base = {
                "platform": "taobao",
                "account": "seller",
                "role": "user",
                "content": "hello",
                "ts": 100.0,
            }
            db.upsert_event(dict(
                base,
                msg_id="cache-1",
                original_msg_id="cache-1",
                buyer_id="buyer-1",
                buyer_nick="buyer-1",
            ))
            real_loads = json.loads
            with patch("standalone_bridge.json.loads", wraps=real_loads) as loads:
                first = db.workbench_sessions()
                first_parse_count = loads.call_count
                second = db.workbench_sessions()
                self.assertEqual(loads.call_count, first_parse_count)
            self.assertEqual(first, second)

            db.upsert_event(dict(
                base,
                msg_id="cache-2",
                original_msg_id="cache-2",
                buyer_id="buyer-2",
                buyer_nick="buyer-2",
                ts=101.0,
            ))
            self.assertEqual(len(db.workbench_sessions()), 2)

    def test_session_keeps_local_real_nickname_when_newer_event_has_numeric_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            buyer_id = "2217298756354.1-11789284.1#11001@cntaobao"
            base = {
                "platform": "taobao",
                "account": "seller",
                "buyer_id": buyer_id,
                "role": "user",
                "content": "hello",
            }
            db.upsert_event(dict(
                base,
                msg_id="nick-real",
                original_msg_id="nick-real",
                buyer_nick="tb1238562800",
                ts=100.0,
            ))
            db.upsert_event(dict(
                base,
                msg_id="nick-numeric",
                original_msg_id="nick-numeric",
                buyer_nick="2217298756354",
                ts=200.0,
            ))

            sessions = db.workbench_sessions()

            self.assertEqual(sessions[0]["buyer_nick"], "tb1238562800")

    def test_session_snapshot_parses_each_event_once_and_caps_visible_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            for index in range(WORKBENCH_SESSION_LIMIT + 10):
                msg_id = f"limit-{index}"
                db.upsert_event({
                    "platform": "taobao",
                    "msg_id": msg_id,
                    "original_msg_id": msg_id,
                    "account": "seller",
                    "buyer_id": f"buyer-{index}",
                    "buyer_nick": f"buyer-{index}",
                    "role": "user",
                    "content": msg_id,
                    "ts": float(index + 1),
                })
            with patch.object(
                db,
                "brain_suppressed_message_ids",
                side_effect=AssertionError("session snapshot must not launch a second event scan"),
            ):
                sessions = db.workbench_sessions()
            self.assertEqual(len(sessions), WORKBENCH_SESSION_LIMIT)
            self.assertEqual(sessions[0]["buyer_id"], f"buyer-{WORKBENCH_SESSION_LIMIT + 9}")

    def test_workbench_refresh_is_throttled_and_skips_unchanged_dom_rebuilds(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("setInterval(refresh, 5000)", source)
        self.assertIn("state.sessionRevision !== nextSessionRevision", source)
        self.assertIn("if (sessionsChanged) renderSessions()", source)
        self.assertNotIn("setInterval(refresh, 1500)", source)

    def test_workbench_status_reuses_the_computed_session_snapshot(self):
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn("self.app.status(workbench_session_count=len(sessions))", source)
        self.assertIn('"sessions": workbench_session_count', source)

    def test_workbench_accepts_appbiz_as_a_receive_link(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("const receiveReady = connected || !!appbiz.receive_ready", source)
        self.assertIn("原生收发就绪", source)

    def test_conversation_search_rejects_agent_id_autofill_before_user_input(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn('name="qianniu-conversation-search-v2"', source)
        self.assertIn("let conversationSearchEdited = false", source)
        self.assertIn("/^agent-[a-z0-9-]{8,}$/i", source)
        self.assertIn("if (!rejectUnexpectedSearchAutofill()) renderSessions()", source)

    def test_docked_reception_has_handoff_filter_tab(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn('id="handoffSessionsTab"', source)
        self.assertIn("state.sessionFilter === 'handoff'", source)
        self.assertIn("item.ai_mode !== 'human'", source)
        self.assertIn("session.handoff_reason", source)

    def test_every_message_bubble_displays_its_traceable_id(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("message.msg_id || message.event_id || '-'", source)
        self.assertIn("idLine.textContent = `ID ${messageId}`", source)
        self.assertIn(".bubble-id", source)

    def test_center_drafts_are_visually_distinct_from_real_qianniu_sends(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("AI 草稿 · 未发送", source)
        self.assertIn("message.role === 'draft' ? 'draft'", source)
        self.assertIn(".draft .bubble-body", source)

    def test_composer_stays_editable_while_send_transport_is_unbound(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("el('replyInput').disabled = !state.current || state.sending", source)
        self.assertNotIn("el('replyInput').disabled = !state.current || !state.canSend", source)
        self.assertIn("if (!state.canSend)", source)
        self.assertIn("!wasSendReady && state.canSend", source)

    def test_only_docked_session_click_opens_qianniu(self):
        source = (ROOT / "workbench.html").read_text(encoding="utf-8")
        self.assertIn("selectSession(session, dockMode)", source)
        self.assertNotIn("selectSession(session, true)", source)

    def test_session_detail_reads_the_same_durable_event_content(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            db.upsert_event({
                "platform": "taobao",
                "msg_id": "detail-1",
                "original_msg_id": "detail-1",
                "account": "seller",
                "buyer_id": "buyer.1-seller.1#11001@cntaobao",
                "buyer_nick": "buyer",
                "role": "user",
                "content": "full detail",
                "ts": 100.0,
            })
            sessions = db.workbench_sessions()
            messages = db.workbench_messages("seller", "buyer.1-seller.1#11001@cntaobao")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["last_message"], "full detail")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["content"], "full detail")

    def test_builtin_gateway_acknowledges_only_a_durable_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            event = {
                "platform": "taobao",
                "msg_id": "gateway-local-1",
                "original_msg_id": "gateway-local-1",
                "account": "seller",
                "buyer_id": "buyer.1-seller.1#11001@cntaobao",
                "content": "visible detail",
            }
            db.upsert_event(event)
            self.assertEqual(len(db.claim_pending()), 1)
            app = SimpleNamespace(
                config=Config(Path(directory) / "config.json", {
                    "gateway_agent_id": "agent-test",
                    "gateway_agent_token": "gateway-token",
                }),
                db=db,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), WorkbenchHandler)
            server.app = app
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({"agent_id": "agent-test", "events": [event]}).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/local-seat/v1/events",
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Agent-Id": "agent-test",
                        "X-Agent-Token": "gateway-token",
                    },
                )
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["event_acks"], [{
                    "event_id": "qn-msg-v1|taobao|gateway-local-1",
                    "committed": True,
                }])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2.0)


class UpdaterGuardTests(unittest.TestCase):
    @patch("standalone_bridge.subprocess.run")
    def test_disables_only_configured_updater_task(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        app = SimpleNamespace(
            config=Config(Path("config.json"), {"updater_task_name": "AliUpdater"}),
            stop_event=threading.Event(),
        )
        guard = UpdaterGuard(app)
        self.assertTrue(guard.disable_once())
        command = run.call_args.args[0]
        self.assertEqual(command, ["schtasks.exe", "/Change", "/TN", "AliUpdater", "/Disable"])
        self.assertEqual(guard.disable_attempts, 1)
        self.assertGreater(guard.last_success_at, 0.0)


class AppBizSendSafetyTests(unittest.TestCase):
    def test_cdp_host_resolves_to_the_outermost_qianniu_process(self):
        expected = ROOT / "runtime" / "AliWorkbench.exe"
        foreign = Mock()
        foreign.name.return_value = "explorer.exe"
        root = Mock(pid=10)
        root.name.return_value = "AliWorkbench.exe"
        root.exe.return_value = str(expected)
        root.parent.return_value = foreign
        child = Mock(pid=20)
        child.name.return_value = "AliWorkbench.exe"
        child.exe.return_value = str(expected)
        child.parent.return_value = root
        host = Mock(pid=30)
        host.parent.return_value = child
        app = SimpleNamespace(
            config=Config(ROOT / "config.json", {"qianniu_exe": "runtime/AliWorkbench.exe"}),
            cdp=SimpleNamespace(host_pid=30),
        )
        adapter = AppBizSendAdapter(app)
        with patch("standalone_bridge.psutil.pid_exists", return_value=True), \
                patch("standalone_bridge.psutil.Process", return_value=host), \
                patch("standalone_bridge.psutil.process_iter") as process_iter:
            self.assertIs(adapter.target_process(), root)
        process_iter.assert_not_called()

    def test_frida_attach_failures_detach_the_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "appbiz_adapter.dll"
            plugin = root / "native_plugin.dll"
            helper.touch()
            plugin.touch()
            session = Mock()
            session.create_script.side_effect = RuntimeError("script failed")
            device = SimpleNamespace(attach=Mock(return_value=session))
            process = Mock(pid=7)
            process.create_time.return_value = 100.0

            appbiz = AppBizSendAdapter(SimpleNamespace(
                config=Config(root / "config.json", {"appbiz_adapter_path": str(helper)})
            ))
            native = NativeAdapter(SimpleNamespace(
                config=Config(root / "config.json", {"native_plugin_path": str(plugin)})
            ))
            with patch("standalone_bridge.frida.get_local_device", return_value=device):
                with self.assertRaisesRegex(RuntimeError, "script failed"):
                    appbiz.attach(process)
                with self.assertRaisesRegex(RuntimeError, "script failed"):
                    native.attach(process)
            self.assertEqual(session.detach.call_count, 2)

    def test_appbiz_receive_ingests_without_the_browser_page(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {}),
            ingest_event=Mock(return_value=("event-1", True)),
        )
        adapter = AppBizSendAdapter(app)
        event = {
            "platform": "taobao",
            "role": "user",
            "account": "seller",
            "buyer_id": "buyer#1@cntaobao",
            "content": "原生接收测试",
            "msg_id": "message.PNM",
            "original_msg_id": "message.PNM",
        }
        adapter.on_message({
            "type": "send",
            "payload": {"event": "appbiz_receive", "message": event},
        }, None)
        app.ingest_event.assert_called_once_with(event)
        self.assertEqual(adapter.received_messages, 1)
        self.assertGreater(adapter.last_receive_at, 0.0)

    def test_appbiz_outgoing_is_ingested_when_browser_page_is_absent(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {}),
            browser=SimpleNamespace(connected=0),
            db=SimpleNamespace(account_for_buyer=Mock(return_value="seller")),
            ingest_event=Mock(return_value=("event-1", True)),
        )
        adapter = AppBizSendAdapter(app)
        event = {
            "platform": "taobao",
            "role": "mall_cs",
            "buyer_id": "buyer#1@cntaobao",
            "content": "客服手动回复",
            "msg_id": "appbiz-out-1",
        }
        adapter.on_message({
            "type": "send",
            "payload": {"event": "appbiz_outgoing", "message": event},
        }, None)
        projected = app.ingest_event.call_args.args[0]
        self.assertEqual(projected["role"], "mall_cs")
        self.assertEqual(projected["account"], "seller")
        self.assertEqual(projected["content"], "客服手动回复")

    def test_appbiz_outgoing_is_skipped_when_browser_already_projects_it(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {}),
            browser=SimpleNamespace(connected=1),
            ingest_event=Mock(),
        )
        adapter = AppBizSendAdapter(app)
        adapter.on_message({
            "type": "send",
            "payload": {"event": "appbiz_outgoing", "message": {"content": "reply"}},
        }, None)
        app.ingest_event.assert_not_called()

    def test_appbiz_agent_contains_verified_message_layout(self):
        source = (ROOT / "appbiz_agent.js").read_text(encoding="utf-8")
        for marker in (
            "MESSAGE_SIZE = 584",
            "MESSAGE_CCODE_OFFSET = 8",
            "MESSAGE_ID_OFFSET = 48",
            "MESSAGE_BUYER_ID_OFFSET = 120",
            "MESSAGE_SELLER_ID_OFFSET = 192",
            "MESSAGE_CONTENT_OFFSET = 352",
            "event: 'appbiz_receive'",
            "event: 'appbiz_outgoing'",
            "source.indexOf('QianniuStandaloneBridge/') !== 0",
        ):
            self.assertIn(marker, source)

    def test_unvalidated_abi_rejects_before_native_call(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {"appbiz_send_abi_validated": False})
        )
        adapter = AppBizSendAdapter(app)
        adapter.script = SimpleNamespace()
        adapter.selected_service = "0x1"
        with self.assertRaisesRegex(RuntimeError, "tagged acceptance test"):
            adapter.send_text("buyer#1@cntaobao", "content", "test")

    def test_ready_reports_an_attached_cold_start_as_operational(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {"appbiz_send_abi_validated": True})
        )
        adapter = AppBizSendAdapter(app)
        adapter.script = SimpleNamespace()
        self.assertFalse(adapter.ready)
        adapter.candidate_count = 3
        self.assertTrue(adapter.ready)
        self.assertFalse(adapter.route_ready)
        adapter.selected_service = "0x1"
        adapter.selection_reason = "message_arrive"
        self.assertTrue(adapter.route_ready)

    def test_ready_does_not_trust_string_heuristic_candidate_count(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {"appbiz_send_abi_validated": True})
        )
        adapter = AppBizSendAdapter(app)
        adapter.script = SimpleNamespace()
        adapter.routable_candidate_count = 1
        self.assertFalse(adapter.ready)
        adapter.candidate_count = 3
        self.assertTrue(adapter.ready)
        self.assertFalse(adapter.route_ready)
        adapter.selected_service = "0x1"
        adapter.selection_reason = "singlemsg_getnewmsg"
        self.assertTrue(adapter.route_ready)

    def test_send_still_rejects_an_attached_adapter_without_a_route(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {"appbiz_send_abi_validated": True})
        )
        adapter = AppBizSendAdapter(app)
        adapter.script = SimpleNamespace()
        adapter.candidate_count = 3
        self.assertTrue(adapter.ready)
        with self.assertRaisesRegex(RuntimeError, "service is not selected"):
            adapter.send_text("buyer#1@cntaobao", "content", "test")

    def test_agent_never_restores_a_service_by_cross_process_ordinal(self):
        source = (ROOT / "appbiz_agent.js").read_text(encoding="utf-8")
        self.assertNotIn("SELECTED_CANDIDATE_INDEX", source)
        self.assertNotIn("EXPECTED_CANDIDATE_COUNT", source)
        self.assertIn("message_arrive", source)
        self.assertIn("serviceGetNewMsg", source)
        self.assertIn("GetNewMsg|", source)
        self.assertIn("singlemsg_getnewmsg", source)
        self.assertIn("serviceByCcode.get(ccode)", source)
        self.assertNotIn("message_context_account_match", source)
        self.assertNotIn("xsd_pick_timeout_once.mp3", source)

    def test_browser_bridge_keeps_active_fetch_behind_an_opt_in(self):
        browser_source = (ROOT / "browser_bridge.js").read_text(encoding="utf-8")
        bridge_source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        launcher_source = (ROOT / "launcher.py").read_text(encoding="utf-8")
        injector_source = (ROOT / "tools" / "inject_runtime_webui.py").read_text(encoding="utf-8")
        # The observe/mirror sources are allowed, but the cursor-advancing fetch
        # path must stay opt-in and must never run by default.
        self.assertIn("var HISTORY_POLL = OPTIONS.history_poll === true;", browser_source)
        self.assertIn("if (!HISTORY_POLL || !ccode) return;", browser_source)
        self.assertIn("if (!HISTORY_POLL || pollRunning) return;", browser_source)
        self.assertIn("enable history_poll to allow them", browser_source)
        self.assertIn("window.__qn_standalone_options", browser_source)
        for source in (launcher_source, injector_source):
            self.assertIn('"history_poll": bool(config.get("bridge_history_poll", False))', source)
            self.assertIn('"ws_mirror": bool(config.get("bridge_ws_mirror", True))', source)
        # Never unbind someone else's handler and never rebind via the window
        # object literal (the wrapper captures and chains the original).
        self.assertNotIn("window.imsdk.off(", browser_source)
        self.assertNotIn("window.imsdk.invoke =", browser_source)
        self.assertIn("sdk.invoke = wrapped;", browser_source)
        self.assertIn("var result = originalSend.apply(this, arguments);", browser_source)
        self.assertNotIn("prepare_send_context", bridge_source)
        self.assertIn("window.imsdk.on([eventName], handler)", browser_source)
        self.assertIn("im.singlemsg.onReceiveNewMsg", browser_source)
        self.assertIn("passive imsdk hook", browser_source)

    def test_agent_rescans_candidates_after_a_cold_start(self):
        source = (ROOT / "appbiz_agent.js").read_text(encoding="utf-8")
        self.assertIn("function scanCandidates()", source)
        self.assertIn("Date.now() - lastCandidateScanMs >= 3000", source)
        self.assertIn("scanCandidates();\n    candidateIndex", source)

    def test_messagesdk_callback_result_is_returned_to_the_bridge(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {
                "appbiz_send_abi_validated": True,
                "appbiz_callback_wait_seconds": 0.1,
            })
        )
        exports = SimpleNamespace(
            sendtext=Mock(return_value=0),
            pollsend=Mock(return_value={"state": 1, "result_code": 0}),
            cancelsend=Mock(return_value=0),
            status=Mock(return_value={
                "candidate_count": 3,
                "selected_service": "0x1",
                "selection_reason": "message_arrive",
                "selected_at_ms": 1000,
                "observed_calls": 0,
                "helper_loaded": True,
            }),
        )
        adapter = AppBizSendAdapter(app)
        adapter.pid = 7
        adapter.script = SimpleNamespace(exports_sync=exports)
        adapter.selected_service = "0x1"
        adapter.selection_reason = "message_arrive"
        receipt = adapter.send_text("buyer#1@cntaobao", "content", "test")
        self.assertTrue(receipt["callback_received"])
        self.assertEqual(receipt["result_code"], 0)
        exports.cancelsend.assert_not_called()

    def test_messagesdk_code_five_explains_missing_conversation_context(self):
        app = SimpleNamespace(
            config=Config(Path("config.json"), {
                "appbiz_send_abi_validated": True,
                "appbiz_callback_wait_seconds": 0.1,
            })
        )
        exports = SimpleNamespace(
            sendtext=Mock(return_value=0),
            pollsend=Mock(return_value={"state": 1, "result_code": 5}),
            cancelsend=Mock(return_value=0),
            status=Mock(return_value={
                "candidate_count": 3,
                "selected_service": "0x1",
                "selection_reason": "singlemsg_getnewmsg",
                "selected_at_ms": 1000,
                "observed_calls": 0,
                "helper_loaded": True,
            }),
        )
        adapter = AppBizSendAdapter(app)
        adapter.pid = 7
        adapter.script = SimpleNamespace(exports_sync=exports)
        adapter.selected_service = "0x1"
        adapter.selection_reason = "singlemsg_getnewmsg"
        with self.assertRaisesRegex(RuntimeError, "CheckConvExist_error"):
            adapter.send_text("buyer#1@cntaobao", "content", "test")


class QianniuPathScopingTests(unittest.TestCase):
    def test_host_processes_only_accept_bundled_runtime(self):
        app = SimpleNamespace(config=Config(Path("config.json"), {"qianniu_exe": "runtime/AliWorkbench.exe"}))
        injector = CdpInjector(app)
        ours_exe = str(ROOT / "runtime" / "9.97.59N" / "AliRender.exe")
        ours = SimpleNamespace(pid=1, info={"pid": 1, "name": "AliRender.exe", "exe": ours_exe, "cmdline": ["host"]})
        typed = SimpleNamespace(pid=2, info={"pid": 2, "name": "AliRender.exe", "exe": ours_exe, "cmdline": ["--type=gpu"]})
        other = SimpleNamespace(pid=3, info={"pid": 3, "name": "AliRender.exe", "exe": r"C:\Other\AliRender.exe", "cmdline": ["host"]})
        with patch("standalone_bridge.psutil.process_iter", return_value=[ours, typed, other]):
            self.assertEqual([p.pid for p in injector.host_processes()], [1])

    def test_maybe_launch_refuses_when_foreign_qianniu_is_running(self):
        app = SimpleNamespace(config=Config(Path("config.json"), {
            "auto_launch_qianniu": True,
            "qianniu_exe": "runtime/AliWorkbench.exe",
        }))
        injector = CdpInjector(app)
        with patch.object(injector, "host_processes", return_value=[]), \
                patch.object(injector, "foreign_qianniu_running", return_value="C:\\Official\\AliWorkbench.exe"), \
                patch("standalone_bridge.subprocess.Popen") as popen:
            injector.maybe_launch()
        popen.assert_not_called()
        self.assertIn("其他安装的千牛", injector.last_error)

    def test_dock_ignores_qianniu_installed_outside_the_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            foreign = Path(directory) / "other" / "AliWorkbench.exe"
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({"qianniu_exe": "runtime/AliWorkbench.exe"}), encoding="utf-8")
            dock = DockedWorkbench(config)
            bundled = SimpleNamespace(info={
                "pid": 123, "name": "AliWorkbench.exe",
                "exe": str(ROOT / "runtime" / "AliWorkbench.exe"),
            })
            stranger = SimpleNamespace(info={
                "pid": 456, "name": "AliWorkbench.exe", "exe": str(foreign),
            })
            with patch("docked_workbench.psutil.process_iter", return_value=[bundled, stranger]) as process_iter:
                self.assertEqual(dock.qianniu_pids(), {123})
                self.assertEqual(dock.qianniu_pids(), {123})
            process_iter.assert_called_once_with(["pid", "name", "exe"])


class SendIdempotencyTests(unittest.TestCase):
    def test_send_is_claimed_before_native_call_and_replayed_as_submitted(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            appbiz = SimpleNamespace(send_text=Mock(return_value=True))
            bridge = StandaloneBridge.__new__(StandaloneBridge)
            bridge.db = db
            bridge.appbiz = appbiz
            bridge.config = Config(
                Path(directory) / "config.json",
                {"send_enabled": True, "send_confirmation_timeout_seconds": 15.0},
            )
            body = {
                "request_id": "request-1",
                "buyer_cid": "buyer.1-seller.1#11001@cntaobao",
                "content": "hello",
            }

            first = bridge.send_text(body, brain_authorized=True)
            second = bridge.send_text(body, brain_authorized=True)

            self.assertTrue(first["ok"])
            self.assertEqual(first["status"], "submitted")
            self.assertFalse(first["confirmed"])
            self.assertEqual(first, second)
            appbiz.send_text.assert_called_once_with(
                body["buyer_cid"], body["content"], f"QianniuStandaloneBridge/{VERSION}"
            )
            self.assertEqual(db.get_send("request-1")["status"], "submitted")

    def test_stop_waits_for_adapters_to_release_their_sessions(self):
        bridge = StandaloneBridge.__new__(StandaloneBridge)
        bridge.stop_event = threading.Event()
        bridge.appbiz = SimpleNamespace(thread=Mock())
        bridge.native = SimpleNamespace(thread=Mock())
        bridge.stop()
        self.assertTrue(bridge.stop_event.is_set())
        bridge.appbiz.thread.join.assert_called_once_with(timeout=3.0)
        bridge.native.thread.join.assert_called_once_with(timeout=3.0)

    def test_shared_send_gate_blocks_every_caller_before_native_send(self):
        bridge = StandaloneBridge.__new__(StandaloneBridge)
        bridge.config = Config(Path("config.json"), {"send_enabled": False})
        bridge.appbiz = SimpleNamespace(send_text=Mock())
        with self.assertRaisesRegex(RuntimeError, "sending is disabled"):
            bridge.send_text({
                "request_id": "blocked-1",
                "buyer_cid": "buyer#1@cntaobao",
                "content": "hello",
            })
        bridge.config.raw["send_enabled"] = True
        with self.assertRaisesRegex(RuntimeError, "backend brain command"):
            bridge.send_text({
                "request_id": "blocked-2",
                "buyer_cid": "buyer#1@cntaobao",
                "content": "hello",
            })
        bridge.appbiz.send_text.assert_not_called()

    def test_in_flight_receipt_blocks_automatic_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            buyer_cid = "buyer#1@cntaobao"
            content = "hello"
            payload = json_text({"buyer_cid": buyer_cid, "content": content})
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            claimed, _ = db.claim_send("request-2", payload_hash, buyer_cid, content)
            self.assertTrue(claimed)
            claimed_again, existing = db.claim_send(
                "request-2", payload_hash, buyer_cid, content
            )
            self.assertFalse(claimed_again)
            self.assertEqual(existing["status"], "in_flight")
            self.assertIn("automatic retry is blocked", json.loads(existing["response"])["error"])

    def test_only_matching_seller_echo_confirms_a_submitted_send(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            buyer_cid = "buyer.1-seller.1#11001@cntaobao"
            content = "receipt-check"
            payload = json_text({"buyer_cid": buyer_cid, "content": content})
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            claimed, _ = db.claim_send("request-3", payload_hash, buyer_cid, content)
            self.assertTrue(claimed)
            db.mark_send_submitted("request-3", payload_hash)

            inbound = {
                "platform": "taobao",
                "msg_id": "inbound-1",
                "original_msg_id": "inbound-1",
                "account": "seller",
                "buyer_id": buyer_cid,
                "role": "user",
                "content": content,
                "ts": time.time(),
            }
            inbound_id, _ = db.upsert_event(inbound)
            self.assertEqual(db.confirm_send_from_event(inbound, inbound_id), "")
            self.assertEqual(db.get_send("request-3")["status"], "submitted")

            outgoing = dict(
                inbound,
                msg_id="outgoing-1",
                original_msg_id="outgoing-1",
                role="mall_cs",
            )
            outgoing_id, _ = db.upsert_event(outgoing)
            self.assertEqual(
                db.confirm_send_from_event(outgoing, outgoing_id), "request-3"
            )
            receipt = db.get_send("request-3")
            self.assertEqual(receipt["status"], "confirmed")
            self.assertEqual(receipt["confirmation_event_id"], outgoing_id)
            messages = db.workbench_messages("seller", buyer_cid)
            self.assertEqual(
                [row["event_id"] for row in messages].count(outgoing_id), 1
            )
            self.assertNotIn("request-3", [row["event_id"] for row in messages])

    def test_unconfirmed_send_becomes_unknown_and_is_not_reissued(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            buyer_cid = "buyer.1-seller.1#11001@cntaobao"
            content = "timeout-check"
            payload = json_text({"buyer_cid": buyer_cid, "content": content})
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            db.claim_send("request-4", payload_hash, buyer_cid, content)
            db.mark_send_submitted("request-4", payload_hash)
            created_at = float(db.get_send("request-4")["created_at"])
            self.assertEqual(
                db.expire_unconfirmed_sends(15.0, now=created_at + 16.0), 1
            )
            receipt = db.get_send("request-4")
            self.assertEqual(receipt["status"], "unknown")
            self.assertFalse(json.loads(receipt["response"])["ok"])
            claimed, existing = db.claim_send(
                "request-4", payload_hash, buyer_cid, content
            )
            self.assertFalse(claimed)
            self.assertEqual(existing["status"], "unknown")

    def test_messagesdk_success_callback_confirms_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            appbiz = SimpleNamespace(send_text=Mock(return_value={
                "native_submitted": True,
                "callback_received": True,
                "result_code": 0,
            }))
            bridge = StandaloneBridge.__new__(StandaloneBridge)
            bridge.db = db
            bridge.appbiz = appbiz
            bridge.config = Config(
                Path(directory) / "config.json",
                {"send_enabled": True, "send_confirmation_timeout_seconds": 15.0},
            )
            body = {
                "request_id": "request-callback",
                "buyer_cid": "buyer.1-seller.1#11001@cntaobao",
                "content": "callback-check",
            }
            response = bridge.send_text(body, brain_authorized=True)
            self.assertTrue(response["ok"])
            self.assertTrue(response["confirmed"])
            self.assertEqual(response["confirmation_source"], "messagesdk_callback")
            self.assertEqual(db.get_send("request-callback")["status"], "confirmed")
            messages = db.workbench_messages("seller", body["buyer_cid"])
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["status"], "confirmed")

    def test_legacy_completed_native_returns_migrate_to_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE sends (
                    request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,response TEXT NOT NULL,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO sends VALUES(?,?,?,?,?,?)",
                ("legacy-1", "hash", "completed", '{"ok":true}', 1.0, 1.0),
            )
            connection.commit()
            connection.close()
            db = StateDB(path)
            receipt = db.get_send("legacy-1")
            self.assertEqual(receipt["status"], "unknown")
            self.assertFalse(json.loads(receipt["response"])["ok"])


class SeparationTests(unittest.TestCase):
    def test_runtime_sources_have_no_original_product_dependency(self):
        forbidden = ("kefuAgent", "\\u63a2\\u57df", "tanyu", "47.107.", "127.0.0.1:41010")
        for name in (
            "standalone_bridge.py", "browser_bridge.js", "native_agent.js",
            "appbiz_agent.js",
        ):
            text = (ROOT / name).read_text(encoding="utf-8").lower()
            for marker in forbidden:
                self.assertNotIn(marker.lower(), text, f"{marker} leaked into {name}")

    @unittest.skipUnless(
        (ROOT / "runtime").is_dir() and (ROOT / "vendor").is_dir(),
        "packaged Qianniu runtime and vendor plugin are not part of the source repository",
    )
    def test_packaged_runtime_has_no_old_bridge_or_injection_modules(self):
        runtime = ROOT / "runtime"
        self.assertTrue((runtime / "AliWorkbench.exe").is_file())
        launcher_config = (runtime / "AliWorkbench.ini").read_text(encoding="utf-8").lower()
        # The packaged runtime can ship more than one supported client build, so
        # assert against the builds the Frida agent actually has profiles for.
        # The active build must be one the Frida agent actually has a profile
        # for. Read the table instead of hardcoding versions, so a client
        # upgrade only needs the profile added.
        agent_source = (ROOT / "appbiz_agent.js").read_text(encoding="utf-8")
        supported = {match.lower() for match in re.findall(r"version:\s*'([^']+)'", agent_source)}
        active = ""
        if "version=" in launcher_config:
            active = launcher_config.split("version=", 1)[1].split()[0].strip()
        self.assertTrue(supported, "appbiz_agent.js exposes no hook profiles")
        self.assertIn(active, supported, f"active build {active} has no AppBiz profile")
        forbidden_names = ("tyagent", "smartrobot", "injector", "insideplugin", "tanyu", "\u63a2\u57df")
        for path in runtime.rglob("*"):
            if not path.is_file():
                continue
            lowered = path.name.lower()
            for marker in forbidden_names:
                self.assertNotIn(marker, lowered, f"unexpected injected runtime file: {path}")

        old_bridge_markers = (
            "127.0.0.1:41010",
            "127.0.0.1:41011",
            "data-qn-bridge",
            "__qn_bridge_v",
            "qn-bridge-inline-v",
        )
        webui_archives = sorted(runtime.rglob("webui.zip"))
        self.assertGreaterEqual(len(webui_archives), 2)
        for archive in webui_archives:
            with zipfile.ZipFile(archive) as package:
                html = package.read("web_chat-packer/recent.html").decode("utf-8").lower()
            for marker in old_bridge_markers:
                self.assertNotIn(marker, html, f"old bridge marker survived in {archive}")
            self.assertEqual(html.count("data-qn-standalone-bridge"), 1)
            self.assertEqual(html.count("__qn_standalone_bridge_v1_installed"), 2)
            self.assertIn("qn-standalone-browser-v6-stable-identity", html)
            for unsafe in (
                "window.imsdk.invoke =",
                "window.imsdk.off(",
            ):
                self.assertNotIn(unsafe, html, f"unsafe bridge code survived in {archive}")
            # The cursor-advancing fetch call sites now exist but must be shipped
            # disabled: the injected options block is the gate.
            self.assertIn('"history_poll": false', html, f"history polling must default to off in {archive}")
            self.assertNotIn("im.singlemsg.getremotehismsg", html, f"remote history fetch survived in {archive}")

        version_config = (runtime / "version.ini").read_text(encoding="utf-8").lower()
        for marker in ("kefuagent", "tanyu", "insideplugin", "qnmsgplugin"):
            self.assertNotIn(marker, version_config, f"legacy dependency survived in version.ini: {marker}")

        plugin = ROOT / "vendor" / "9.77.01_qnmsgplugin_x64.dll"
        digest = hashlib.sha256(plugin.read_bytes()).hexdigest().upper()
        self.assertEqual(digest, "E73206A73F1D44969E8C9B9DDD91193369CA4ADEADAEB3E1525B5B9A741080AE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
