from __future__ import annotations

import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
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
# 福客's follow panel is a frameless 300x720 strip; match its footprint so the
# dock reads as the same kind of native app component instead of a browser popup.
DEFAULT_DOCK_WIDTH = 300
DEFAULT_DOCK_HEIGHT = 720
LOG = logging.getLogger("qianniu.dock")


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

    # ctypes otherwise defaults to 32-bit integers, truncating handles on x64.
    user32.EnumWindows.argtypes = (enum_proc_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    user32.GetMonitorInfoW.restype = wintypes.BOOL

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

    # Window-long accessors are Ptr on 64-bit and plain Long on 32-bit builds.
    _has_long_ptr = hasattr(user32, "GetWindowLongPtrW")
    if _has_long_ptr:
        user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetWindowLongPtrW.restype = ctypes.c_longlong
        user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_longlong)
        user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    else:  # pragma: no cover - 32-bit interpreter only
        user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
        user32.SetWindowLongW.restype = ctypes.c_long

    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SW_RESTORE = 9
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040
    HWND_TOPMOST = wintypes.HWND(-1)
    HWND_NOTOPMOST = wintypes.HWND(-2)
    WM_CLOSE = 0x0010
    MONITOR_DEFAULTTONEAREST = 2

    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_SYSMENU = 0x00080000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_BORDER = 0x00800000
    WS_DLGFRAME = 0x00400000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000

    _FRAME_BITS = (
        WS_CAPTION | WS_THICKFRAME | WS_SYSMENU
        | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_BORDER | WS_DLGFRAME
    )

    @classmethod
    def windows(cls) -> list[WindowInfo]:
        rows: list[WindowInfo] = []

        @cls.enum_proc_type
        def visit(hwnd: int, _lparam: int) -> bool:
            if not cls.user32.IsWindow(hwnd):
                return True
            length = cls.user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            cls.user32.GetWindowTextW(hwnd, buffer, len(buffer))
            pid = wintypes.DWORD()
            cls.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = wintypes.RECT()
            if not cls.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
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

    @classmethod
    def _get_long(cls, hwnd: int, index: int) -> int:
        if cls._has_long_ptr:
            return int(cls.user32.GetWindowLongPtrW(hwnd, index))
        return int(cls.user32.GetWindowLongW(hwnd, index))

    @classmethod
    def _set_long(cls, hwnd: int, index: int, value: int) -> None:
        if cls._has_long_ptr:
            cls.user32.SetWindowLongPtrW(hwnd, index, value)
        else:  # pragma: no cover - 32-bit interpreter only
            cls.user32.SetWindowLongW(hwnd, index, value)

    @classmethod
    def style(cls, hwnd: int) -> tuple[int, int]:
        return cls._get_long(hwnd, cls.GWL_STYLE), cls._get_long(hwnd, cls.GWL_EXSTYLE)

    @classmethod
    def apply_app_frame(cls, hwnd: int, *, skip_taskbar: bool = True) -> bool:
        """Turn an Edge `--app` window into a frameless app panel.

        Chromium keeps the stock title bar (WS_CAPTION/WS_THICKFRAME) on its
        `--app` windows, which is exactly what makes the dock read as a browser
        popup instead of a native panel. We strip the frame bits and, for the
        dock, also hide it from the taskbar (WS_EX_TOOLWINDOW) so it behaves
        like 福客's follow panel. Returns True when something changed.
        """
        style, ex_style = cls.style(hwnd)
        changed = False
        new_style = style & ~cls._FRAME_BITS
        if new_style != style:
            cls._set_long(hwnd, cls.GWL_STYLE, new_style)
            changed = True
        if skip_taskbar:
            new_ex = (ex_style | cls.WS_EX_TOOLWINDOW) & ~cls.WS_EX_APPWINDOW
            if new_ex != ex_style:
                cls._set_long(hwnd, cls.GWL_EXSTYLE, new_ex)
                changed = True
        if changed:
            cls.user32.SetWindowPos(
                hwnd,
                None,
                0,
                0,
                0,
                0,
                cls.SWP_NOSIZE | cls.SWP_NOMOVE | cls.SWP_NOZORDER | cls.SWP_FRAMECHANGED,
            )
        return changed

    @classmethod
    def is_frameless(cls, hwnd: int) -> bool:
        style, _ex_style = cls.style(hwnd)
        return not (style & cls.WS_CAPTION)


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
                with urllib.request.urlopen(request, timeout=5.0) as response:
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
        LOG.info("Edge launched pid=%s rect=%s", self.edge.pid, initial)

    def find_dock_window(self, windows: list[WindowInfo]) -> WindowInfo | None:
        owned_pids = self.edge_pids()
        candidates: list[WindowInfo] = []
        for item in windows:
            if item.pid not in owned_pids or DOCK_TITLE not in item.title:
                continue
            # Chromium parks a second, off-screen helper window with the same
            # title; keep only real on-screen panels (hidden-but-placed counts).
            if item.rect.left <= -1000 or item.rect.width < 80:
                continue
            try:
                if psutil.Process(item.pid).name().lower() != "msedge.exe":
                    continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            candidates.append(item)
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.rect.width * item.rect.height)

    def close(self) -> None:
        LOG.info("Closing dock hwnd=%s", self.dock_hwnd)
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

    def browser_closed(self, dock: WindowInfo | None) -> bool:
        """A Chromium launcher can exit while its actual app window lives on."""
        if dock is not None or self.edge is None:
            return False
        if self.edge.poll() != 0:
            return False
        return not self.profile_edge_processes(ROOT / "state" / "dock-edge-profile")

    def place_dock(self, desired: Rect, *, topmost: bool) -> bool:
        insert_after = Win32.HWND_TOPMOST if topmost else Win32.HWND_NOTOPMOST
        ctypes.set_last_error(0)
        if Win32.user32.SetWindowPos(
            self.dock_hwnd, insert_after,
            desired.left, desired.top, desired.width, desired.height,
            Win32.SWP_NOACTIVATE | Win32.SWP_SHOWWINDOW,
        ):
            self._dock_rect = desired
            self._dock_visible = True
            return True
        # Windows can replace an Edge HWND between enumeration and positioning.
        # Rediscover it on the next pass instead of exiting and killing Edge.
        LOG.warning("Dock position failed hwnd=%s error=%s; rediscovering window",
                    self.dock_hwnd, ctypes.get_last_error())
        self.dock_hwnd = 0
        self._dock_rect = None
        self._dock_visible = False
        return False

    def run(self) -> int:
        mode = str(self.config.get("dock_mode") or "snap").strip().lower()
        if mode == "snap":
            return self.run_snapping()
        return self.run_floating()

    def screen_size(self) -> tuple[int, int]:
        return (
            int(Win32.user32.GetSystemMetrics(0)),
            int(Win32.user32.GetSystemMetrics(1)),
        )

    def panel_options(self) -> dict[str, bool]:
        return {
            "frameless": bool(self.config.get("dock_frameless", True)),
            "skip_taskbar": bool(self.config.get("dock_skip_taskbar", True)),
            "follow_height": bool(self.config.get("dock_follow_height", True)),
            "topmost": bool(self.config.get("dock_topmost", False)),
        }

    def apply_panel_frame(self, dock: WindowInfo) -> None:
        """Strip the browser title bar so the dock looks like a native panel."""
        options = self.panel_options()
        if not options["frameless"] and not options["skip_taskbar"]:
            return
        Win32.apply_app_frame(dock.hwnd, skip_taskbar=options["skip_taskbar"])

    def seed_rect(self, target: WindowInfo, width: int, options: dict[str, bool]) -> Rect:
        rect = choose_dock_rect(target.rect, Win32.work_area(target.hwnd), width)
        if not options["follow_height"]:
            height = max(320, min(1600, int(self.config.get("dock_height", DEFAULT_DOCK_HEIGHT))))
            rect = Rect(rect.left, rect.top, rect.right, rect.top + height)
        return rect

    def run_floating(self) -> int:
        """Show the workbench as a plain floating app window.

        No snapping to Qianniu, no auto-hide when another app is focused, no
        forced topmost: the window stays where the user puts it and behaves
        like any other app window. Edge remembers its own geometry for the
        dedicated profile after the first launch, so dock_x/dock_y/
        dock_width/dock_height only seed the first position.
        """
        width = max(240, min(720, int(self.config.get("dock_width", DEFAULT_DOCK_WIDTH))))
        height = max(320, min(1600, int(self.config.get("dock_height", DEFAULT_DOCK_HEIGHT))))
        screen_w, _screen_h = self.screen_size()
        left = int(self.config.get("dock_x", max(0, screen_w - width - 24)))
        top = int(self.config.get("dock_y", 80))
        self.wait_for_workbench()
        self.launch(Rect(left, top, left + width, top + height))
        # Stay alive until the app is stopped or the user closes the window.
        last_check = 0.0
        dock_seen = False
        missing_since = 0.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now - last_check >= 2.0:
                last_check = now
                dock = self.find_dock_window(Win32.windows())
                if dock is not None:
                    dock_seen = True
                    missing_since = 0.0
                    self.dock_hwnd = dock.hwnd
                    if not Win32.is_frameless(dock.hwnd):
                        self.apply_panel_frame(dock)
                else:
                    missing_since = missing_since or now
                    if dock_seen and now - missing_since >= 5.0 and self.browser_closed(dock):
                        LOG.info("Floating window closed; Edge exit code=%s", self.edge.poll())
                        break
                    if now - missing_since >= 20.0:
                        LOG.warning("Floating window missing for 20s; restarting dock browser")
                        self.launch(Rect(left, top, left + width, top + height))
                        dock_seen = False
                        missing_since = 0.0
            self.stop_event.wait(1.0)
        self.close()
        return 0

    def run_snapping(self) -> int:
        """Follow Qianniu like 福客's panel: sit beside it, match its height."""
        options = self.panel_options()
        width = max(240, min(420, int(self.config.get("dock_width", DEFAULT_DOCK_WIDTH))))
        poll_delay = max(0.25, min(2.0, float(self.config.get("dock_poll_interval_seconds", 1.0))))
        self.wait_for_workbench()
        while not self.stop_event.is_set():
            windows = Win32.windows()
            pids = self.qianniu_pids()
            target = self.select_target(windows, pids)
            if target is not None:
                self.launch(self.seed_rect(target, width, options))
                break
            self.stop_event.wait(0.25)
        if self.edge is None:
            return 1

        dock_seen = False
        dock_missing_since = 0.0
        frame_hwnd = 0
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
                if frame_hwnd != dock.hwnd or not Win32.is_frameless(dock.hwnd):
                    self.apply_panel_frame(dock)
                    frame_hwnd = dock.hwnd
            else:
                self.dock_hwnd = 0
                self._dock_visible = False
                self._dock_rect = None
                frame_hwnd = 0
                dock_missing_since = dock_missing_since or time.time()
                if dock_seen and time.time() - dock_missing_since >= 5.0 and self.browser_closed(dock):
                    LOG.info("Snapped window closed; Edge exit code=%s", self.edge.poll())
                    break
                retry_after = 10.0 if dock_seen else 20.0
                if target is not None and time.time() - dock_missing_since >= retry_after:
                    LOG.warning("Dock window missing; restarting dock browser")
                    self.launch(self.seed_rect(target, width, options))
                    dock_seen = False
                    dock_missing_since = 0.0
            if target is None or not self.dock_hwnd or target.minimized:
                self.set_dock_visible(False)
                self.stop_event.wait(poll_delay)
                continue
            desired = self.seed_rect(target, width, options)
            if self._dock_rect != desired or not self._dock_visible:
                self.place_dock(desired, topmost=options["topmost"])
            self.stop_event.wait(poll_delay)
        self.close()
        return 0


def close_existing() -> int:
    # Scope to the Edge processes that use *this* install's dock profile, so a
    # stop from one install (or a dev/e2e copy under another root) never closes
    # another install's panel.
    owned = {process.pid for process in DockedWorkbench.profile_edge_processes(
        ROOT / "state" / "dock-edge-profile"
    )}
    closed = 0
    for window in Win32.windows():
        if DOCK_TITLE not in window.title or window.pid not in owned:
            continue
        try:
            if psutil.Process(window.pid).name().lower() != "msedge.exe":
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
    log_path = ROOT / "state" / "docked_workbench.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.info("Dock starting config=%s", args.config)
    app = DockedWorkbench(Path(args.config))
    signal.signal(signal.SIGINT, lambda *_: app.stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: app.stop_event.set())
    try:
        return app.run()
    except Exception:
        LOG.exception("Dock exited unexpectedly")
        raise
    finally:
        app.close()
        LOG.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
