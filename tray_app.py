"""Persistent tray owner for the packaged Qianniu assistant."""

from __future__ import annotations

import ctypes
import hashlib
import os
import threading
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

import psutil

import config_dialog
import docked_workbench
import launcher
from app_version import VERSION
from tray_control import AppStatus, ComponentStatus, TrayCallbacks, TrayControlCenter


ERROR_ALREADY_EXISTS = 183
# 与 docked_workbench.DOCK_TITLE 一致的浮窗标题；配合 Edge 命令行里的
# dock-edge-profile 标记，避免误把福客/用户自己的 Edge 窗口当成浮窗。
DOCK_PROFILE_MARKER = "dock-edge-profile"
SW_RESTORE = 9


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
    # /api/v1/status can take a couple of seconds while the bridge runs its
    # first process/GPU scan; the old sub-second probe timed out and reported a
    # perfectly healthy bridge as stopped, leaving the tray stuck on
    # "部分组件未就绪".
    bridge = launcher.fetch_status(config, timeout=5.0) if config else None
    qianniu_count = _managed_qianniu_count(root)
    dock_running = _pid_file_running(root / "state" / "docked_workbench.pid")
    bridge_ok = bool(bridge and bridge.get("ok"))

    components = {
        "bridge": ComponentStatus(
            state="healthy" if bridge_ok else "stopped",
            detail=(f"127.0.0.1:{int(config.get('api_port', 42111))}" if bridge_ok else "未监听"),
        ),
        "qianniu": ComponentStatus(
            state="healthy" if qianniu_count else "stopped",
            detail=f"{qianniu_count} 个进程" if qianniu_count else "未运行",
        ),
        # The floating window is optional: it shows as a component, but a
        # missing dock never drags the whole app into "degraded".
        "dock": ComponentStatus(
            state="healthy" if dock_running else "stopped",
            detail="浮窗已运行" if dock_running else "未运行",
        ),
    }
    required_healthy = bridge_ok and qianniu_count > 0
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

    def find_dock_hwnd() -> int | None:
        """Return the dock Edge app window handle, or None when it is gone."""
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        rows: list[tuple[int, int, int, int]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def visit(hwnd: int, _lparam: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(hwnd, buffer, len(buffer))
            if docked_workbench.DOCK_TITLE not in buffer.value:
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rows.append((int(hwnd), int(pid.value), int(rect.right) - int(rect.left), int(rect.bottom) - int(rect.top), int(rect.left)))
            return True

        user32.EnumWindows(visit, 0)
        candidates = []
        for hwnd, pid, width, height, left in rows:
            # Chromium parks a second off-screen helper window with the same title.
            if left <= -1000 or width < 80:
                continue
            try:
                process = psutil.Process(pid)
                if process.name().lower() != "msedge.exe":
                    continue
                if DOCK_PROFILE_MARKER not in " ".join(process.cmdline()):
                    continue
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            candidates.append((hwnd, width * height))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[1])[0]

    def show_dock() -> None:
        def worker() -> None:
            pid_file = root / "state" / "docked_workbench.pid"
            pid_text = ""
            try:
                pid_text = pid_file.read_text(encoding="ascii").strip()
            except OSError:
                pass
            pid = 0
            try:
                pid = int(pid_text)
            except ValueError:
                pass
            if pid > 0 and psutil.pid_exists(pid):
                hwnd = find_dock_hwnd()
                if hwnd:
                    user32 = ctypes.windll.user32
                    user32.ShowWindow(hwnd, SW_RESTORE)
                    user32.SetForegroundWindow(hwnd)
                    return
                # The dock process is alive but its window is gone: retire the
                # stale process so start_dock below can recreate it.
                try:
                    psutil.Process(pid).terminate()
                except psutil.NoSuchProcess:
                    pass
                deadline = time.time() + 3.0
                while time.time() < deadline and psutil.pid_exists(pid):
                    time.sleep(0.1)
            launcher.start_dock()

        run_async(worker)

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
            show_dock=show_dock,
        )
        control = TrayControlCenter(callbacks, lambda: build_status(root), state_dir=root / "state")
        # Start the bridge whenever it is not up, even when the Qianniu client is
        # already running: the old `state == "stopped"` check skipped the start
        # as soon as any component was alive, so a stopped bridge stayed down.
        bridge_state = build_status(root).components.get("bridge")
        if bridge_state is None or bridge_state.state != "healthy":
            start_all()
        control.run(show_initially=True)
        return 0
    finally:
        if control is not None:
            control.close()
        release_single_instance(handle)
