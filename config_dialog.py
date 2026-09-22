"""First-run configuration dialog for the Qianniu bridge package.

The local service tokens are generated automatically; the customer only needs
to fill in their brain settings (or skip brain and run local-only first).
"""

from __future__ import annotations

import json
import secrets
import string
import sys
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

from app_version import VERSION
from brain_endpoint import brain_server_url
from config_defaults import DEFAULT_WORKBENCH_PORT, apply_operational_defaults
from device_identity import (
    atomic_write_json,
    brain_workstation_token,
    set_brain_workstation_token,
)


PLACEHOLDER_VALUES = {
    "replace-with-a-random-token",
    "replace-with-a-different-random-token",
    "replace-with-a-workbench-token",
    "replace-with-your-local-agent-id",
    "replace-with-your-local-agent-token",
    "replace-with-your-device-id",
    "replace-with-your-brain-agent-id",
}

TOKEN_KEYS = (
    "api_token",
    "browser_token",
    "workbench_token",
    "gateway_agent_id",
    "gateway_agent_token",
    "brain_agent_id",
)


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def generate_token(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ensure_local_tokens(config: dict) -> dict:
    for key in TOKEN_KEYS:
        value = str(config.get(key) or "").strip()
        if not value or value in PLACEHOLDER_VALUES:
            config[key] = generate_token()
    return config


def build_default_config() -> dict:
    example = app_root() / "config.example.json"
    if example.is_file():
        try:
            config = json.loads(example.read_text(encoding="utf-8-sig"))
            apply_operational_defaults(config)
            return config
        except (OSError, ValueError):
            pass
    config = {
        "ws_host": "127.0.0.1",
        "ws_port": 42110,
        "api_host": "127.0.0.1",
        "api_port": 42111,
        "workbench_host": "127.0.0.1",
        "workbench_port": DEFAULT_WORKBENCH_PORT,
    }
    apply_operational_defaults(config)
    return config


def needs_setup(config: dict) -> bool:
    for key in TOKEN_KEYS:
        value = str(config.get(key) or "").strip()
        if not value or value in PLACEHOLDER_VALUES:
            return True
    return False


def save_config(config: dict, path: Path) -> None:
    atomic_write_json(path, config)


def validate_brain_settings(server_url: str, token: str) -> str:
    url = server_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "大脑地址需要是完整的 http:// 或 https:// 地址。"
    if parsed.username or parsed.password:
        return "大脑地址不能包含用户名或密码。"
    if not token.strip():
        return "请填写大脑工位令牌。"
    return ""


def test_brain_connection(config: dict, timeout: float = 5.0) -> None:
    server_url = str(config.get("brain_server_url") or "").strip().rstrip("/")
    token = brain_workstation_token(config)
    error = validate_brain_settings(server_url, token)
    if error:
        raise ValueError(error)
    body = json.dumps({
        "agent_id": str(config.get("brain_agent_id") or ""),
        "agent_name": str(config.get("brain_agent_name") or "千牛本机工位"),
        "version": VERSION,
        "build_hash": "setup-check",
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        server_url + "/api/bridge/v1/register",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Agent-Token": token,
            "X-Agent-Id": str(config.get("brain_agent_id") or ""),
            "X-Device-Id": str(config.get("device_id") or ""),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise RuntimeError(f"无法连接大脑：{error}") from error
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise RuntimeError(str(payload.get("error") or "大脑拒绝了连接"))


class ConfigDialog:
    def __init__(self, config: dict, path: Path):
        self.config = config
        self.path = path
        self.saved = False

        self.root = tk.Tk()
        self.root.title("千牛客服助手 · 配置")
        self.root.resizable(False, False)

        self.brain_url = tk.StringVar(master=self.root, value=brain_server_url())
        self.brain_token = tk.StringVar(
            master=self.root,
            value=brain_workstation_token(config)
        )
        self.brain_name = tk.StringVar(
            master=self.root,
            value=str(config.get("brain_agent_name") or "千牛本机工位")
        )
        self.show_token = tk.BooleanVar(master=self.root, value=False)
        self.connection_status = tk.StringVar(master=self.root, value="")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)

        ttk.Label(
            outer,
            text="大脑地址已随安装包固定，首次使用只需填写工位令牌，其余配置已自动完成。",
            wraplength=480,
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))

        brain = ttk.LabelFrame(outer, text="连接中心大脑", padding=12)
        brain.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        brain.columnconfigure(1, weight=1)

        fields = (
            ("大脑地址", self.brain_url),
            ("工位令牌", self.brain_token),
            ("工位名称", self.brain_name),
        )
        for row, (label, variable) in enumerate(fields):
            ttk.Label(brain, text=label).grid(row=row, column=0, sticky="e", padx=(0, 8), pady=4)
            entry = ttk.Entry(brain, textvariable=variable, width=42)
            if label == "大脑地址":
                self.brain_url_entry = entry
                entry.configure(state="readonly")
            if label == "工位令牌":
                self.token_entry = entry
                entry.configure(show="*")
            entry.grid(row=row, column=1, sticky="ew", pady=4)

        ttk.Checkbutton(
            brain,
            text="显示令牌",
            variable=self.show_token,
            command=lambda: self.token_entry.configure(show="" if self.show_token.get() else "*"),
        ).grid(row=3, column=1, sticky="w", pady=(2, 4))
        ttk.Label(brain, textvariable=self.connection_status).grid(
            row=4, column=1, sticky="w", pady=(2, 0)
        )

        actions = ttk.Frame(outer)
        actions.grid(row=2, column=0, sticky="e")
        ttk.Button(actions, text="测试连接", command=self._test_connection).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(actions, text="保存并启动", command=self._save_and_start).pack(side="left")

    def _collect_brain(self) -> None:
        self.config["brain_server_url"] = self.brain_url.get().strip()
        set_brain_workstation_token(self.config, self.brain_token.get())
        self.config["brain_agent_name"] = self.brain_name.get().strip() or "千牛本机工位"

    def _settings_error(self) -> str:
        return validate_brain_settings(
            str(self.config.get("brain_server_url") or ""),
            brain_workstation_token(self.config),
        )

    def _test_connection(self) -> None:
        self._collect_brain()
        error = self._settings_error()
        if error:
            messagebox.showwarning("千牛客服助手", error)
            return
        self.connection_status.set("正在测试连接……")
        self.root.update_idletasks()
        try:
            test_brain_connection(self.config)
        except (ValueError, RuntimeError) as error:
            self.connection_status.set("连接失败")
            messagebox.showerror("千牛客服助手", str(error))
            return
        self.connection_status.set("连接成功")
        messagebox.showinfo("千牛客服助手", "大脑连接成功，工位令牌有效。")

    def _save_and_start(self) -> None:
        self._collect_brain()
        error = self._settings_error()
        if error:
            messagebox.showwarning("千牛客服助手", error)
            return
        self.config["brain_enabled"] = True
        save_config(self.config, self.path)
        self.saved = True
        self.root.destroy()

    def _on_close(self) -> None:
        self.root.destroy()

    def run(self) -> bool:
        self.root.update_idletasks()
        width = 540
        height = 335
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.mainloop()
        return self.saved


def show_and_save(config: dict, path: Path) -> dict | None:
    ensure_local_tokens(config)
    dialog = ConfigDialog(config, path)
    if dialog.run():
        return config
    return None
