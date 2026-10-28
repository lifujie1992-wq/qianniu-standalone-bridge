"""Self-contained Windows upgrade UI for existing QianniuAIService seats."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

import psutil
from client_support import agent_profiles, launcher_version, size_of_image
from config_defaults import apply_operational_defaults
from device_identity import stable_device_id


VERSION = "1.6.57"
PROTECTED = {"config.json", "state", "runtime", "upgrade-backups"}
PAYLOAD_FILES = {
    "QianniuAgent.exe", "_internal", "build", "browser_bridge.js", "appbiz_agent.js",
    "native_message_store.js", "native_agent.js", "workbench.html", "config.example.json",
    "build-info.json", "版本来源.json", "升级说明.txt",
}


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("配置文件格式异常")
    return value


def installation(path: Path) -> dict | None:
    try:
        root = path.resolve()
        if not (root / "QianniuAgent.exe").is_file() or not (root / "config.json").is_file():
            return None
        read_json(root / "config.json")
        try:
            version = str(read_json(root / "build-info.json").get("version") or "旧版")
        except (OSError, ValueError):
            version = "旧版"
        return {"path": str(root), "version": version}
    except (OSError, ValueError):
        return None


def running_candidates() -> list[Path]:
    roots = []
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if str(process.info.get("name") or "").lower() == "qianniuagent.exe":
                roots.append(Path(process.info["exe"]).parent)
        except (OSError, psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            continue
    return roots


def registry_candidates() -> list[Path]:
    if os.name != "nt":
        return []
    import winreg
    roots = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view) as uninstall:
                    for index in range(winreg.QueryInfoKey(uninstall)[0]):
                        try:
                            with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, index)) as key:
                                name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                                if "千牛 AI 客服助手" not in name and "qianniu ai" not in name.lower():
                                    continue
                                try:
                                    location = str(winreg.QueryValueEx(key, "InstallLocation")[0])
                                except OSError:
                                    command = str(winreg.QueryValueEx(key, "UninstallString")[0])
                                    location = str(Path(command.split('"')[1] if command.startswith('"') else command).parent)
                                roots.append(Path(location))
                        except OSError:
                            continue
            except OSError:
                continue
    return roots


def common_candidates() -> list[Path]:
    roots = [Path(f"{drive}:/QianniuAIService") for drive in "DCEFGHIJKLMNOPQRSTUVWXYZ"]
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]) / "QianniuAIService")
    return roots


def discover(candidates=None) -> list[dict]:
    if candidates is None:
        candidates = running_candidates() + registry_candidates() + common_candidates()
    found, seen = [], set()
    for candidate in candidates:
        row = installation(Path(candidate))
        if row is not None and os.path.normcase(row["path"]) not in seen:
            found.append(row)
            seen.add(os.path.normcase(row["path"]))
    return found


def extract_payload(archive: Path, destination: Path, expected: dict) -> Path:
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected["zip_sha256"]:
        raise RuntimeError("升级包校验失败，请重新获取升级程序。")
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            parts = PurePosixPath(entry.filename).parts
            if (not parts or parts[0] != "QianniuAIService" or ".." in parts
                    or "\\" in entry.filename or ":" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise RuntimeError("升级包含有非法文件路径。")
            if len(parts) > 1 and (parts[1] in PROTECTED or parts[1] not in PAYLOAD_FILES):
                raise RuntimeError("升级包含有不应覆盖的文件。")
        package.extractall(destination)
    source = destination / "QianniuAIService"
    if hashlib.sha256((source / "QianniuAgent.exe").read_bytes()).hexdigest() != expected["exe_sha256"]:
        raise RuntimeError("助手程序校验失败。")
    if read_json(source / "build-info.json")["version"] != VERSION:
        raise RuntimeError("升级包版本不匹配。")
    return source


def validate_runtime(root: Path, source: Path) -> None:
    runtime = root / "runtime"
    version = launcher_version(runtime)
    library = runtime / version / "AppBiz.dll" if version else runtime / "AppBiz.dll"
    if not library.is_file() or size_of_image(library) not in agent_profiles(source):
        raise RuntimeError("这台电脑的千牛版本不在本包支持范围，请先联系维护人员；原程序未改动。")


def checked_child(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if path.parent != root.resolve():
        raise RuntimeError("安装目录含有异常重定向，已停止升级。")
    return path


def check_preserved_config(original: bytes, path: Path) -> None:
    if path.read_bytes() == original:
        return
    expected = json.loads(original.decode("utf-8-sig"))
    # Permit only the migrations performed by the shipped application itself.
    # Workstation credentials and customer-specific settings must remain intact.
    apply_operational_defaults(expected)
    expected["device_id"] = stable_device_id()
    if read_json(path) != expected:
        raise RuntimeError("启动后配置发生意外变化。")


def replace_program(root: Path, source: Path, backup: Path, stop, start, progress=lambda _: None) -> dict:
    """Replace only allowlisted payload paths, restoring them if startup fails."""
    root, source, backup = root.resolve(), source.resolve(), backup.resolve()
    boundary = (root / "upgrade-backups").resolve()
    if (not backup.is_relative_to(boundary) or not boundary.is_relative_to(root)
            or backup.exists() or root == root.parent):
        raise RuntimeError("备份路径校验失败。")
    names = sorted(path.name for path in source.iterdir())
    if not names or any(name in PROTECTED or name not in PAYLOAD_FILES for name in names):
        raise RuntimeError("升级文件清单异常。")
    # Check all resolved move targets before stopping any process.
    for name in names:
        checked_child(root, name)
        checked_child(source, name)
        checked_child(backup, name)
    config_bytes = (root / "config.json").read_bytes()
    backup.mkdir(parents=True)
    (backup / "config.json").write_bytes(config_bytes)
    progress("正在安全停止旧助手…")
    stop()
    moved, created = [], []
    try:
        database = root / "state/bridge.sqlite3"
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True)) as old:
                with closing(sqlite3.connect(backup / "bridge.sqlite3")) as copy:
                    old.backup(copy)
        progress("正在备份并更新程序…")
        for name in names:
            target = checked_child(root, name)
            saved = checked_child(backup, name)
            # All moves stay inside the selected installation and its checked backup.
            if target.exists():
                target.rename(saved)
                moved.append(name)
            created.append(name)
            incoming = checked_child(source, name)
            if incoming.is_dir():
                if name == "build" and name in moved:
                    shutil.copytree(saved, target)
                    shutil.copytree(incoming, target, dirs_exist_ok=True)
                else:
                    shutil.copytree(incoming, target)
            else:
                shutil.copy2(incoming, target)
        if (root / "config.json").read_bytes() != config_bytes:
            raise RuntimeError("配置保留检查失败。")
        progress("正在启动并验证新版助手…")
        result = start(VERSION)
        check_preserved_config(config_bytes, root / "config.json")
    except Exception as original:
        progress("升级未完成，正在恢复旧版…")
        try:
            stop()
            for name in reversed(created):
                target = checked_child(root, name)
                if target.exists():
                    target.rename(checked_child(backup, "failed-new-" + name))
            for name in reversed(moved):
                checked_child(backup, name).rename(checked_child(root, name))
            (root / "config.json").write_bytes(config_bytes)
            start(None)
        except Exception as recovery:
            raise RuntimeError(f"自动恢复未完成。备份位于 {backup}，请联系维护人员。") from recovery
        raise RuntimeError(f"升级未完成，已恢复旧版。\n{original}") from original
    return {"version": VERSION, "path": str(root), "backup": str(backup), "api_ok": bool(result.get("ok"))}


class Seat:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.config = read_json(root / "config.json")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.roles = {role for _, role in self.processes()}
        self.want_dock = "dock" in self.roles or bool(self.config.get("dock_enabled"))

    def processes(self):
        found = []
        for process in psutil.process_iter(["exe", "cmdline"]):
            try:
                if Path(process.info.get("exe") or "").resolve() != self.root / "QianniuAgent.exe":
                    continue
                args = process.info.get("cmdline") or []
                role = args[args.index("--role")+1] if "--role" in args else "tray"
                found.append((process, role))
            except (OSError, psutil.NoSuchProcess, psutil.AccessDenied, IndexError):
                continue
        return found

    def status(self):
        with self.opener.open(f"http://127.0.0.1:{int(self.config.get('api_port',42111))}/api/v1/status", timeout=4) as response:
            return json.load(response)

    def ensure_idle(self):
        database = self.root / "state/bridge.sqlite3"
        if database.is_file():
            with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=3)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "sends" in tables and db.execute("SELECT count(*) FROM sends WHERE status IN ('in_flight','submitted')").fetchone()[0]:
                    raise RuntimeError("助手正在发送消息，请等这次接待结束后再点升级。")
        if any(role == "bridge" for _, role in self.processes()):
            try:
                status = self.status()
            except (OSError, ValueError) as error:
                raise RuntimeError("旧助手暂时没有响应，请稍后再试；原文件未改动。") from error
            if (status.get("context_enrich", {}).get("pending", 0)
                    or any(status.get("brain", {}).get("events", {}).get(key, 0) for key in ("pending", "sending"))):
                raise RuntimeError("助手还有任务正在处理，请在接待空档升级。")
        else:
            try:
                self.status()
            except (OSError, ValueError):
                pass
            else:
                raise RuntimeError("另一个安装目录的助手正在运行，请重新检测并选择正在运行的版本。")

    def stop(self):
        rows = self.processes()
        if any(role == "bridge" for _, role in rows):
            request = urllib.request.Request(
                f"http://127.0.0.1:{int(self.config.get('api_port',42111))}/api/v1/shutdown",
                data=b"{}", headers={"X-Bridge-Token": str(self.config.get("api_token", "")), "Content-Type": "application/json"},
            )
            try:
                with self.opener.open(request, timeout=3):
                    pass
            except OSError:
                pass
        for process, role in rows:
            try:
                if role == "dock":
                    for child in process.children(recursive=True):
                        if child.name().lower() == "msedge.exe":
                            child.terminate()
                if role != "bridge":
                    process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs([process for process, _ in rows], timeout=6)
        for process in alive:
            if Path(process.exe()).resolve() != self.root / "QianniuAgent.exe":
                raise RuntimeError("助手进程身份发生变化，已停止升级。")
            process.terminate()
        _, alive = psutil.wait_procs(alive, timeout=5)
        if alive or self.processes():
            raise RuntimeError("旧助手没有完全退出，请稍后重试。")

    def launch(self, role):
        args = [str(self.root / "QianniuAgent.exe")]
        if role != "tray":
            args += ["--role", role, "--config", str(self.root / "config.json")]
        process = subprocess.Popen(args, cwd=self.root, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        name = {"bridge": "standalone_bridge.pid", "dock": "docked_workbench.pid"}.get(role)
        if name:
            (self.root / "state").mkdir(exist_ok=True)
            (self.root / "state" / name).write_text(str(process.pid), encoding="ascii")
        return process

    def start(self, version):
        bridge = self.launch("bridge")
        deadline = time.monotonic()+45
        while time.monotonic() < deadline:
            if bridge.poll() is not None:
                raise RuntimeError("桥接进程启动失败。")
            try:
                status = self.status()
                if (version is None or (status.get("version") == version and status.get("ok"))):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("新版助手未能通过启动检查。")
        if self.want_dock or read_json(self.root / "config.json").get("dock_enabled"):
            self.launch("dock")
        self.launch("tray")
        return status


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def upgrade(root: Path, progress):
    if installation(root) is None:
        raise RuntimeError("未找到完整的旧版助手，请先打开原来的助手，再重新检测。")
    manifest = read_json(resource_root() / "payload-manifest.json")
    with tempfile.TemporaryDirectory(prefix="qn-upgrade-") as directory:
        progress("正在校验升级文件…")
        source = extract_payload(resource_root()/"payload.zip", Path(directory), manifest)
        validate_runtime(root, source)
        seat = Seat(root)
        seat.ensure_idle()
        backup = root / "upgrade-backups" / ("auto-" + VERSION + "-" + time.strftime("%Y%m%d-%H%M%S"))
        return replace_program(root, source, backup, seat.stop, seat.start, progress)


def show_window():
    import tkinter as tk
    from tkinter import filedialog, ttk
    window = tk.Tk()
    window.title(f"千牛助手 · 自动升级到 {VERSION}")
    window.geometry("620x340")
    window.resizable(False, False)
    window.configure(bg="#f4f7fb")
    events = queue.Queue()
    busy = False
    rows = []
    title = tk.Label(window, text="升级千牛客服助手", font=("Microsoft YaHei UI",20,"bold"), bg="#f4f7fb", fg="#15233b")
    title.pack(anchor="w", padx=28, pady=(25,8))
    tk.Label(window,text="保留原配置和聊天数据 · 自动备份 · 失败自动恢复旧版",font=("Microsoft YaHei UI",10),bg="#f4f7fb",fg="#55647a").pack(anchor="w",padx=30)
    selection = tk.StringVar()
    picker = ttk.Combobox(window,textvariable=selection,state="readonly",font=("Microsoft YaHei UI",10),width=65)
    picker.pack(fill="x",padx=30,pady=(23,8))
    info = tk.StringVar(value="正在寻找旧版助手…")
    tk.Label(window,textvariable=info,justify="left",wraplength=550,bg="#f4f7fb",fg="#33435a",font=("Microsoft YaHei UI",10)).pack(anchor="w",padx=30,pady=7)
    bar = ttk.Progressbar(window,mode="indeterminate")
    bar.pack(fill="x",padx=30,pady=10)
    controls = tk.Frame(window,bg="#f4f7fb")
    controls.pack(fill="x",padx=30,pady=10)

    def refresh(extra=None):
        nonlocal rows
        candidates = ([Path(extra)] if extra else []) + running_candidates()+registry_candidates()+common_candidates()
        rows = discover(candidates)
        picker["values"] = [row["path"]+"  （"+row["version"]+"）" for row in rows]
        if rows:
            picker.current(0)
            info.set("已找到旧版助手，正在准备自动升级（约 3 秒后开始）…")
            start_button.configure(state="normal")
            schedule_auto_begin()
        else:
            selection.set("未找到旧版助手")
            info.set("请先打开原来的千牛助手，再点重新检测；也可选择原安装文件夹。")
            start_button.configure(state="disabled")

    auto_state = {"timer": None, "countdown": 0}

    def cancel_auto_begin():
        if auto_state["timer"] is not None:
            try:
                window.after_cancel(auto_state["timer"])
            except Exception:  # noqa: BLE001
                pass
        auto_state["timer"] = None
        auto_state["countdown"] = 0

    def schedule_auto_begin():
        """小白模式：检测到旧版后自动开始，不需要点按钮（可取消）。"""
        if busy or not rows or auto_state["timer"] is not None:
            return
        auto_state["countdown"] = 3

        def tick():
            if busy or not rows:
                auto_state["timer"] = None
                return
            if auto_state["countdown"] <= 0:
                auto_state["timer"] = None
                begin()
                return
            info.set(f"已找到旧版助手，{auto_state['countdown']} 秒后自动开始升级（无需操作，可点取消）")
            auto_state["countdown"] -= 1
            auto_state["timer"] = window.after(1000, tick)

        auto_state["timer"] = window.after(1000, tick)

    def choose():
        path = filedialog.askdirectory(title="选择原来安装助手的文件夹")
        if path:
            refresh(path)

    def begin():
        nonlocal busy
        cancel_auto_begin()
        index = picker.current()
        if busy or index < 0:
            return
        root = Path(rows[index]["path"])
        busy = True
        for widget in (start_button,detect_button,browse_button,picker):
            widget.configure(state="disabled")
        bar.start(12)

        def worker():
            try:
                result = upgrade(root, lambda text: events.put(("progress",text)))
                events.put(("done",result))
            except Exception as error:
                events.put(("error",str(error)))
        threading.Thread(target=worker,name="upgrade-installer",daemon=False).start()

    start_button = ttk.Button(controls,text="立即升级",command=begin,state="disabled")
    start_button.pack(side="right")
    cancel_button = ttk.Button(controls,text="取消自动升级",command=cancel_auto_begin)
    cancel_button.pack(side="right",padx=8)
    detect_button = ttk.Button(controls,text="重新检测",command=refresh)
    detect_button.pack(side="left")
    browse_button = ttk.Button(controls,text="选择文件夹",command=choose)
    browse_button.pack(side="left",padx=8)

    def poll():
        nonlocal busy
        try:
            while True:
                kind,value = events.get_nowait()
                if kind == "progress":
                    info.set(value)
                else:
                    busy = False
                    bar.stop()
                    if kind == "done":
                        title.configure(text=f"升级完成 · {VERSION}")
                        info.set("助手已自动启动，原配置和聊天数据已保留。\n可以关闭此窗口，正常使用助手。")
                        start_button.configure(text="完成",command=window.destroy,state="normal")
                    else:
                        info.set(value)
                        for widget in (start_button,detect_button,browse_button):
                            widget.configure(state="normal")
                        picker.configure(state="readonly")
        except queue.Empty:
            pass
        window.after(150,poll)

    def close():
        if not busy:
            window.destroy()

    window.protocol("WM_DELETE_WINDOW",close)
    window.after(100,refresh)
    window.after(150,poll)
    window.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="read-only payload and installation detection check")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.check:
        expected = read_json(resource_root()/"payload-manifest.json")
        with tempfile.TemporaryDirectory(prefix="qn-check-") as directory:
            source = extract_payload(resource_root()/"payload.zip",Path(directory),expected)
            found = discover()
            for row in found:
                validate_runtime(Path(row["path"]),source)
        report = {"version": VERSION, "payload_verified": True, "installations": found, "read_only": True}
        if args.report:
            args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        elif sys.stdout is not None:
            print(json.dumps(report,ensure_ascii=False))
        return 0
    show_window()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
