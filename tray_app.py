"""Persistent tray owner for the packaged Qianniu assistant."""

from __future__ import annotations

import ctypes
import hashlib
import os
import socket
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


# A single failed probe is not a dead bridge. /api/v1/status is served by the
# same process that ingests 7 injected pages into SQLite, so on a busy seat one
# 5s probe can miss while messages keep flowing. Requiring two consecutive
# failures (and naming the reason) stops the panel from flashing "已停止 / 未监听"
# for a bridge that is only slow - the exact symptom reported on 2026-10-17.
_BRIDGE_STOP_AFTER_FAILURES = 2
_BRIDGE_PROBE_STATE: dict[str, dict] = {}


def _managed_bridge_alive(config: dict, root: Path) -> bool:
    """Keep the tray status healthy across a slow status response."""
    state_dir = root / "state"
    if not launcher.live_managed_pid(state_dir / "standalone_bridge.pid"):
        return False
    for port in (int(config.get("api_port", 42111)), int(config.get("workbench_port", 18776))):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                continue
        except OSError:
            return False
    return True


def _bridge_failure_detail(reason: str, port: int) -> str:
    if reason == "timeout":
        return f"响应超时 {port}（进程在忙）"
    if reason.startswith("http_"):
        return f"本地接口 HTTP {reason[5:]}"
    if reason == "bad_response":
        return "本地接口返回异常"
    if reason == "no_config":
        return "未配置"
    return f"未监听 {port}"


def _probe_bridge(config: dict) -> tuple[bool, str, dict | None]:
    """Return (healthy, detail, status payload) for the local bridge."""
    if not config:
        return False, "未配置", None
    port = int(config.get("api_port", 42111))
    status, reason = launcher.fetch_status_with_reason(config, timeout=20.0)
    key = str(port)
    state = _BRIDGE_PROBE_STATE.setdefault(key, {"failures": 0, "ever_ok": False})
    if isinstance(status, dict) and status.get("ok"):
        state["failures"] = 0
        state["ever_ok"] = True
        return True, f"127.0.0.1:{port}", status
    if isinstance(status, dict):
        # The API answered. A component fault is not a stopped process or an
        # unbound port, and must not be hidden by the timeout grace period.
        state["failures"] = 0
        error = "本地状态异常"
        for name in ("api", "workbench", "browser"):
            component = status.get(name)
            if isinstance(component, dict) and component.get("error"):
                error = str(component["error"])[:200]
                break
        return False, "服务运行中：" + error, status
    if status is not None:
        status, reason = None, "bad_response"
    state["failures"] += 1
    detail = _bridge_failure_detail(reason, port)
    if state["ever_ok"] and state["failures"] < _BRIDGE_STOP_AFTER_FAILURES:
        # Grace period: keep the panel honest but not alarming for one tick.
        return True, f"{detail}（1 次探测失败）", status
    return False, detail, status


def _accessibility_component(settings: object, bridge: dict | None) -> tuple[ComponentStatus, str]:
    """千牛「无障碍模式」行 + 一句给现场的结论。

    2026-11-03 现场：面板全绿、消息能收，但订单/商品取不到 —— 根因就是这个
    开关关着（千牛不会把 `workbench` 等原生 JSAPI 注入 webview）。它必须直接
    显示在状态面板上，不能让现场看到一个绿色的“运行正常”就走。
    """
    if not isinstance(settings, dict):
        return (
            ComponentStatus(state="stopped", label="未知", detail="助手版本不支持检测"),
            "",
        )
    connections = ((bridge or {}).get("browser") or {}).get("connections") or []
    hooked = any(
        isinstance(page, dict) and page.get("imsdk_hooked") for page in connections
    )
    enabled = settings.get("enabled")
    if enabled is False:
        return (
            ComponentStatus(
                state="degraded",
                label="已关闭",
                detail="取不到订单 · 设置→通用→无障碍模式",
            ),
            "千牛「无障碍模式」未开启：订单和商品一定取不到。"
            "请在 千牛 → 设置 → 通用 → 无障碍模式 打开，"
            "然后彻底退出千牛（托盘也要退）再重开。",
        )
    if enabled is True:
        if hooked:
            return ComponentStatus(
                state="healthy", label="已开启", detail="页面 JSAPI 已就绪"
            ), ""
        return (
            ComponentStatus(
                state="degraded",
                label="已开启",
                detail="页面无 JSAPI · 彻底退出千牛后重开",
            ),
            "无障碍模式已开启，但千牛页面里还没有原生 JSAPI："
            "请彻底退出千牛（托盘也要退）后重新打开。",
        )
    error = str(settings.get("error") or "").strip()
    return (
        ComponentStatus(
            state="stopped", label="读取失败", detail=(error or "读不到千牛配置")[:60]
        ),
        "读不到千牛「无障碍模式」设置，若取不到订单请先跑现场诊断脚本。",
    )


def build_status(root: Path) -> AppStatus:
    config = launcher.load_config_silent(root / "config.json") or {}
    # /api/v1/status can take a couple of seconds while the bridge runs its
    # first process/GPU scan; the old sub-second probe timed out and reported a
    # perfectly healthy bridge as stopped, leaving the tray stuck on
    # "部分组件未就绪".
    bridge_ok, bridge_detail, bridge = _probe_bridge(config)
    if not bridge_ok and bridge is None and _managed_bridge_alive(config, root):
        bridge_ok = True
        bridge_detail = f"127.0.0.1:{int(config.get('api_port', 42111))}"
    bridge_reachable = isinstance(bridge, dict)
    qianniu_count = _managed_qianniu_count(root)
    dock_running = _pid_file_running(root / "state" / "docked_workbench.pid")

    components = {
        "bridge": ComponentStatus(
            state="healthy" if bridge_ok else ("degraded" if bridge_reachable else "stopped"),
            detail=bridge_detail,
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
    access, access_note = _accessibility_component(
        (bridge or {}).get("client_settings"), bridge
    )
    components["access"] = access
    required_healthy = bridge_ok and qianniu_count > 0
    any_running = bridge_ok or bridge_reachable or qianniu_count > 0 or dock_running
    if required_healthy:
        state = "healthy"
        # 服务本身正常，但无障碍关着就是取不到订单 —— 用红字说清楚，
        # 别让现场把“运行正常”当成“能取单”。
        error = access_note
    elif any_running:
        state = "degraded"
        error = access_note or "部分组件未就绪，请查看组件详情。"
    else:
        state = "stopped"
        error = access_note
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
        owned_pids = {
            process.pid for process in docked_workbench.DockedWorkbench.profile_edge_processes(
                root / "state" / "dock-edge-profile"
            )
        }
        candidates = []
        for window in docked_workbench.Win32.windows():
            if window.pid not in owned_pids or docked_workbench.DOCK_TITLE not in window.title:
                continue
            # Chromium parks a second off-screen helper window with the same title.
            if window.rect.left <= -1000 or window.rect.width < 80:
                continue
            candidates.append((window.hwnd, window.rect.width * window.rect.height))
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
                    user32 = docked_workbench.Win32.user32
                    user32.ShowWindow(hwnd, SW_RESTORE)
                    user32.SetForegroundWindow(hwnd)
                    return
                # A snap-mode dock can remain alive while waiting for a
                # Qianniu window. An explicit wake click should replace it
                # with the floating panel instead of silently doing nothing.
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

    def completely_exit(keep_qianniu: bool = False) -> None:
        launcher.stop_all(include_tray=False, include_qianniu=not keep_qianniu)

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
