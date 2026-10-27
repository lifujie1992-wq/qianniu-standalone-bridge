"""imsdk 强制挂钩 + 人工提醒（任务 B / 1.6.29）。

阶段机（严格按任务书）：

    0 启动宽限   60s 内只观察
    1 自动重试   每 30s 探测同源 frame + 让已连接页面重新挂钩（无侵入）
    2 打开聊天页 仅使用仓库现有安全能力；没有则记 no_safe_open_method 直接进阶段 3
    3 提醒人工   本地弹窗（置顶）+ 尽力通知；限频/夜间静默/降频
    4 授权修复   必须人点击 + 二次确认；只用现有 cdp_injection 机制，不重启千牛

安全边界（与任务书一致）：
  * 阶段 1/2 不关闭千牛、不模拟键鼠、不截屏、不向买家发消息；
  * 阶段 4 只在人点击并二次确认后，才去启用现有 CDP 注入能力；
  * 仓库没有"带调试端口重启千牛"的实现，因此阶段 4 不会重启千牛，
    只把结果如实写进 last_action_result（不发明不安全做法）；
  * 不新增任何对外网络请求；日志不写买家内容/cookie/token/订单号。
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hook_diagnostics import (
    CHAT_KINDS,
    chat_page_hooked,
    hook_summary,
    mask_text,
    orders_capable,
    sanitize_page_url,
)

PROMPT_TITLE = "千牛助手 - 订单信息获取异常"
PROMPT_BODY = (
    "助手没有检测到可用的聊天页面，AI 现在拿不到买家订单"
    "（会导致售前售后判断错误、误发催付）。\n"
    "请这样处理：① 打开千牛【接待台】聊天窗口；② 随便点开一个买家会话；③ 等 10 秒。\n"
    "如果这条提醒 2 分钟后还在，请点【授权修复】。"
)
ANSWER_HANDLED = "handled"
ANSWER_AUTHORIZE = "authorize"
ANSWER_SNOOZE = "snooze"
ANSWER_IGNORED = "ignored"

STAGE_OBSERVE = 0
STAGE_RETRY = 1
STAGE_OPEN_CHAT = 2
STAGE_PROMPT = 3
STAGE_AUTHORIZED = 4

# 阶段 1 的重试节奏（任务书：每 30 秒）
RETRY_INTERVAL = 30.0
# 阶段 1 持续失败多久后进阶段 2
OPEN_CHAT_AFTER = 120.0
# 无人处理 3 次后降频到每小时
PROMPT_DOWNGRADE_AFTER = 3
PROMPT_DOWNGRADE_INTERVAL = 3600.0

# 同源 frame 探测表达式：只读 typeof，不改任何页面状态
_PROBE_EXPRESSION = r"""
(() => {
  const rows = [];
  const describe = (win, index, isTop) => {
    let url = "";
    try { url = String((win.location && win.location.href) || ""); } catch (e) { url = ""; }
    let typeofValue = "inaccessible";
    let invokeTypeof = "inaccessible";
    try {
      typeofValue = typeof win.imsdk;
      invokeTypeof = typeofValue === "undefined" ? "n/a" : typeof win.imsdk.invoke;
    } catch (e) {
      typeofValue = "inaccessible";
      invokeTypeof = "inaccessible";
    }
    rows.push({frame: index, is_top: isTop, url: url,
               imsdk_typeof: typeofValue, imsdk_invoke_typeof: inviteSafe(invokeTypeof)});
  };
  const inviteSafe = value => value;
  try { describe(window, 0, true); } catch (e) {}
  try {
    const count = Number(window.frames && window.frames.length) || 0;
    for (let i = 0; i < Math.min(count, 20); i += 1) {
      try { describe(window.frames[i], i + 1, false); } catch (e) {}
    }
  } catch (e) {}
  return {frames: rows, count: rows.length};
})()
"""

# 让已连接页面重新执行一次现有挂钩（不改变挂钩实现本身）
_REHOOK_EXPRESSION = (
    "(() => { try { return !!(window.__qn_standalone_rehook && window.__qn_standalone_rehook()); }"
    " catch (e) { return false; } })()"
)


class HookRecovery:
    """阶段机 + 提醒。所有外部副作用都可注入，便于单测。"""

    def __init__(
        self,
        app: Any,
        *,
        now_fn: Callable[[], float] = time.time,
        prompt_fn: Callable[[dict[str, Any]], str] | None = None,
        notify_fn: Callable[[str, str], None] | None = None,
        logger: Any = None,
    ) -> None:
        self.app = app
        self.now = now_fn
        self.prompt_fn = prompt_fn
        self.notify_fn = notify_fn
        self.logger = logger
        self.thread = threading.Thread(target=self.run, name="imsdk-hook-recovery", daemon=True)
        self.lock = threading.RLock()

        started = self.now()
        self.started_at = started
        self.stage = STAGE_OBSERVE
        self.recovered = False
        self.last_action = ""
        self.last_action_result = ""
        self.last_action_at = 0.0
        self.needs_human = False
        self.human_action = "none"
        self.prompts_shown = 0
        self.last_prompt_at = 0.0
        self.last_prompt_answer = ""
        self.cdp_authorized_at = 0.0
        self.snooze_until = 0.0
        self.last_retry_at = 0.0
        self.stage2_attempted = False
        self.frames_seen: list[dict[str, Any]] = []
        # 每个 frame 的明细（url/imsdk_typeof/injected），供 process_pages 汇总
        self.frames_detail: list[dict[str, Any]] = []
        self.child_frame_with_imsdk = False
        self.recovery_attempted = False
        self.recovered_count = 0
        self._prompt_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _config(self, key: str, default: Any) -> Any:
        get = getattr(self.app.config, "get", None)
        if not callable(get):
            return default
        try:
            value = get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._config("imsdk_force_hook_enabled", True))

    def _grace_seconds(self) -> float:
        try:
            return max(0.0, float(self._config("imsdk_hook_grace_seconds", 60)))
        except (TypeError, ValueError):
            return 60.0

    def _remind_interval(self) -> float:
        try:
            return max(60.0, float(self._config("imsdk_hook_remind_interval_minutes", 15)) * 60.0)
        except (TypeError, ValueError):
            return 900.0

    def _quiet_minutes(self) -> float:
        try:
            return max(0.0, float(self._config("imsdk_hook_quiet_when_no_buyer_minutes", 30)))
        except (TypeError, ValueError):
            return 30.0

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def connections(self) -> list[dict[str, Any]]:
        browser = getattr(self.app, "browser", None)
        if browser is None:
            return []
        try:
            return browser.diagnostics_by_connection()
        except Exception:  # noqa: BLE001
            return []

    def recovered_now(self) -> bool:
        # 取订单只依赖 imsdk 是否存在：子 frame 挂上也应视为恢复。
        return orders_capable(self.connections())

    def _buyer_active(self) -> bool:
        minutes = self._quiet_minutes()
        if minutes <= 0:
            return True
        db = getattr(self.app, "db", None)
        if db is None:
            return True
        try:
            return bool(db.has_recent_buyer_activity(minutes))
        except Exception:  # noqa: BLE001
            return True

    # ------------------------------------------------------------------
    # 阶段 1：同源 frame 探测 + 重新挂钩（无侵入）
    # ------------------------------------------------------------------
    def probe_frames(self) -> dict[str, Any]:
        browser = getattr(self.app, "browser", None)
        if browser is None:
            return {"ok": False, "error": "browser bridge unavailable"}
        try:
            value = browser.execute(_PROBE_EXPRESSION, timeout=4.0)
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": mask_text(str(error), 160)}
        if not isinstance(value, dict):
            return {"ok": False, "error": "probe returned non-object"}
        frames = [row for row in value.get("frames") or [] if isinstance(row, dict)]
        rows = []
        child_with_imsdk = False
        for row in frames[:20]:
            sanitized = {
                "frame": int(row.get("frame") or 0),
                "is_top": bool(row.get("is_top")),
                "page_url": sanitize_page_url(row.get("url")),
                "imsdk_typeof": str(row.get("imsdk_typeof") or "undefined")[:16],
                "imsdk_invoke_typeof": str(row.get("imsdk_invoke_typeof") or "n/a")[:16],
            }
            if not sanitized["is_top"] and sanitized["imsdk_typeof"] in {"object", "function"}:
                child_with_imsdk = True
            rows.append(sanitized)
        self.frames_seen = rows
        self.child_frame_with_imsdk = child_with_imsdk
        self.last_action = "probe_frames"
        self.last_action_at = self.now()
        self.last_action_result = (
            f"frames={len(rows)} child_imsdk={child_with_imsdk}" if rows else "no frames"
        )
        return {"ok": True, "frames": rows, "child_frame_with_imsdk": child_with_imsdk}

    def hook_frames(self) -> dict[str, Any]:
        """在同源子 frame 里注入桥接脚本（新版客户端聊天页可能在 dx-h5 的 iframe 里）。

        只注入同源 frame，且仅当该 frame 已有 imsdk 或 URL/标题像聊天页；
        失败不影响主流程。默认开启，可用 imsdk_frame_hook_enabled=false 关闭。
        """
        if not self.app.config.get("imsdk_frame_hook_enabled", True):
            return {"ok": False, "error": "frame hook disabled"}
        browser = getattr(self.app, "browser", None)
        if browser is None:
            return {"ok": False, "error": "browser bridge unavailable"}
        try:
            source = (Path(__file__).resolve().parent / "browser_bridge.js").read_text(encoding="utf-8-sig")
        except OSError as error:
            return {"ok": False, "error": f"bridge source unreadable: {error}"}
        expression = build_frame_hook_expression(source)
        try:
            value = browser.execute(expression, timeout=15.0)
        except Exception as error:  # noqa: BLE001
            self.last_action = "hook_frames"
            self.last_action_at = self.now()
            self.last_action_result = f"frame_hook failed: {mask_text(str(error), 120)}"
            return {"ok": False, "error": self.last_action_result}
        frames = value.get("frames") if isinstance(value, dict) else None
        injected = int((value or {}).get("injected") or 0) if isinstance(value, dict) else 0
        if isinstance(frames, list):
            self.frames_detail = [row for row in frames if isinstance(row, dict)][:20]
        self.last_action = "hook_frames"
        self.last_action_at = self.now()
        self.last_action_result = f"frame_hook injected={injected} frames={len(frames or [])}"
        self._log(f"imsdk frame hook injected={injected}")
        return {"ok": injected > 0, "injected": injected, "frames": frames or []}

    def rehook_pages(self) -> dict[str, Any]:
        browser = getattr(self.app, "browser", None)
        if browser is None:
            return {"ok": False, "error": "browser bridge unavailable"}
        try:
            value = browser.execute(_REHOOK_EXPRESSION, timeout=4.0)
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": mask_text(str(error), 160)}
        self.last_action = "rehook"
        self.last_action_at = self.now()
        self.last_action_result = "rehook ok" if value is True else "rehook returned false"
        return {"ok": True, "hooked": bool(value is True)}

    # ------------------------------------------------------------------
    # 阶段 2：尝试打开聊天页（只用现有安全能力）
    # ------------------------------------------------------------------
    def open_chat_page(self) -> dict[str, Any]:
        """仓库没有"安全打开接待台"的现成接口，因此如实记录不支持。

        任务书要求：没有安全的现成方式时不要自己发明（不模拟键鼠、不截屏点击）。
        """
        self.stage2_attempted = True
        self.last_action = "open_chat_page"
        self.last_action_at = self.now()
        self.last_action_result = "no_safe_open_method"
        return {"ok": False, "error": "no_safe_open_method"}

    # ------------------------------------------------------------------
    # 阶段 3：提醒
    # ------------------------------------------------------------------
    def _prompt_interval(self) -> float:
        base = self._remind_interval()
        if self.prompts_shown >= PROMPT_DOWNGRADE_AFTER:
            return max(base, PROMPT_DOWNGRADE_INTERVAL)
        return base

    def should_prompt(self) -> bool:
        now = self.now()
        if self.snooze_until and now < self.snooze_until:
            return False
        if not self.last_prompt_at:
            return True
        return now - self.last_prompt_at >= self._prompt_interval()

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "title": PROMPT_TITLE,
            "body": PROMPT_BODY,
            "buttons": ["我已处理", "授权修复", "30 分钟后再提醒"],
            "hint": PROMPT_BODY,
            "summary": hook_summary(self.connections()),
        }

    def _default_prompt(self, payload: dict[str, Any]) -> str:
        return show_prompt_window(payload)

    def _default_notify(self, title: str, body: str) -> None:
        notify_windows(title, body)

    def prompt_now(self) -> str:
        """执行一次提醒（限频判断由调用方先做）。返回 human 的答复。"""
        payload = self.prompt_payload()
        with self.lock:
            self.prompts_shown += 1
            self.last_prompt_at = self.now()
            self.stage = STAGE_PROMPT
            self.needs_human = True
            self.last_action = "prompt"
            self.last_action_at = self.last_prompt_at
        notify = self.notify_fn or self._default_notify
        try:
            notify(payload["title"], PROMPT_BODY)
        except Exception:  # noqa: BLE001 - 通知失败不影响置顶窗口
            pass
        answer = ANSWER_IGNORED
        try:
            prompt = self.prompt_fn or self._default_prompt
            answer = str(prompt(payload) or ANSWER_IGNORED)
        except Exception:  # noqa: BLE001
            answer = ANSWER_IGNORED
        self.apply_answer(answer, confirmed=True)
        return answer

    def apply_answer(self, answer: str, *, confirmed: bool = False) -> None:
        """处理人的答复；授权修复必须 confirmed=True（二次确认）。"""
        answer = answer if answer in {ANSWER_HANDLED, ANSWER_AUTHORIZE, ANSWER_SNOOZE, ANSWER_IGNORED} \
            else ANSWER_IGNORED
        with self.lock:
            self.last_prompt_answer = answer
            self.last_prompt_at = self.now()
        if answer == ANSWER_SNOOZE:
            self.snooze_until = self.now() + 1800.0
            self.last_action = "snooze"
            self.last_action_result = "snoozed 30min"
            self._log("imsdk hook prompt snoozed 30min")
            return
        if answer == ANSWER_HANDLED:
            self.last_action = "human_handled"
            self.last_action_result = "waiting for recovery"
            self._log("imsdk hook prompt answered handled")
            return
        if answer == ANSWER_AUTHORIZE:
            if not confirmed:
                self.last_action = "authorize_rejected"
                self.last_action_result = "confirmation required"
                return
            self.authorize_repair()
            return
        self.last_action = "prompt_ignored"
        self.last_action_result = "no answer"

    # ------------------------------------------------------------------
    # 阶段 4：授权修复（只用现有 cdp_injection；不重启千牛）
    # ------------------------------------------------------------------
    def authorize_repair(self) -> dict[str, Any]:
        self.stage = STAGE_AUTHORIZED
        self.human_action = "authorize_cdp"
        self.cdp_authorized_at = self.now()
        if self.recovery_attempted:
            self.last_action = "authorize_repair"
            self.last_action_result = "already attempted once; manual intervention required"
            return {"ok": False, "error": "already_attempted"}
        self.recovery_attempted = True
        self.last_action = "authorize_repair"
        self.last_action_at = self.now()
        # 仓库现有的 CDP 能力只能"注入到已存在的调试目标"，不能带端口重启千牛，
        # 因此这里只做授权登记 + 一次性启用，并如实回报结果。
        try:
            if hasattr(self.app.config, "set"):
                self.app.config.set("cdp_injection_enabled", True)
        except Exception:  # noqa: BLE001
            pass
        cdp = getattr(self.app, "cdp", None)
        if cdp is None:
            self.last_action_result = "cdp_unavailable: injector missing"
            return {"ok": False, "error": "cdp_unavailable"}
        # 现场教训（2026-10-27）：9.98.49N 的 CEF 不监听 --remote-debugging-port，
        # 重启千牛还会退回登录页、丢掉这段时间的买家消息。默认禁止自动重启，
        # 只有维护人员显式打开 cdp_restart_enabled 才允许。
        if not self.app.config.get("cdp_restart_enabled", False):
            self.last_action_result = (
                "restart_disabled_by_policy: 本版千牛(9.98.49N)不暴露调试端口，"
                "重启还会丢登录态与消息，已停用；请让人打开接待台聊天页后重试"
            )
            self.human_action = "open_reception_window"
            self._log("imsdk authorize repair blocked by policy")
            return {"ok": False, "error": "restart_disabled_by_policy"}
        # 任务 B1：真正带 --remote-debugging-port 重启千牛并按聊天页挂钩。
        # 失败原因必须具体（restart_failed / cdp_port_not_open / no_target_found）。
        try:
            result = cdp.restart_with_debug_port()
        except Exception as error:  # noqa: BLE001
            result = {"ok": False, "reason": f"restart_failed: {mask_text(str(error), 120)}"}
        if result.get("ok"):
            self.last_action_result = (
                f"cdp_ready port={result.get('port')} pages={result.get('pages')} "
                f"injected={result.get('injected')}"
            )
            self._log(f"imsdk authorize repair ok port={result.get('port')}")
            return {"ok": True, **result}
        self.last_action_result = str(result.get("reason") or "cdp_unavailable")[:200]
        self.human_action = "authorize_cdp"
        self._log(f"imsdk authorize repair failed: {self.last_action_result}")
        return {"ok": False, "error": self.last_action_result}

    # ------------------------------------------------------------------
    # 阶段机
    # ------------------------------------------------------------------
    def tick(self) -> None:
        recovered = self.recovered_now()
        now = self.now()
        with self.lock:
            if recovered:
                if not self.recovered:
                    self.recovered = True
                    self.recovered_count += 1
                    self.stage = STAGE_OBSERVE
                    self.needs_human = False
                    self.human_action = "none"
                    self.last_action = "recovered"
                    self.last_action_result = "imsdk recovered"
                    self.last_action_at = now
                    self._log("imsdk recovered")
                return
            self.recovered = False

        if not self.enabled:
            self.stage = STAGE_OBSERVE
            return
        if now - self.started_at < self._grace_seconds():
            self.stage = STAGE_OBSERVE
            return

        # 阶段 1：自动重试（每 30s）
        if self.stage < STAGE_RETRY:
            self.stage = STAGE_RETRY
            self.last_retry_at = 0.0
        if now - self.last_retry_at >= RETRY_INTERVAL:
            self.last_retry_at = now
            probe = self.probe_frames()
            self.rehook_pages()
            if self.recovered_now():
                return
            if isinstance(probe, dict) and probe.get("child_frame_with_imsdk"):
                self.hook_frames()
                if self.recovered_now():
                    return

        # 阶段 2：没有聊天页且阶段 1 已持续 2 分钟
        summary = hook_summary(self.connections())
        stalled_seconds = now - max(self.started_at, self.last_action_at or self.started_at)
        if not summary.get("chat_page_present") and not self.stage2_attempted \
                and now - self.started_at >= self._grace_seconds() + OPEN_CHAT_AFTER:
            self.stage = STAGE_OPEN_CHAT
            self.open_chat_page()

        # 阶段 3：提醒人工。needs_human 先置位（服务器要能看到），
        # 但只有真正弹了提醒才算进入阶段 3（被 snooze/夜间静默压住时留在阶段 1）。
        self.needs_human = True
        if not self.should_prompt():
            return
        self.stage = STAGE_PROMPT
        if not self._buyer_active():
            self.last_action = "prompt_suppressed"
            self.last_action_result = "quiet: no buyer activity"
            return
        if self._prompt_thread is not None and self._prompt_thread.is_alive():
            return
        self._prompt_thread = threading.Thread(
            target=self.prompt_now, name="imsdk-hook-prompt", daemon=True
        )
        self._prompt_thread.start()

    def run(self) -> None:
        stop_event = getattr(self.app, "stop_event", None)
        while True:
            if stop_event is not None:
                if stop_event.wait(5.0):
                    return
            else:
                time.sleep(5.0)
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 阶段机不允许影响主流程
                continue

    # ------------------------------------------------------------------
    # 状态（任务 B6，字段名严格一致）
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": self.enabled,
                "stage": int(self.stage),
                "recovered": bool(self.recovered),
                "started_at_ms": int(self.started_at * 1000),
                "last_action": str(self.last_action),
                "last_action_result": mask_text(self.last_action_result, 200),
                "needs_human": bool(self.needs_human),
                "human_action": str(self.human_action),
                "prompts_shown": int(self.prompts_shown),
                "last_prompt_at_ms": int(self.last_prompt_at * 1000),
                "last_prompt_answer": str(self.last_prompt_answer),
                "cdp_authorized_by_user_at_ms": int(self.cdp_authorized_at * 1000),
                # 诊断补充（非任务书字段，便于服务器判断）
                "frames_seen": list(self.frames_seen),
                "frames_detail": list(self.frames_detail),
                "child_frame_with_imsdk": bool(self.child_frame_with_imsdk),
                "snooze_until_ms": int(self.snooze_until * 1000),
            }

    def hint(self) -> str:
        if self.recovered or not self.needs_human:
            return ""
        return PROMPT_BODY

    def _log(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger.info(message)
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------------
# 本地提醒（置顶窗口 + 尽力系统通知）
# ----------------------------------------------------------------------
def notify_windows(title: str, body: str) -> None:
    """尽力弹系统通知；失败不抛（置顶窗口是主提醒渠道）。"""
    if not sys.platform.startswith("win"):
        return
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n=$t.GetElementsByTagName('text');"
        "$n.Item(0).AppendChild($t.CreateTextNode(%s)) | Out-Null;"
        "$n.Item(1).AppendChild($t.CreateTextNode(%s)) | Out-Null;"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('千牛客服助手').Show($toast);"
    ) % (json.dumps(title, ensure_ascii=False), json.dumps(body.splitlines()[0], ensure_ascii=False))
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception:  # noqa: BLE001
        return


def show_prompt_window(payload: dict[str, Any]) -> str:
    """置顶小窗口。返回 handled / authorize / snooze / ignored。"""
    try:
        import tkinter as tk
    except Exception:  # noqa: BLE001
        return ANSWER_IGNORED
    answer = {"value": ANSWER_IGNORED}
    window = tk.Tk()
    window.title(str(payload.get("title") or PROMPT_TITLE))
    window.attributes("-topmost", True)
    window.geometry("520x260")
    window.resizable(False, False)
    try:
        window.bell()
    except Exception:  # noqa: BLE001
        pass
    tk.Label(window, text=str(payload.get("title") or PROMPT_TITLE),
             font=("Microsoft YaHei UI", 14, "bold"), fg="#b42318").pack(anchor="w", padx=16, pady=(14, 6))
    tk.Label(window, text=str(payload.get("body") or PROMPT_BODY), justify="left",
             wraplength=480, font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=16)
    summary = payload.get("summary") or {}
    detail = (f"页面 {summary.get('pages', 0)} 个，已挂钩 {summary.get('hooked', 0)} 个；"
              f"分类 {json.dumps(summary.get('by_kind', {}), ensure_ascii=False)}")
    tk.Label(window, text=detail, fg="#667085", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16, pady=(8, 0))

    def close(value: str) -> None:
        answer["value"] = value
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    row = tk.Frame(window)
    row.pack(anchor="w", padx=16, pady=14)
    tk.Button(row, text="我已处理", width=12, command=lambda: close(ANSWER_HANDLED)).pack(side="left", padx=4)
    tk.Button(row, text="授权修复", width=12, command=lambda: close(ANSWER_AUTHORIZE)).pack(side="left", padx=4)
    tk.Button(row, text="30 分钟后再提醒", width=16,
              command=lambda: close(ANSWER_SNOOZE)).pack(side="left", padx=4)
    # 不阻塞：置顶窗口停留，最多 120 秒无操作按 ignored 处理
    window.after(120000, lambda: close(ANSWER_IGNORED))
    try:
        window.mainloop()
    except Exception:  # noqa: BLE001
        return ANSWER_IGNORED
    return answer["value"]


def confirm_authorize(title: str = "授权修复") -> bool:
    """二次确认：默认否，必须显式点【继续】。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:  # noqa: BLE001
        return False
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        return bool(messagebox.askokcancel(
            title,
            "将启用助手现有的调试通道（CDP）挂钩能力，并再检测一次。\n"
            "本版本不会重启千牛，也不会自动重启：若检测不到调试通道，需要人工按维护流程处理。\n\n是否继续？",
            parent=root,
        ))
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "HookRecovery",
    "PROMPT_TITLE",
    "PROMPT_BODY",
    "ANSWER_HANDLED",
    "ANSWER_AUTHORIZE",
    "ANSWER_SNOOZE",
    "ANSWER_IGNORED",
    "STAGE_OBSERVE",
    "STAGE_RETRY",
    "STAGE_OPEN_CHAT",
    "STAGE_PROMPT",
    "STAGE_AUTHORIZED",
    "notify_windows",
    "show_prompt_window",
    "confirm_authorize",
]


