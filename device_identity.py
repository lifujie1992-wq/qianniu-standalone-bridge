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


_DIRECT_OPENER: urllib.request.OpenerDirector | None = None


def direct_opener() -> urllib.request.OpenerDirector:
    """公共入口：绕过系统/环境代理的直连 opener（带缓存）。

    大脑是固定公网 IP、本机探测是 127.0.0.1，都不需要代理；机器上装了
    代理/加速器时，系统代理会把请求交给一个不存在的出口，报出
    「[Errno 2] No such file or directory」「无法连接大脑」这类误导性
    错误（2026-11-03 现场）。所有出站 HTTP 一律走这里。
    """
    return _direct_opener()


def _direct_opener() -> urllib.request.OpenerDirector:
    """绕过系统/环境代理直连大脑。

    大脑是固定公网 IP，不需要代理；机器上装了代理/加速器时，系统代理
    会把请求交给一个不存在的出口，报出「[Errno 2] No such file or
    directory」这类误导性错误（2026-11-03 现场）。注入 opener 的调用方
    （测试）不受影响。
    """
    global _DIRECT_OPENER
    if _DIRECT_OPENER is None:
        _DIRECT_OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
    return _DIRECT_OPENER


def http_header_value(value: Any, field: str) -> str:
    """Return a value safe for Python's HTTP/1 header encoder."""
    text = str(value or "").strip()
    try:
        text.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} 只能包含 ASCII 字符，请重新填写") from error
    return text


@dataclass(frozen=True)
class DeviceIdentityResult:
    device_id: str
    changed: bool
    migrated: bool
    migration_error: str = ""


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
            "X-Agent-Token": http_header_value(token, "大脑工位令牌"),
            "X-Device-Id": http_header_value(old_device_id, "设备标识"),
            "Content-Type": "application/json",
        },
    )
    open_request = opener or _direct_opener().open
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
    allow_fallback: bool = False,
) -> DeviceIdentityResult:
    old_device_id = str(config.get("device_id") or "").strip()
    new_device_id = stable_device_id(machine_guid)
    if old_device_id == new_device_id:
        return DeviceIdentityResult(new_device_id, changed=False, migrated=False)

    token = brain_workstation_token(config)
    migrated = False
    migration_error = ""
    if token:
        try:
            _request_migration(config, old_device_id, new_device_id, opener)
            migrated = True
        except DeviceIdentityError as error:
            # 换机/拷了别人的 config 时，服务端会用 403 拒绝迁移（令牌绑在
            # 原机器上）。本地桥接本身不依赖这个 id，没必要因此拒绝启动：
            # allow_fallback 时记下原因、照常落新 id，让大脑侧如实报
            # "令牌已绑另一台电脑"，而不是让整个助手起不来。
            if not allow_fallback:
                raise
            migration_error = str(error)

    updated = dict(config)
    updated["device_id"] = new_device_id
    try:
        atomic_write_json(Path(config_path), updated)
    except OSError as error:
        raise DeviceIdentityError(f"稳定设备标识保存失败：{error}") from error
    config["device_id"] = new_device_id
    return DeviceIdentityResult(
        new_device_id, changed=True, migrated=migrated, migration_error=migration_error
    )
