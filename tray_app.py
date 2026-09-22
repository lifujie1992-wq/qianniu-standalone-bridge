"""Persistent tray owner for the packaged Qianniu assistant."""

from __future__ import annotations

import ctypes
import hashlib
import os
import threading
import time
import webbrowser
from pathlib import Path

import psutil

import config_dialog
import launcher
from app_version import VERSION
from tray_control import AppStatus, ComponentStatus, TrayCallbacks, TrayControlCenter


ERROR_ALREADY_EXISTS = 183


def _mutex_name(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return f"Local\\QianniuAIServiceTray_{digest}"


def acquire_single_instance(root: Path) -> int | None:
    if os.name != "nt":
        return 1
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    handle = int(kernel32.CreateMutexW(None, False, _mutex_name(root)))
    if not handle or int(kernel32.GetLastError()) == ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(handle)
        return None
    return handle


def release_single_instance(handle: int | None) -> None:
    if os.name == "nt" and handle:
        ctypes.windll.kernel32.CloseHandle(handle)


def request_existing_window(root: Path) -> None:
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tray.show").write_text("show\n", encoding="ascii")


def _pid_file_running(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="ascii").strip())
        return pid > 0 and psutil.pid_exists(pid)
    except (OSError, ValueError):
        return False


_QIANNIU_COUNT_TTL_SECONDS = 10.0
_QIANNIU_COUNT_CACHE: dict[str, tuple[float, int]] = {}


def _managed_qianniu_count(root: Path) -> int:
    # Enumerating every process is the most expensive part of the tray status
    # refresh, and the count moves slowly, so reuse it for a few seconds.
    runtime = root / "runtime"
    key = str(runtime).lower()
    now = time.monotonic()
    cached = _QIANNIU_COUNT_CACHE.get(key)
    if cached is not None and now - cached[0] < _QIANNIU_COUNT_TTL_SECONDS:
        return cached[1]
    count = 0
    for process in psutil.process_iter(["exe"]):
        try:
            if launcher._path_is_within(process.info.get("exe") or "", runtime):
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _QIANNIU_COUNT_CACHE[key] = (now, count)
    return count


def build_status(root: Path) -> AppStatus:
    config = launcher.load_config_silent(root / "config.json") or {}
    bridge = launcher.fetch_status(config, timeout=0.7) if config else None
    qianniu_count = _managed_qianniu_count(root)
    dock_running = _pid_file_running(root / "state" / "docked_workbench.pid")
    bridge_ok = bool(bridge and bridge.get("ok"))
    dock_required = bool(config.get("dock_enabled", False))

    components = {
        "bridge": ComponentStatus(
            state="healthy" if bridge_ok else "stopped",
            detail=(f"127.0.0.1:{int(config.get('api_port', 42111))}" if bridge_ok else "未监听"),
        ),
        "qianniu": ComponentStatus(
            state="healthy" if qianniu_count else "stopped",
            detail=f"{qianniu_count} 个进程" if qianniu_count else "未运行",
        ),
        "dock": ComponentStatus(
            state="healthy" if dock_running else "stopped",
            detail="右侧浮层已运行" if dock_running else "未运行",
        ),
    }
    required_healthy = bridge_ok and qianniu_count > 0 and (dock_running or not dock_required)
    any_running = bridge_ok or qianniu_count > 0 or dock_running
    if required_healthy:
        state = "healthy"
        error = ""
    elif any_running:
        state = "degraded"
        error = "部分组件未就绪，可右键托盘图标重新启动全部。"
    else:
        state = "stopped"
        error = ""
    return AppStatus(
        state=state,
        version=str((bridge or {}).get("version") or VERSION),
        components=components,
        error=error,
    )


def run() -> int:
    root = launcher.app_root()
    handle = acquire_single_instance(root)
    if handle is None:
        request_existing_window(root)
        return 0

    control: TrayControlCenter | None = None
    action_lock = threading.Lock()

    def run_async(action) -> None:
        def worker() -> None:
            if not action_lock.acquire(blocking=False):
                return
            try:
                action()
            finally:
                action_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def open_workbench() -> None:
        config = launcher.load_config_silent(root / "config.json") or {}
        webbrowser.open(launcher.workbench_url(config))

    def start_all() -> None:
        run_async(launcher.start_all)

    def restart_all() -> None:
        def restart() -> None:
            launcher.stop_all(include_tray=False, include_qianniu=True)
            launcher.start_all()

        run_async(restart)

    def open_settings() -> None:
        launcher.configure()

    def completely_exit() -> None:
        launcher.stop_all(include_tray=False, include_qianniu=True)

    try:
        config = launcher.load_config_silent(root / "config.json")
        if config is None or config_dialog.needs_setup(config):
            launcher.configure()
        callbacks = TrayCallbacks(
            open_workbench=open_workbench,
            start_all=start_all,
            restart_all=restart_all,
            open_settings=open_settings,
            completely_exit=completely_exit,
        )
        control = TrayControlCenter(callbacks, lambda: build_status(root), state_dir=root / "state")
        if build_status(root).state == "stopped":
            start_all()
        control.run(show_initially=True)
        return 0
    finally:
        if control is not None:
            control.close()
        release_single_instance(handle)
