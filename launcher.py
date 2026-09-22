"""One-click start/stop/status orchestration for the Qianniu bridge package.

Everything is kept local to the executable folder. The launcher:
  1. validates and lightly repairs config.json,
  2. injects browser_bridge.js into every runtime webui.zip,
  3. starts the bridge (and the dock, when enabled) as hidden background jobs,
  4. waits for the local status endpoint and reports success or failure.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

import psutil

import client_updater
import config_dialog
from config_defaults import apply_operational_defaults
from device_identity import (
    DeviceIdentityError,
    atomic_write_json,
    brain_workstation_token,
    prepare_device_identity,
)


CHAT_ENTRY = "web_chat-packer/recent.html"
INJECTION_TAG = "data-qn-standalone-bridge"
INJECTION_RE = re.compile(
    r'<script\b[^>]*\bdata-qn-standalone-bridge\s*=\s*["\'][^"\']*["\'][^>]*>'
    r"[\s\S]*?</script>",
    re.IGNORECASE,
)

MB_OK = 0x0
MB_YESNO = 0x4
MB_ICONINFORMATION = 0x40
MB_ICONWARNING = 0x30
MB_ICONERROR = 0x10
IDYES = 6
PASSIVE_UPGRADE_MARKER = "passive-bridge-v5.installed"
QIANNIU_PROCESS_NAMES = {"aliworkbench.exe", "alirender.exe", "qianniuagent.exe"}


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def message_box(title: str, text: str, flags: int = MB_ICONINFORMATION) -> int:
    try:
        return int(ctypes.windll.user32.MessageBoxW(0, str(text), str(title), MB_OK | flags))
    except Exception:
        return 0


def load_config() -> tuple[dict, Path] | None:
    path = app_root() / "config.json"
    if not path.is_file():
        message_box(
            "千牛客服助手",
            f"未找到配置文件：\n{path}\n\n请确认安装包解压完整后重试。",
            MB_ICONERROR,
        )
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        message_box(
            "千牛客服助手",
            f"配置文件损坏，无法读取：\n{path}\n\n{error}",
            MB_ICONERROR,
        )
        return None
    return data, path


def load_config_silent(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_config(data: dict, path: Path) -> None:
    atomic_write_json(path, data)


def ensure_device_identity(config: dict, config_path: Path) -> bool:
    try:
        prepare_device_identity(config, config_path)
        return True
    except DeviceIdentityError as error:
        message_box(
            "千牛客服助手 · 设备身份迁移失败",
            f"{error}\n\n本地仍保留旧 device_id，尚未连接或注册大脑。",
            MB_ICONERROR,
        )
        return False


def find_edge() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def ensure_edge(config: dict, config_path: Path) -> bool:
    edge_path = Path(str(config.get("dock_edge_path") or ""))
    if edge_path.is_file():
        return True
    found = find_edge()
    if found:
        config["dock_edge_path"] = str(found)
        save_config(config, config_path)
        return True
    config["dock_enabled"] = False
    save_config(config, config_path)
    message_box(
        "千牛客服助手",
        "未检测到 Microsoft Edge，浮层功能不可用。\n请安装 Edge 后重试。",
        MB_ICONWARNING,
    )
    return False


def build_injection(config: dict, bridge_source: str) -> str:
    host = str(config.get("ws_host") or "")
    port = int(config.get("ws_port") or 0)
    token = str(config.get("browser_token") or "")
    if host != "127.0.0.1" or not 1 <= port <= 65535 or not token:
        raise ValueError("browser bridge requires a loopback host, valid port, and token")
    query = urllib.parse.urlencode({"token": token})
    ws_url = f"ws://{host}:{port}/?{query}"
    # Message-source switches for the in-page bridge. history_poll stays off by
    # default because GetNewMsg/PeekNewMsg advance Qianniu's own message cursor.
    options = {
        "invoke_observer": bool(config.get("bridge_invoke_observer", True)),
        "ws_mirror": bool(config.get("bridge_ws_mirror", True)),
        "history_poll": bool(config.get("bridge_history_poll", False)),
        "discovery_poll": bool(config.get("bridge_discovery_poll", True)),
    }
    return (
        f'<script {INJECTION_TAG}="v1">\n'
        f"window.__qn_standalone_ws_url={json.dumps(ws_url)};\n"
        f"window.__qn_standalone_options={json.dumps(options, ensure_ascii=False)};\n"
        f"{bridge_source.rstrip()}\n"
        "</script>"
    )


def inject_zip(path: Path, injection: str) -> bool:
    with zipfile.ZipFile(path, "r") as source:
        original = source.read(CHAT_ENTRY).decode("utf-8")
        if original.count(INJECTION_TAG) == 1 and injection in original:
            return False
        cleaned = INJECTION_RE.sub("", original)
        if "</body>" not in cleaned.lower():
            raise RuntimeError(f"chat entry has no body end tag: {path}")
        html = re.sub(
            r"</body>",
            lambda _match: injection + "\n</body>",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        )
        if html.count(INJECTION_TAG) != 1:
            raise RuntimeError(f"standalone injection count is invalid: {path}")
        if html == original:
            return False

        with tempfile.NamedTemporaryFile(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(temporary_path, "w") as target:
                for item in source.infolist():
                    data = source.read(item.filename)
                    if item.filename.replace("\\", "/") == CHAT_ENTRY:
                        data = html.encode("utf-8")
                    target.writestr(item, data)
            source.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return True


def running_qianniu_processes() -> list[str]:
    found = set()
    for process in psutil.process_iter(["name"]):
        try:
            if process.pid == os.getpid():
                continue
            name = str(process.info.get("name") or "").lower()
            if name in QIANNIU_PROCESS_NAMES:
                found.add(name)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return sorted(found)


def inject_all_webui(config: dict) -> None:
    runtime_dir = app_root() / "runtime"
    bridge_path = app_root() / "browser_bridge.js"
    if not bridge_path.is_file():
        raise FileNotFoundError(f"missing bridge source: {bridge_path}")
    bridge_source = bridge_path.read_text(encoding="utf-8-sig")
    injection = build_injection(config, bridge_source)
    archives = sorted(runtime_dir.rglob("webui.zip"))
    if not archives:
        raise FileNotFoundError(f"no webui.zip found under {runtime_dir}")
    for archive in archives:
        inject_zip(archive, injection)


def status_url(config: dict) -> str:
    return f"http://127.0.0.1:{int(config.get('api_port', 42111))}/api/v1/status"


def fetch_status(config: dict, timeout: float = 2.0) -> dict | None:
    request = urllib.request.Request(status_url(config), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return None


def workbench_url(config: dict) -> str:
    from config_defaults import DEFAULT_WORKBENCH_PORT

    return f"http://127.0.0.1:{int(config.get('workbench_port', DEFAULT_WORKBENCH_PORT))}/"


def sync_running_brain_config(config: dict, timeout: float = 3.0) -> dict:
    token = str(config.get("workbench_token") or "").strip()
    if not token:
        raise RuntimeError("running workbench token is missing")
    body = json.dumps({
        "enabled": bool(config.get("brain_enabled", False)),
        "server_url": str(config.get("brain_server_url") or "").strip(),
        "agent_token": brain_workstation_token(config),
        "agent_id": str(config.get("brain_agent_id") or "").strip(),
        "agent_name": str(config.get("brain_agent_name") or "").strip(),
        "ai_reply_enabled": bool(config.get("brain_ai_reply_enabled", True)),
        "remote_open_chat_enabled": bool(
            config.get("brain_remote_open_chat_enabled", True)
        ),
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        urllib.parse.urljoin(workbench_url(config), "api/v1/brain/config"),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Workbench-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"running bridge rejected brain config: HTTP {error.code} {detail}") from error
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise RuntimeError(f"could not update the running bridge: {error}") from error
    if response.status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError(str(payload.get("error") or "running bridge did not accept brain config"))
    return payload


def exe_command(role: str, config_path: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(sys.executable), "--role", role, "--config", str(config_path)]
    entry = Path(__file__).resolve().parent / "qianniu_app.py"
    return [sys.executable, str(entry), "--role", role, "--config", str(config_path)]


def start_process(role: str, config_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        exe_command(role, config_path),
        cwd=str(app_root()),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def write_pid(name: str, pid: int) -> None:
    state_dir = app_root() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / name).write_text(str(pid), encoding="ascii")


def kill_tree(pid: int) -> bool:
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    children = []
    try:
        children = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    for child in reversed(children):
        try:
            child.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        root.terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    _gone, alive = psutil.wait_procs(children + [root], timeout=3)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if alive:
        _gone, alive = psutil.wait_procs(alive, timeout=3)
    return not alive


def request_bridge_shutdown(root: Path) -> bool:
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
        token = str(config.get("api_token") or "")
        port = int(config.get("api_port", 42111))
        if not token:
            return False
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/shutdown",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Token": token,
            },
        )
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status == 202
    except (OSError, ValueError, urllib.error.URLError):
        return False


def wait_for_exit(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return not psutil.pid_exists(pid)


def _path_is_within(path: str | Path, parent: Path) -> bool:
    try:
        candidate = os.path.normcase(str(Path(path).resolve()))
        boundary = os.path.normcase(str(parent.resolve()))
        return os.path.commonpath([candidate, boundary]) == boundary
    except (OSError, ValueError):
        return False


def stop_managed_qianniu(root: Path | None = None) -> list[int]:
    """Stop only Qianniu processes shipped inside this installation."""
    runtime_root = (root or app_root()) / "runtime"
    managed: dict[int, psutil.Process] = {}
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            if process.pid != os.getpid() and _path_is_within(process.info.get("exe") or "", runtime_root):
                managed[process.pid] = process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not managed:
        return []
    roots = []
    for pid, process in managed.items():
        try:
            if process.ppid() not in managed:
                roots.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            roots.append(pid)
    failed = []
    for pid in roots:
        if not kill_tree(pid):
            failed.append(pid)
    return failed


def request_tray_exit(root: Path | None = None) -> bool:
    root = root or app_root()
    state_dir = root / "state"
    pid_file = state_dir / "tray.pid"
    if not pid_file.is_file():
        return True
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return True
    if pid <= 0 or pid == os.getpid() or not psutil.pid_exists(pid):
        pid_file.unlink(missing_ok=True)
        return True
    try:
        process = psutil.Process(pid)
        executable = process.exe()
        command_line = " ".join(process.cmdline())
        owned = _path_is_within(executable, root) or (
            os.path.normcase(str(root.resolve())) in os.path.normcase(command_line)
            and "qianniu_app.py" in command_line.lower()
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        owned = False
    if not owned:
        pid_file.unlink(missing_ok=True)
        return True
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tray.exit").write_text("exit\\n", encoding="ascii")
    if wait_for_exit(pid, timeout=5.0):
        pid_file.unlink(missing_ok=True)
        return True
    exited = kill_tree(pid)
    if exited:
        pid_file.unlink(missing_ok=True)
    return exited


def stop_all(*, include_tray: bool = True, include_qianniu: bool = True) -> None:
    root = app_root()
    request_bridge_shutdown(root)
    try:
        import docked_workbench

        docked_workbench.close_existing()
    except Exception:
        pass
    stopped = 0
    failed: list[int] = []
    for name in ("docked_workbench.pid", "standalone_bridge.pid"):
        pid_file = root / "state" / name
        if not pid_file.is_file():
            continue
        try:
            pid = int(pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)
            continue
        if pid > 0:
            exited = wait_for_exit(pid) or kill_tree(pid)
            if exited:
                stopped += 1
                pid_file.unlink(missing_ok=True)
            else:
                failed.append(pid)
        else:
            pid_file.unlink(missing_ok=True)
    if include_qianniu:
        failed.extend(stop_managed_qianniu(root))
    if include_tray and not request_tray_exit(root):
        try:
            failed.append(int((root / "state" / "tray.pid").read_text(encoding="ascii").strip()))
        except (OSError, ValueError):
            pass
    if not failed:
        return
    if failed:
        message_box(
            "千牛客服助手",
            "以下后台进程未能退出，请以管理员身份重试：\n" + ", ".join(map(str, failed)),
            MB_ICONERROR,
        )
    elif stopped:
        message_box("千牛客服助手", "已停止千牛桥接和吸附窗。")
    else:
        message_box("千牛客服助手", "当前没有正在运行的桥接进程。")


def start_all() -> int:
    config_path = app_root() / "config.json"
    config = load_config_silent(config_path)
    had_config = config is not None
    if config is None:
        config = config_dialog.build_default_config()
    defaults_changed = apply_operational_defaults(config)
    if defaults_changed and had_config:
        save_config(config, config_path)
    if not ensure_device_identity(config, config_path):
        return 6
    if config_dialog.needs_setup(config):
        config = config_dialog.show_and_save(config, config_path)
        if config is None:
            return 2

    if config.get("auto_update_check_enabled", True) and offer_update(config, quiet=True):
        return 0

    state_dir = app_root() / "state"
    passive_upgrade_marker = state_dir / PASSIVE_UPGRADE_MARKER
    current_status = fetch_status(config, timeout=0.6)
    if not passive_upgrade_marker.is_file():
        running = running_qianniu_processes()
        if current_status or running:
            message_box(
                "千牛客服助手 · 安全升级",
                "检测到旧客服助手或千牛仍在运行。\n\n"
                "请先运行“停止客服助手”，再完全退出千牛（包括托盘进程），\n"
                "然后重新启动客服助手。这样才能清除旧页面中的消息轮询。",
                MB_ICONWARNING,
            )
            return 5

    if current_status:
        message_box("千牛客服助手", "桥接服务已在运行，正在打开工作台。")
        if not config.get("dock_enabled", False):
            webbrowser.open(workbench_url(config))
        return 0

    if config.get("dock_enabled", False):
        ensure_edge(config, config_path)

    try:
        inject_all_webui(config)
    except Exception as error:
        message_box(
            "千牛客服助手",
            f"浏览器桥注入失败：\n{error}\n\n"
            "请关闭千牛工作台后重试。",
            MB_ICONERROR,
        )
        return 4

    state_dir.mkdir(parents=True, exist_ok=True)
    passive_upgrade_marker.write_text("qn-standalone-browser-v5-passive\n", encoding="ascii")

    bridge = None
    dock = None

    def cleanup_started() -> None:
        for process, pid_name in (
            (dock, "docked_workbench.pid"),
            (bridge, "standalone_bridge.pid"),
        ):
            if process is None:
                continue
            if process.poll() is not None or kill_tree(process.pid):
                (state_dir / pid_name).unlink(missing_ok=True)

    try:
        bridge = start_process("bridge", config_path)
        write_pid("standalone_bridge.pid", bridge.pid)
        if config.get("dock_enabled", False):
            dock = start_process("dock", config_path)
            write_pid("docked_workbench.pid", dock.pid)
    except Exception as error:
        cleanup_started()
        message_box(
            "千牛客服助手",
            f"启动后台进程失败，已清理本次启动内容：\n{error}",
            MB_ICONERROR,
        )
        return 5

    deadline = time.time() + 25.0
    status = None
    while time.time() < deadline:
        status = fetch_status(config, timeout=1.0)
        if status and bool(status.get("ok")):
            break
        time.sleep(1.0)

    if not status or not bool(status.get("ok")):
        cleanup_started()
        message_box(
            "千牛客服助手",
            "启动未就绪，已清理本次启动的后台进程。\n"
            "请检查以下日志并联系交付方：\n"
            f"{app_root() / 'state' / 'standalone_bridge.log'}",
            MB_ICONERROR,
        )
        return 5

    if config.get("dock_enabled", False):
        message_box(
            "千牛客服助手",
            "启动成功。吸附窗将自动停靠在千牛旁边。\n\n"
            f"工作台地址：{workbench_url(config)}",
        )
    else:
        message_box(
            "千牛客服助手",
            "启动成功，正在打开工作台。\n\n"
            f"工作台地址：{workbench_url(config)}",
        )
        webbrowser.open(workbench_url(config))
    return 0


def show_status() -> int:
    loaded = load_config()
    if loaded is None:
        return 2
    config, _ = loaded
    status = fetch_status(config, timeout=2.0)
    if not status:
        message_box("千牛客服助手", "桥接服务未运行。", MB_ICONWARNING)
        return 1
    browser = status.get("browser") or {}
    appbiz = status.get("appbiz_send") or {}
    brain = status.get("brain") or {}
    lines = [
        f"客户端版本: {status.get('version')}",
        f"运行状态: {'正常' if status.get('ok') else '异常'}",
        f"浏览器连接: {browser.get('connected')}",
        f"发送适配器就绪: {appbiz.get('ready')}",
        f"大脑在线: {brain.get('state')}",
    ]
    message_box("千牛客服助手状态", "\n".join(lines))
    return 0


def start_dock() -> int:
    loaded = load_config()
    if loaded is None:
        return 2
    config, config_path = loaded

    if not fetch_status(config, timeout=1.0):
        message_box(
            "千牛客服助手",
            "桥接服务未运行。请先双击“启动千牛客服助手.cmd”，再启动浮层。",
            MB_ICONWARNING,
        )
        return 1

    if not ensure_edge(config, config_path):
        return 4

    config["dock_enabled"] = True
    save_config(config, config_path)

    dock_pid_file = app_root() / "state" / "docked_workbench.pid"
    if dock_pid_file.is_file():
        try:
            old_pid = int(dock_pid_file.read_text(encoding="ascii").strip())
            if psutil.pid_exists(old_pid):
                message_box("千牛客服助手", "浮层已在运行。")
                return 0
        except (OSError, ValueError):
            pass

    dock = start_process("dock", config_path)
    write_pid("docked_workbench.pid", dock.pid)
    message_box(
        "千牛客服助手",
        "浮层已启动。请打开千牛“接待中心 / 千牛工作台”，浮层会自动贴到旁边。",
    )
    return 0


def configure() -> int:
    config_path = app_root() / "config.json"
    config = load_config_silent(config_path) or config_dialog.build_default_config()
    if apply_operational_defaults(config) and config_path.is_file():
        save_config(config, config_path)
    if not ensure_device_identity(config, config_path):
        return 6
    config = config_dialog.show_and_save(config, config_path)
    if config is None:
        return 2
    if fetch_status(config, timeout=0.6):
        try:
            sync_running_brain_config(config)
        except RuntimeError as error:
            message_box(
                "千牛客服助手 · 大脑配置未生效",
                f"配置已保存，但运行中的助手未能立即加载：\n{error}\n\n请停止助手后重新启动。",
                MB_ICONERROR,
            )
            return 7
        return 0
    return start_all()


def offer_update(config: dict, quiet: bool = False) -> bool:
    try:
        release = client_updater.fetch_update_manifest(
            config, timeout=3.0 if quiet else 8.0
        )
    except (ValueError, RuntimeError) as error:
        if not quiet:
            message_box("千牛客服助手 · 在线升级", str(error), MB_ICONWARNING)
        return False
    if not client_updater.update_available(release):
        if not quiet:
            message_box("千牛客服助手 · 在线升级", "当前已是最新版本。")
        return False
    notes = str(release.get("notes") or "").strip()
    detail = (
        f"当前版本：{client_updater.VERSION}\n"
        f"最新版本：{release['version']}\n\n"
        "是否立即下载并安装？选择“否”将继续启动当前版本。"
    )
    if notes:
        detail += "\n\n更新说明：\n" + notes[:800]
    if message_box("千牛客服助手 · 发现新版本", detail, MB_YESNO | MB_ICONINFORMATION) != IDYES:
        return False
    try:
        installer = client_updater.download_installer(release)
    except RuntimeError as error:
        message_box("千牛客服助手 · 在线升级", str(error), MB_ICONERROR)
        return False
    stop_all()
    client_updater.launch_installer_after_exit(installer)
    return True


def check_for_updates() -> int:
    config = load_config_silent(app_root() / "config.json")
    if config is None:
        message_box("千牛客服助手 · 在线升级", "请先完成首次配置。", MB_ICONWARNING)
        return 2
    offer_update(config)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Qianniu bridge one-click launcher")
    parser.add_argument("--stop", action="store_true", help="stop bridge and dock")
    parser.add_argument("--status", action="store_true", help="show running status")
    parser.add_argument("--start-dock", action="store_true", help="start the dock only")
    parser.add_argument("--configure", action="store_true", help="open configuration dialog")
    parser.add_argument("--update", action="store_true", help="check for online updates")
    args = parser.parse_args()
    if args.stop:
        stop_all()
        return 0
    if args.status:
        return show_status()
    if args.start_dock:
        return start_dock()
    if args.configure:
        return configure()
    if args.update:
        return check_for_updates()
    return start_all()


if __name__ == "__main__":
    raise SystemExit(main())
