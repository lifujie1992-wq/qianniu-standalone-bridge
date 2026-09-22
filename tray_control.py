"""Windows-native tray owner and compact control center.

The module deliberately knows nothing about bridge process creation.  Callers pass
the lifecycle actions and a status provider, which keeps this UI usable from both
``launcher`` and the frozen ``qianniu_app`` entry point.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping


PRODUCT_NAME = "千牛 AI 客服助手"
MB_YESNO = 0x00000004
MB_ICONWARNING = 0x00000030
MB_SETFOREGROUND = 0x00010000
MB_TOPMOST = 0x00040000
IDYES = 6
STATUS_TEXT = {
    "starting": "正在启动",
    "healthy": "运行正常",
    "degraded": "连接异常",
    "stopped": "已停止",
    "exiting": "正在彻底退出",
}


@dataclass(frozen=True)
class ComponentStatus:
    state: str = "stopped"
    detail: str = "未运行"


@dataclass(frozen=True)
class AppStatus:
    state: str = "stopped"
    version: str = ""
    components: Mapping[str, ComponentStatus] = field(default_factory=dict)
    error: str = ""

    @property
    def label(self) -> str:
        return STATUS_TEXT.get(self.state, STATUS_TEXT["degraded"])


@dataclass
class TrayCallbacks:
    open_workbench: Callable[[], None]
    start_all: Callable[[], None]
    restart_all: Callable[[], None]
    open_settings: Callable[[], None]
    completely_exit: Callable[[], None]


def coerce_status(value: AppStatus | Mapping[str, object] | None) -> AppStatus:
    """Normalize a caller status payload without requiring a shared model."""
    if isinstance(value, AppStatus):
        return value
    if not isinstance(value, Mapping):
        return AppStatus(state="degraded", error="无法读取运行状态")
    state = str(value.get("state") or "degraded")
    if state not in STATUS_TEXT:
        state = "degraded"
    raw_components = value.get("components")
    components: dict[str, ComponentStatus] = {}
    if isinstance(raw_components, Mapping):
        for key, item in raw_components.items():
            if isinstance(item, ComponentStatus):
                components[str(key)] = item
            elif isinstance(item, Mapping):
                components[str(key)] = ComponentStatus(
                    state=str(item.get("state") or "stopped"),
                    detail=str(item.get("detail") or ""),
                )
    return AppStatus(
        state=state,
        version=str(value.get("version") or ""),
        components=components,
        error=str(value.get("error") or ""),
    )


if sys.platform == "win32":
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    WM_APP = 0x8000
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_COMMAND = 0x0111
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    NIM_ADD = 0
    NIM_MODIFY = 1
    NIM_DELETE = 2
    NIM_SETVERSION = 4
    NIF_MESSAGE = 0x01
    NIF_ICON = 0x02
    NIF_TIP = 0x04
    NOTIFYICON_VERSION_4 = 4
    TPM_RETURNCMD = 0x0100
    TPM_RIGHTBUTTON = 0x0002
    MF_STRING = 0x0000
    MF_SEPARATOR = 0x0800
    MF_GRAYED = 0x0001
    MF_DISABLED = 0x0002
    IDI_APPLICATION = 32512
    IDI_WARNING = 32515
    IDI_ERROR = 32513
    IDI_INFORMATION = 32516
    CW_USEDEFAULT = -2147483648
    WM_TRAY = WM_APP + 71

    CMD_OPEN = 1001
    CMD_WORKBENCH = 1002
    CMD_RESTART = 1003
    CMD_SETTINGS = 1004
    CMD_EXIT = 1005

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.LoadIconW.restype = wintypes.HICON
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.WORD
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
    ]
    user32.AppendMenuW.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, ctypes.POINTER(wintypes.RECT),
    ]
    user32.TrackPopupMenu.restype = wintypes.UINT
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.DestroyMenu.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW),
    ]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class NativeTrayIcon:
    """A Shell_NotifyIcon host with a native, keyboard-accessible menu."""

    def __init__(
        self,
        dispatch: Callable[[str], None],
        initial_status: AppStatus | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._status = initial_status or AppStatus(state="starting")
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._notify_data = None
        self._wndproc = None
        self._taskbar_created = 0

    def start(self, timeout: float = 3.0) -> bool:
        if sys.platform != "win32":
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._message_loop, daemon=True)
        self._thread.start()
        return self._ready.wait(timeout)

    def stop(self) -> None:
        if sys.platform == "win32" and self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def update(self, status: AppStatus | Mapping[str, object] | None) -> None:
        with self._lock:
            self._status = coerce_status(status)
        if sys.platform == "win32" and self._hwnd:
            user32.PostMessageW(self._hwnd, WM_APP + 72, 0, 0)

    def _snapshot(self) -> AppStatus:
        with self._lock:
            return self._status

    def _icon_id(self, state: str) -> int:
        if state in {"starting", "degraded"}:
            return IDI_WARNING
        if state == "exiting":
            return IDI_ERROR
        if state == "healthy":
            return IDI_INFORMATION
        return IDI_APPLICATION

    def _fill_notify_data(self) -> "NOTIFYICONDATAW":
        status = self._snapshot()
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAY
        data.hIcon = user32.LoadIconW(0, self._icon_id(status.state))
        data.szTip = f"{PRODUCT_NAME} · {status.label}"[:127]
        data.uTimeoutOrVersion = NOTIFYICON_VERSION_4
        return data

    def _add_icon(self) -> None:
        self._notify_data = self._fill_notify_data()
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._notify_data))
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(self._notify_data))

    def _update_icon(self) -> None:
        self._notify_data = self._fill_notify_data()
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._notify_data))

    def _delete_icon(self) -> None:
        if self._notify_data is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._notify_data))
            self._notify_data = None

    def _show_menu(self) -> None:
        status = self._snapshot()
        busy = status.state in {"starting", "exiting"}
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            disabled = MF_STRING | MF_GRAYED | MF_DISABLED
            user32.AppendMenuW(menu, disabled, 0, status.label)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "打开控制中心")
            flags = MF_STRING | (MF_GRAYED | MF_DISABLED if busy else 0)
            user32.AppendMenuW(menu, flags, CMD_WORKBENCH, "打开工作台")
            user32.AppendMenuW(menu, flags, CMD_RESTART, "重新启动全部")
            user32.AppendMenuW(menu, flags, CMD_SETTINGS, "设置")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "彻底退出")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self._hwnd)
            command = user32.TrackPopupMenu(
                menu,
                TPM_RETURNCMD | TPM_RIGHTBUTTON,
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )
            actions = {
                CMD_OPEN: "open",
                CMD_WORKBENCH: "workbench",
                CMD_RESTART: "restart",
                CMD_SETTINGS: "settings",
                CMD_EXIT: "exit",
            }
            if command in actions:
                self._dispatch(actions[command])
        finally:
            user32.DestroyMenu(menu)

    def _message_loop(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        class_name = f"QianniuAiTrayOwner_{os.getpid()}"

        @WNDPROC
        def window_proc(hwnd, message, wparam, lparam):
            if message == self._taskbar_created:
                self._add_icon()
                return 0
            if message == WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event == WM_LBUTTONDBLCLK:
                    self._dispatch("open")
                elif event == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if message == WM_APP + 72:
                self._update_icon()
                return 0
            if message == WM_CLOSE:
                self._delete_icon()
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = window_proc
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = window_proc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            self._ready.set()
            return
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._hwnd = user32.CreateWindowExW(
            0, class_name, PRODUCT_NAME, 0, CW_USEDEFAULT, CW_USEDEFAULT,
            0, 0, 0, 0, instance, None,
        )
        if not self._hwnd:
            user32.UnregisterClassW(class_name, instance)
            self._ready.set()
            return
        self._add_icon()
        self._ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        self._delete_icon()
        self._hwnd = 0
        user32.UnregisterClassW(class_name, instance)


class TrayControlCenter:
    """Tk status window coordinated with a native Windows tray icon."""

    COMPONENTS = (
        ("bridge", "桥接服务"),
        ("qianniu", "千牛客户端"),
        ("dock", "右侧浮层"),
    )

    def __init__(
        self,
        callbacks: TrayCallbacks,
        status_provider: Callable[[], AppStatus | Mapping[str, object] | None],
        *,
        poll_ms: int = 5000,
        state_dir: Path | None = None,
    ) -> None:
        self.callbacks = callbacks
        self.status_provider = status_provider
        self.poll_ms = poll_ms
        self._failures = 0
        self._exiting = False
        self._commands: queue.Queue[str] = queue.Queue()
        root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
            else Path(__file__).resolve().parent
        self.state_dir = Path(state_dir) if state_dir is not None else root / "state"
        self.pid_file = self.state_dir / "tray.pid"
        self.exit_file = self.state_dir / "tray.exit"
        self.show_file = self.state_dir / "tray.show"
        self.root = tk.Tk()
        self.root.title(PRODUCT_NAME)
        self.root.geometry("460x360")
        self.root.minsize(460, 360)
        self.root.resizable(False, True)
        self.root.configure(background="#EFF5F1")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda _event: self.hide())
        self._build_window()
        self.tray = NativeTrayIcon(self._commands.put, AppStatus(state="starting"))

    def _build_window(self) -> None:
        header = tk.Frame(self.root, bg="#087A5B", padx=20, pady=16)
        header.pack(fill="x")
        self.status_label = tk.Label(
            header, text=STATUS_TEXT["starting"], bg="#087A5B", fg="white",
            font=("Microsoft YaHei UI", 18, "bold"), anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.version_label = tk.Label(
            header, text="", bg="#087A5B", fg="white",
            font=("Consolas", 10), anchor="e",
        )
        self.version_label.pack(side="right")

        body = tk.Frame(self.root, bg="#FCFEFD", padx=20, pady=8)
        body.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        self.component_labels: dict[str, tuple[tk.Label, tk.Label]] = {}
        for index, (key, title) in enumerate(self.COMPONENTS):
            row = tk.Frame(body, bg="#FCFEFD", pady=8)
            row.pack(fill="x")
            tk.Label(
                row, text=title, width=12, anchor="w", bg="#FCFEFD", fg="#183029",
                font=("Microsoft YaHei UI", 11),
            ).pack(side="left")
            state = tk.Label(
                row, text="已停止", width=10, anchor="w", bg="#FCFEFD", fg="#60766E",
                font=("Microsoft YaHei UI", 10),
            )
            state.pack(side="left")
            detail = tk.Label(
                row, text="未运行", anchor="w", bg="#FCFEFD", fg="#60766E",
                font=("Consolas", 9),
            )
            detail.pack(side="left", fill="x", expand=True)
            self.component_labels[key] = (state, detail)
            if index < len(self.COMPONENTS) - 1:
                tk.Frame(body, height=1, bg="#CBD9D2").pack(fill="x")
        self.error_label = tk.Label(
            body, text="", anchor="w", bg="#FCFEFD", fg="#BD3F50",
            font=("Microsoft YaHei UI", 9),
        )
        self.error_label.pack(fill="x", pady=(4, 0))

        footer = tk.Frame(self.root, bg="#EFF5F1", padx=16, pady=12)
        footer.pack(fill="x")
        self.primary_button = tk.Button(
            footer, text="启动服务", command=self._primary_action,
            bg="#087A5B", fg="white", activebackground="#14765A",
            activeforeground="white", relief="flat", padx=16, pady=6,
            font=("Microsoft YaHei UI", 10),
        )
        self.primary_button.pack(side="right")
        self.settings_button = tk.Button(
            footer, text="设置", command=lambda: self._invoke("settings"), padx=12, pady=5,
        )
        self.settings_button.pack(side="right", padx=(0, 8))
        self.restart_button = tk.Button(
            footer, text="重新启动", command=lambda: self._invoke("restart"), padx=12, pady=5,
        )
        self.restart_button.pack(side="right", padx=(0, 8))

    def run(self, *, show_initially: bool = True) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.exit_file.unlink(missing_ok=True)
        self.show_file.unlink(missing_ok=True)
        self.pid_file.write_text(str(os.getpid()), encoding="ascii")
        try:
            self.tray.start()
            if not show_initially:
                self.root.withdraw()
            self.root.after(50, self._drain_commands)
            self.root.after(0, self._refresh)
            self.root.mainloop()
        finally:
            self._remove_lifecycle_files()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()

    def close(self) -> None:
        self._exiting = True
        self.tray.stop()
        self._remove_lifecycle_files()
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except tk.TclError:
            pass

    def _remove_lifecycle_files(self) -> None:
        try:
            owner = int(self.pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            owner = 0
        if owner == os.getpid():
            self.pid_file.unlink(missing_ok=True)
        self.exit_file.unlink(missing_ok=True)
        self.show_file.unlink(missing_ok=True)

    def _drain_commands(self) -> None:
        if self.show_file.is_file():
            self.show_file.unlink(missing_ok=True)
            self.show()
        if self.exit_file.is_file():
            self.exit_file.unlink(missing_ok=True)
            self._exit_without_confirmation()
            return
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            self._invoke(command)
        if not self._exiting:
            self.root.after(50, self._drain_commands)

    def _invoke(self, command: str) -> None:
        if command == "open":
            self.show()
        elif command == "workbench":
            self.callbacks.open_workbench()
        elif command == "restart":
            self.callbacks.restart_all()
        elif command == "settings":
            self.callbacks.open_settings()
        elif command == "exit":
            self._confirm_exit()

    def _exit_without_confirmation(self) -> None:
        self._begin_exit()

    def _begin_exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        self._apply_status(AppStatus(state="exiting"))
        self.root.update_idletasks()

        def worker() -> None:
            try:
                self.callbacks.completely_exit()
            finally:
                try:
                    self.root.after(0, self.close)
                except tk.TclError:
                    self.close()

        threading.Thread(target=worker, name="tray-complete-exit", daemon=False).start()

    def _confirm_exit(self) -> None:
        if self._exiting:
            return
        answer = ctypes.windll.user32.MessageBoxW(
            0,
            "将关闭客服助手、浮层和随包千牛。确定退出？",
            PRODUCT_NAME,
            MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST,
        ) if sys.platform == "win32" else 7
        if answer != IDYES:
            return
        self._begin_exit()

    def _primary_action(self) -> None:
        if getattr(self, "_current", AppStatus()).state == "healthy":
            self.callbacks.open_workbench()
        else:
            self.callbacks.start_all()

    def _refresh(self) -> None:
        if self._exiting:
            return
        try:
            raw = self.status_provider()
            status = coerce_status(raw)
            if raw is None:
                raise RuntimeError("status unavailable")
            self._failures = 0
        except Exception as error:
            self._failures += 1
            if self._failures < 2 and hasattr(self, "_current"):
                status = self._current
            else:
                status = AppStatus(state="degraded", error=str(error) or "状态检查失败")
        self._apply_status(status)
        self.root.after(self.poll_ms, self._refresh)

    def _apply_status(self, status: AppStatus) -> None:
        self._current = status
        self.status_label.configure(text=status.label)
        self.version_label.configure(text=f"v{status.version}" if status.version else "")
        self.error_label.configure(text=status.error)
        for key, _title in self.COMPONENTS:
            component = status.components.get(key, ComponentStatus())
            label = STATUS_TEXT.get(component.state, component.state or "未知")
            color = "#14765A" if component.state == "healthy" else "#AD6200"
            if component.state == "stopped":
                color = "#60766E"
            if component.state in {"degraded", "exiting"}:
                color = "#BD3F50"
            state_label, detail_label = self.component_labels[key]
            state_label.configure(text=label, fg=color)
            detail_label.configure(text=component.detail)
        busy = status.state in {"starting", "exiting"}
        normal = tk.DISABLED if busy else tk.NORMAL
        self.primary_button.configure(
            text="打开工作台" if status.state == "healthy" else "启动服务",
            state=normal,
        )
        self.restart_button.configure(state=normal)
        self.settings_button.configure(state=normal)
        self.tray.update(status)


__all__ = [
    "AppStatus",
    "ComponentStatus",
    "NativeTrayIcon",
    "STATUS_TEXT",
    "TrayCallbacks",
    "TrayControlCenter",
    "coerce_status",
]
