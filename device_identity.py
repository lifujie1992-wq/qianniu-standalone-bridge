"""Stable Windows device identity and brain workstation token handling."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


BRAIN_WORKSTATION_TOKEN_KEY = "brain_agent_token"
MACHINE_GUID_KEY = r"SOFTWARE\Microsoft\Cryptography"
MACHINE_GUID_VALUE = "MachineGuid"
MIGRATION_PATH = "/api/bridge/v1/device/migrate"


class DeviceIdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceIdentityResult:
    device_id: str
    changed: bool
    migrated: bool


def brain_workstation_token(config: Any) -> str:
    if hasattr(config, "get"):
        return str(config.get(BRAIN_WORKSTATION_TOKEN_KEY) or "").strip()
    return ""


def set_brain_workstation_token(config: dict[str, Any], token: str) -> None:
    config[BRAIN_WORKSTATION_TOKEN_KEY] = str(token or "").strip()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_machine_guid() -> str:
    if os.name != "nt":
        raise DeviceIdentityError("MachineGuid 仅适用于 Windows")
    try:
        import winreg

        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            MACHINE_GUID_KEY,
            0,
            access,
        ) as key:
            value, _value_type = winreg.QueryValueEx(key, MACHINE_GUID_VALUE)
    except OSError as error:
        raise DeviceIdentityError(f"无法读取 Windows MachineGuid: {error}") from error
    guid = str(value or "").strip()
    if not guid:
        raise DeviceIdentityError("Windows MachineGuid 为空")
    return guid


def stable_device_id(machine_guid: str | None = None) -> str:
    guid = str(machine_guid if machine_guid is not None else read_machine_guid()).strip().lower()
    if not guid:
        raise DeviceIdentityError("Windows MachineGuid 为空")
    source = "windows-machine-guid\x00" + guid
    digest = hashlib.sha256(
        ("pdd-bridge-device-v1\x00" + source).encode("utf-8")
    ).hexdigest()
    return "device-" + digest


def _migration_url(config: dict[str, Any]) -> str:
    base = str(config.get("brain_server_url") or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DeviceIdentityError("已有大脑工位令牌，但大脑服务地址无效，无法迁移设备身份")
    return base + MIGRATION_PATH


def _request_migration(
    config: dict[str, Any],
    old_device_id: str,
    new_device_id: str,
    opener: Callable[..., Any] | None,
) -> None:
    token = brain_workstation_token(config)
    body = json.dumps(
        {"new_device_id": new_device_id},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        _migration_url(config),
        data=body,
        method="POST",
        headers={
            "X-Agent-Token": token,
            "X-Device-Id": old_device_id,
            "Content-Type": "application/json",
        },
    )
    open_request = opener or urllib.request.urlopen
    try:
        timeout = float(config.get("brain_request_timeout_seconds", 8.0))
    except (TypeError, ValueError) as error:
        raise DeviceIdentityError("大脑请求超时时间配置无效，无法迁移设备身份") from error
    try:
        with open_request(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise DeviceIdentityError(
            f"设备身份迁移失败：HTTP {error.code}: {detail or error.reason}"
        ) from error
    except (OSError, urllib.error.URLError) as error:
        raise DeviceIdentityError(f"设备身份迁移请求失败：{error}") from error
    except Exception as error:
        raise DeviceIdentityError(f"设备身份迁移请求失败：{error}") from error
    if status != 200:
        raise DeviceIdentityError(f"设备身份迁移失败：HTTP {status}")
    try:
        payload = json.loads(raw or "{}")
    except ValueError as error:
        raise DeviceIdentityError("设备身份迁移失败：服务端返回了无效 JSON") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        detail = payload.get("error") if isinstance(payload, dict) else ""
        raise DeviceIdentityError(f"设备身份迁移被服务端拒绝：{detail or 'ok 不为 true'}")


def prepare_device_identity(
    config: dict[str, Any],
    config_path: Path,
    *,
    machine_guid: str | None = None,
    opener: Callable[..., Any] | None = None,
) -> DeviceIdentityResult:
    old_device_id = str(config.get("device_id") or "").strip()
    new_device_id = stable_device_id(machine_guid)
    if old_device_id == new_device_id:
        return DeviceIdentityResult(new_device_id, changed=False, migrated=False)

    token = brain_workstation_token(config)
    migrated = False
    if token:
        _request_migration(config, old_device_id, new_device_id, opener)
        migrated = True

    updated = dict(config)
    updated["device_id"] = new_device_id
    try:
        atomic_write_json(Path(config_path), updated)
    except OSError as error:
        raise DeviceIdentityError(f"稳定设备标识保存失败：{error}") from error
    config["device_id"] = new_device_id
    return DeviceIdentityResult(new_device_id, changed=True, migrated=migrated)
