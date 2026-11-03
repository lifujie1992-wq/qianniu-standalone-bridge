"""设备身份迁移失败时不要让助手起不来（2026-11-04 现场）。

同事把别人机器上的 config.json 拷过来时，`device_id` 是原机器的，
启动会先请求 `/api/bridge/v1/device/migrate`；令牌绑在原机器上，
大脑回 403，`ensure_device_identity` 直接 return False —— 助手完全不启动，
现场只能看到一句「设备身份迁移失败 403」，被当成"这个版本坏了"。

正确行为：本地桥接不依赖这个 id，迁移失败也照常启动，把问题交给大脑侧
如实报「令牌已绑另一台电脑」，同时提示重新签发令牌。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import device_identity
import launcher


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class DeviceMigrationFallbackTests(unittest.TestCase):
    MACHINE_GUID = "test-machine-guid"

    def _config(self) -> dict:
        return {
            "device_id": "device-from-another-pc",
            "brain_server_url": "http://brain.example",
            "brain_agent_token": "copied-seat-token",
        }

    def test_fallback_keeps_starting_and_records_the_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = self._config()
            rejected = mock.Mock(return_value=FakeResponse(200, {"ok": False, "error": "invalid agent token"}))
            result = device_identity.prepare_device_identity(
                config, path, machine_guid=self.MACHINE_GUID, opener=rejected, allow_fallback=True
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(result.changed)
        self.assertFalse(result.migrated)
        self.assertIn("invalid agent token", result.migration_error)
        self.assertEqual(saved["device_id"], result.device_id)
        self.assertEqual(config["device_id"], result.device_id)

    def test_strict_mode_still_raises_for_other_callers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            rejected = mock.Mock(return_value=FakeResponse(200, {"ok": False, "error": "nope"}))
            with self.assertRaises(device_identity.DeviceIdentityError):
                device_identity.prepare_device_identity(
                    self._config(), path, machine_guid=self.MACHINE_GUID, opener=rejected
                )

    def test_launcher_continues_startup_and_warns(self):
        def fake_prepare(config, path, **_kwargs):
            config["device_id"] = "device-new"
            return device_identity.DeviceIdentityResult(
                "device-new", changed=True, migrated=False,
                migration_error="设备身份迁移失败：HTTP 403: invalid agent token",
            )

        with mock.patch.object(launcher, "prepare_device_identity", fake_prepare), \
                mock.patch.object(launcher, "message_box") as box:
            started = launcher.ensure_device_identity(self._config(), Path("config.json"))

        self.assertTrue(started)
        box.assert_called_once()
        title, text = box.call_args.args[0], box.call_args.args[1]
        self.assertIn("设备身份未迁移", title)
        self.assertIn("重新签发", text)


if __name__ == "__main__":
    unittest.main()
