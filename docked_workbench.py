from __future__ import annotations

import argparse
import ctypes
import json
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil

from config_defaults import DEFAULT_WORKBENCH_PORT


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_root()
DOCK_TITLE = "千牛聚合接待"


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    rect: Rect
    visible: bool
    minimized: bool


def choose_dock_rect(target: Rect, work_area: Rect, width: int) -> Rect:
    height = min(target.height, work_area.height)
    top = max(work_area.top, min(target.top, work_area.bottom - height))
    if work_area.right - target.right >= width:
        left = target.right
    elif target.left - work_area.left >= width:
        left = target.left - width
    else:
        left = max(work_area.left, min(target.right - width, work_area.right - width))
    return Rect(left, top, left + width, top + height)


class Win32:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.SetWindowPos.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.PostMessageW.restype = wintypes.BOOL

    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SW_RESTORE = 9
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    HWND_TOPMOST = wintypes.HWND(-1)
    WM_CLOSE = 0x0010
    MONITOR_DEFAULTTONEAREST = 2

    @classmethod
    def windows(cls) -> list[WindowInfo]:
        rows: list[WindowInfo] = []

        @cls.enum_proc_type
        def visit(hwnd: int, _lparam: int) -> bool:
            length = cls.user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            cls.user32.GetWindowTextW(hwnd, buffer, len(buffer))
            pid = wintypes.DWORD()
            cls.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = wintypes.RECT()
            cls.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append(WindowInfo(
                hwnd=int(hwnd),
                pid=int(pid.value),
                title=buffer.value,
                rect=Rect(rect.left, rect.top, rect.right, rect.bottom),
                visible=bool(cls.user32.IsWindowVisible(hwnd)),
                minimized=bool(cls.user32.IsIconic(hwnd)),
            ))
            return True

        cls.user32.EnumWindows(visit, 0)
        return rows

    @classmethod
    def work_area(cls, hwnd: int) -> Rect:
        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        monitor = cls.user32.MonitorFromWindow(hwnd, cls.MONITOR_DEFAULTTONEAREST)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not cls.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return Rect(0, 0, 1920, 1080)
        rect = info.rcWork
        return Rect(rect.left, rect.top, rect.right, rect.bottom)

    @classmethod
    def foreground_pid(cls) -> int:
        hwnd = cls.user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        cls.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)


