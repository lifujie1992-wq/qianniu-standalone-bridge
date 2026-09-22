from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app_version import VERSION
from device_identity import brain_workstation_token


def version_key(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.strip().split("."))
    except ValueError:
        return ()


def update_manifest_url(config: dict[str, Any]) -> str:
    explicit = str(config.get("update_manifest_url") or "").strip()
    if explicit:
        return explicit
    server = str(config.get("brain_server_url") or "").strip().rstrip("/")
    return server + "/api/bridge/v1/client-update" if server else ""


def _safe_download_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("更新地址必须是无账号信息的 HTTPS 地址")
    return value


def fetch_update_manifest(config: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    url = update_manifest_url(config)
    if not url:
        raise ValueError("尚未配置在线更新地址")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("更新清单地址无效")
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Agent-Token": brain_workstation_token(config),
        "X-Agent-Id": str(config.get("brain_agent_id") or ""),
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise RuntimeError(f"检查更新失败：{error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("更新清单格式错误")
    release = payload.get("release") if isinstance(payload.get("release"), dict) else payload
    version = str(release.get("version") or "").strip()
    checksum = str(release.get("sha256") or "").strip().lower()
    if not version_key(version) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
        raise RuntimeError("更新清单缺少有效版本号或 SHA-256")
    return {
        "version": version,
        "installer_url": _safe_download_url(str(release.get("installer_url") or "").strip()),
        "sha256": checksum,
        "size": int(release.get("size") or 0),
        "notes": str(release.get("notes") or ""),
        "mandatory": bool(release.get("mandatory")),
    }


def update_available(release: dict[str, Any]) -> bool:
    return version_key(str(release.get("version") or "")) > version_key(VERSION)


def download_installer(release: dict[str, Any], timeout: float = 30.0) -> Path:
    version = str(release["version"])
    target = Path(tempfile.gettempdir()) / f"QianniuAIService-{version}-Setup.exe"
    temporary = target.with_suffix(".download")
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(str(release["installer_url"]), headers={"Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
                output.write(chunk)
    except (OSError, urllib.error.URLError) as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"下载安装包失败：{error}") from error
    expected_size = int(release.get("size") or 0)
    if (expected_size and total != expected_size) or digest.hexdigest() != release["sha256"]:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("安装包校验失败，已拒绝安装")
    temporary.replace(target)
    return target


def launch_installer_after_exit(installer: Path, parent_pid: int | None = None) -> None:
    pid = int(parent_pid or os.getpid())
    command = (
        f'while (Get-Process -Id {pid} -ErrorAction SilentlyContinue) '
        '{ Start-Sleep -Milliseconds 300 }; '
        f'Start-Process -FilePath {json.dumps(str(installer))} '
        "-ArgumentList '/SILENT','/CLOSEAPPLICATIONS','/RESTARTAPPLICATIONS'"
    )
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", command],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