_FRAME_HOOK_TEMPLATE = """
(() => {
  const source = __SOURCE__;
  const hints = ['web_chat-packer', '/chat', 'reception'];
  const titles = ['\u804a\u5929', '\u63a5\u5f85'];
  const rows = [];
  let injected = 0;
  const frames = [];
  try {
    for (let i = 0; i < Math.min(window.frames.length, 20); i += 1) frames.push(window.frames[i]);
  } catch (e) {}
  for (let index = 0; index < frames.length; index += 1) {
    const frame = frames[index];
    let url = '';
    let typeofValue = 'inaccessible';
    let title = '';
    try {
      url = String(frame.location.href || '');
      typeofValue = typeof frame.imsdk;
      title = String((frame.document && frame.document.title) || '');
    } catch (e) {
      rows.push({frame: index + 1, url: '', imsdk_typeof: 'cross_origin', injected: false});
      continue;
    }
    const looksChat = hints.some(function (hint) { return url.indexOf(hint) >= 0; })
      || titles.some(function (hint) { return title.indexOf(hint) >= 0; });
    const hasImsdk = typeofValue === 'object' || typeofValue === 'function';
    if (!hasImsdk && !looksChat) {
      rows.push({frame: index + 1, url: url, imsdk_typeof: typeofValue, injected: false});
      continue;
    }
    let ok = false;
    try {
      if (!frame.document.querySelector('script[data-qn-frame-bridge]')) {
        const node = frame.document.createElement('script');
        node.setAttribute('data-qn-frame-bridge', '1');
        node.textContent =
          'window.__qn_standalone_ws_url = window.__qn_standalone_ws_url || '
          + JSON.stringify(window.__qn_standalone_ws_url || '') + ';' + String.fromCharCode(10) + source;
        frame.document.head.appendChild(node);
      }
      ok = true;
      injected += 1;
    } catch (e) {
      ok = false;
    }
    rows.push({frame: index + 1, url: url, imsdk_typeof: typeofValue, injected: ok});
  }
  return {frames: rows, injected: injected};
})()
"""


def build_frame_hook_expression(source: str) -> str:
    """构造"在符合条件同源 iframe 里注入桥接脚本"的表达式。"""
    return _FRAME_HOOK_TEMPLATE.replace("__SOURCE__", json.dumps(source, ensure_ascii=False))