class DockedWorkbench:
    def __init__(self, config_path: Path):
        self.config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        self.stop_event = threading.Event()
        self.edge: subprocess.Popen[bytes] | None = None
        self.dock_hwnd = 0
        self._qianniu_pids: set[int] = set()
        self._qianniu_pids_at = 0.0
        self._dock_visible = False
        self._dock_rect: Rect | None = None

    def qianniu_pids(self) -> set[int]:
        now = time.monotonic()
        if now - self._qianniu_pids_at < 2.0:
            return set(self._qianniu_pids)
        expected = Path(str(self.config.get("qianniu_exe") or "runtime/AliWorkbench.exe"))
        if not expected.is_absolute():
            expected = ROOT / expected
        expected_name = expected.name.lower()
        root = expected.parent
        result: set[int] = set()
        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if str(process.info.get("name") or "").lower() != expected_name:
                    continue
                exe = Path(str(process.info.get("exe") or ""))
                if not str(exe) or exe.resolve().parent != root:
                    continue
                result.add(int(process.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        self._qianniu_pids = result
        self._qianniu_pids_at = now
        return set(result)

    @staticmethod
    def select_target(windows: list[WindowInfo], pids: set[int]) -> WindowInfo | None:
        candidates = [
            item for item in windows
            if item.pid in pids and item.visible and item.rect.width >= 700 and item.rect.height >= 500
        ]
        if not candidates:
            return None
        preferred = [item for item in candidates if "接待中心" in item.title]
        pool = preferred or [item for item in candidates if "千牛工作台" in item.title] or candidates
        return max(pool, key=lambda item: item.rect.width * item.rect.height)

    def edge_pids(self) -> set[int]:
        result = {
            process.pid
            for process in self.profile_edge_processes(ROOT / "state" / "dock-edge-profile")
        }
        if self.edge is None:
            return result
        result.add(int(self.edge.pid))
        try:
            result.update(child.pid for child in psutil.Process(self.edge.pid).children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return result

    def workbench_url(self) -> str:
        return f"http://127.0.0.1:{int(self.config.get('workbench_port', DEFAULT_WORKBENCH_PORT))}/"

    @staticmethod
    def profile_edge_processes(profile: Path) -> list[psutil.Process]:
        expected = str(profile.resolve()).lower()
        result: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if str(process.info.get("name") or "").lower() != "msedge.exe":
                    continue
                command = " ".join(process.info.get("cmdline") or []).lower()
                if expected in command and "--user-data-dir" in command:
                    result.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return result

    @classmethod
    def stop_profile_edge(cls, profile: Path, timeout: float = 2.0) -> None:
        processes = cls.profile_edge_processes(profile)
        for process in reversed(processes):
            try:
                process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _gone, alive = psutil.wait_procs(processes, timeout=timeout)
        for process in alive:
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=timeout)

    def wait_for_workbench(self, timeout: float = 45.0) -> None:
        deadline = time.time() + timeout
        while not self.stop_event.is_set():
            try:
                request = urllib.request.Request(self.workbench_url(), method="GET")
                with urllib.request.urlopen(request, timeout=0.75) as response:
                    page = response.read(131072).decode("utf-8", errors="ignore")
                    if response.status == 200 and DOCK_TITLE in page:
                        return
            except (OSError, urllib.error.URLError):
                pass
            if time.time() >= deadline:
                raise RuntimeError("local workbench did not become ready")
            self.stop_event.wait(0.25)
        raise RuntimeError("dock startup was cancelled")

    def launch(self, initial: Rect) -> None:
        edge_path = Path(str(self.config.get(
            "dock_edge_path",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        )))
        if not edge_path.is_file():
            raise FileNotFoundError(f"Microsoft Edge not found: {edge_path}")
        profile = ROOT / "state" / "dock-edge-profile"
        profile.mkdir(parents=True, exist_ok=True)
        self.stop_profile_edge(profile)
        url = f"http://127.0.0.1:{int(self.config.get('workbench_port', DEFAULT_WORKBENCH_PORT))}/?dock=1"
        self.edge = subprocess.Popen(
            [
                str(edge_path),
                f"--app={url}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-component-update",
                "--disk-cache-size=52428800",
                "--media-cache-size=10485760",
                f"--window-position={initial.left},{initial.top}",
                f"--window-size={initial.width},{initial.height}",
            ],
            cwd=str(ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def find_dock_window(self, windows: list[WindowInfo]) -> WindowInfo | None:
        owned_pids = self.edge_pids()
        for item in windows:
            if item.pid not in owned_pids or DOCK_TITLE not in item.title:
                continue
            try:
                if psutil.Process(item.pid).name().lower() == "msedge.exe":
                    return item
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    def close(self) -> None:
        if self.dock_hwnd:
            Win32.user32.PostMessageW(self.dock_hwnd, Win32.WM_CLOSE, 0, 0)
        self.stop_profile_edge(ROOT / "state" / "dock-edge-profile")
        self.edge = None

    def set_dock_visible(self, visible: bool) -> None:
        if not self.dock_hwnd or self._dock_visible == visible:
            return
        Win32.user32.ShowWindow(
            self.dock_hwnd,
            Win32.SW_SHOWNOACTIVATE if visible else Win32.SW_HIDE,
        )
        self._dock_visible = visible

    def run(self) -> int:
        width = max(240, min(420, int(self.config.get("dock_width", 286))))
        poll_delay = max(0.25, min(2.0, float(self.config.get("dock_poll_interval_seconds", 0.5))))
        self.wait_for_workbench()
        while not self.stop_event.is_set():
            windows = Win32.windows()
            pids = self.qianniu_pids()
            target = self.select_target(windows, pids)
            if target is not None:
                initial = choose_dock_rect(target.rect, Win32.work_area(target.hwnd), width)
                self.launch(initial)
                break
            self.stop_event.wait(0.25)
        if self.edge is None:
            return 1

        dock_seen = False
        dock_missing_since = 0.0
        while not self.stop_event.is_set():
            windows = Win32.windows()
            pids = self.qianniu_pids()
            target = self.select_target(windows, pids)
            dock = self.find_dock_window(windows)
            if dock is not None:
                if self.dock_hwnd != dock.hwnd:
                    self._dock_visible = dock.visible
                    self._dock_rect = None
                self.dock_hwnd = dock.hwnd
                dock_seen = True
                dock_missing_since = 0.0
            else:
                self.dock_hwnd = 0
                self._dock_visible = False
                self._dock_rect = None
                dock_missing_since = dock_missing_since or time.time()
                retry_after = 1.0 if dock_seen else 20.0
                if target is not None and time.time() - dock_missing_since >= retry_after:
                    initial = choose_dock_rect(target.rect, Win32.work_area(target.hwnd), width)
                    self.launch(initial)
                    dock_seen = False
                    dock_missing_since = 0.0
            if target is None or not self.dock_hwnd or target.minimized:
                self.set_dock_visible(False)
                self.stop_event.wait(poll_delay)
                continue
            foreground_pid = Win32.foreground_pid()
            active_pids = pids | {dock.pid}
            if foreground_pid not in active_pids:
                self.set_dock_visible(False)
                self.stop_event.wait(poll_delay)
                continue
            desired = choose_dock_rect(target.rect, Win32.work_area(target.hwnd), width)
            if self._dock_rect != desired or not self._dock_visible:
                if not Win32.user32.SetWindowPos(
                    self.dock_hwnd,
                    Win32.HWND_TOPMOST,
                    desired.left,
                    desired.top,
                    desired.width,
                    desired.height,
                    Win32.SWP_NOACTIVATE | Win32.SWP_SHOWWINDOW,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._dock_rect = desired
                self._dock_visible = True
            self.stop_event.wait(poll_delay)
        self.close()
        return 0


def close_existing() -> int:
    closed = 0
    for window in Win32.windows():
        if DOCK_TITLE in window.title:
            try:
                process = psutil.Process(window.pid)
                if process.name().lower() != "msedge.exe":
                    continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            Win32.user32.PostMessageW(window.hwnd, Win32.WM_CLOSE, 0, 0)
            closed += 1
    return closed


def main() -> int:
    parser = argparse.ArgumentParser(description="Dock the local reception list beside Qianniu")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    if args.stop:
        if sys.stdout is not None:
            print(f"DOCK_WINDOWS_CLOSED:{close_existing()}")
        else:
            close_existing()
        return 0
    app = DockedWorkbench(Path(args.config))
    signal.signal(signal.SIGINT, lambda *_: app.stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: app.stop_event.set())
    try:
        return app.run()
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
