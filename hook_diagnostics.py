"""imsdk 挂钩诊断（任务 A / 1.6.28）：只加字段，不改任何现有行为。

设计约束（来自任务书）：
  * 只采集"页面是否挂上 imsdk / 为什么没挂上"，不记录 cookie、token、聊天内容、
    买家昵称、订单号；
  * URL 只保留 协议+域名+路径，去掉 `?` 与 `#`，路径超 160 字符截断；
  * 错误文案里出现 URL 或 ≥6 位数字串时打码；
  * 现有字段名与含义一律不动，只新增。

对外可见路径：
  status.standalone_status.browser.connections[i]  ← 每个页面连接一条
  status.standalone_status.browser.hook_summary    ← 汇总
  status.standalone_status.context_enrich.{last_error_detail,unavailable_reason}
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from typing import Any

CHAT_KINDS = ("chat_recent", "chat_other")

PAGE_KINDS = ("chat_recent", "chat_other", "miniapp", "select_order", "other")

HOOK_ERROR_VALUES = (
    "imsdk_undefined", "invoke_missing", "timeout", "blocked",
)

NO_CHAT_PAGE_HINT = "未检测到聊天(接待台)页面，请打开接待台并点开一个买家会话"

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d{6,}")
# 只允许这些字符进入错误文案，避免顺手把内容带出来
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------
def sanitize_page_url(value: Any) -> str:
    """只保留 协议+域名+路径；去掉 query 与 fragment；超过 160 字符截断。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    path = parts.path or "/"
    if len(path) > 160:
        path = path[:160]
    return f"{parts.scheme}://{parts.netloc}{path}"


def mask_text(value: Any, limit: int = 200) -> str:
    """错误文案打码：URL → <url>，≥6 位数字串 → ***，控制字符剔除，超长截断。"""
    text = str(value or "")
    text = _CONTROL_RE.sub(" ", text)
    text = _URL_RE.sub("<url>", text)
    text = _DIGITS_RE.sub("***", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, int(limit))]


def mask_title(value: Any, limit: int = 60) -> str:
    """页面标题：不打码数字（标题通常是"千牛工作台"之类），只截断去控制字符。"""
    text = _CONTROL_RE.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, int(limit))]


# --------------------------------------------------------------------------
# 归类
# --------------------------------------------------------------------------
def page_kind(url: Any) -> str:
    """页面归类：chat_recent / chat_other / miniapp / select_order / other。"""
    path = ""
    try:
        path = urllib.parse.urlsplit(str(url or "")).path
    except ValueError:
        path = str(url or "")
    if "web_chat-packer/recent" in path:
        return "chat_recent"
    if "web_chat-packer" in path:
        return "chat_other"
    if "miniappNav" in path or "miniapp" in path.lower():
        return "miniapp"
    if "selectOrder" in path:
        return "select_order"
    return "other"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    return value is True or str(value).lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# 每个连接
# --------------------------------------------------------------------------
def normalize_connection(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """把页面侧上报的原始 diagnostics 归一成任务 A1 的字段集合。

    保留调用方合并进来的既有字段（不改旧字段名/含义），只补 A1 规定的新字段。
    """
    raw = diagnostics if isinstance(diagnostics, dict) else {}
    url = sanitize_page_url(raw.get("page_url"))
    kind = str(raw.get("page_kind") or "").strip()
    if kind not in PAGE_KINDS:
        kind = page_kind(raw.get("page_url") or url)
    imsdk_typeof = str(raw.get("imsdk_typeof") or "undefined").strip() or "undefined"
    invoke_typeof = str(raw.get("imsdk_invoke_typeof") or "").strip()
    if not invoke_typeof:
        invoke_typeof = "n/a" if imsdk_typeof in {"undefined", ""} else "undefined"
    first_hooked = _as_int(raw.get("first_hooked_at_ms"))
    output = dict(raw)
    output.update({
        "page_url": url,
        "page_title": mask_title(raw.get("page_title")),
        "page_kind": kind,
        "is_top_frame": _as_bool(raw.get("is_top_frame", True)),
        "ready_state": str(raw.get("ready_state") or "")[:16],
        "imsdk_typeof": imsdk_typeof,
        "imsdk_invoke_typeof": invoke_typeof,
        "hook_attempts": _as_int(raw.get("hook_attempts")),
        "last_hook_attempt_at_ms": _as_int(raw.get("last_hook_attempt_at_ms")),
        "last_hook_error": mask_text(raw.get("last_hook_error"), 200),
        "first_hooked_at_ms": first_hooked,
    })
    return output


def hook_summary(connections: list[dict[str, Any]]) -> dict[str, Any]:
    """按 page_kind 汇总：页面数、已挂钩数、是否存在聊天页。"""
    rows = [row for row in (connections or []) if isinstance(row, dict)]
    by_kind: dict[str, dict[str, int]] = {}
    hooked = 0
    chat_present = False
    for row in rows:
        kind = str(row.get("page_kind") or "other")
        bucket = by_kind.setdefault(kind, {"pages": 0, "hooked": 0})
        bucket["pages"] += 1
        if row.get("imsdk_hooked") is True:
            bucket["hooked"] += 1
            hooked += 1
        if kind in CHAT_KINDS:
            chat_present = True
    summary = {
        "pages": len(rows),
        "hooked": hooked,
        "by_kind": by_kind,
        "chat_page_present": chat_present,
    }
    if not chat_present:
        summary["no_chat_page_hint"] = NO_CHAT_PAGE_HINT
    return summary


def chat_page_hooked(connections: list[dict[str, Any]]) -> bool:
    """是否已有"聊天页且已挂钩"（任务 B 的恢复判定）。"""
    for row in connections or []:
        if not isinstance(row, dict):
            continue
        if row.get("imsdk_hooked") is True and str(row.get("page_kind") or "") in CHAT_KINDS:
            return True
    return False


def unavailable_reason(
    connections: list[dict[str, Any]],
    last_error: str = "",
    needs_human: bool = False,
) -> str:
    """取订单不可用的原因分类（context_enrich.unavailable_reason）。"""
    if needs_human:
        return "needs_human_action"
    rows = [row for row in (connections or []) if isinstance(row, dict)]
    if not rows:
        return "no_page_hooked"
    summary = hook_summary(rows)
    chat_kinds = [row for row in rows if str(row.get("page_kind") or "") in CHAT_KINDS]
    if not chat_kinds:
        return "chat_page_missing"
    if not any(row.get("imsdk_hooked") is True for row in chat_kinds):
        return "no_page_hooked"
    text = str(last_error or "").lower()
    if "imsdk" in text or "invoke" in text or "channel" in text:
        return "hooked_but_invoke_failed"
    return "hooked_but_invoke_failed" if last_error else ""


def reasons_counter(connections: list[dict[str, Any]]) -> dict[str, int]:
    """给 60 秒日志用的失败原因计数。"""
    counter: dict[str, int] = {}
    for row in connections or []:
        if not isinstance(row, dict) or row.get("imsdk_hooked") is True:
            continue
        reason = str(row.get("last_hook_error") or "unknown")
        key = reason.split(":", 1)[0].strip()[:40] or "unknown"
        counter[key] = counter.get(key, 0) + 1
    return counter


# --------------------------------------------------------------------------
# 日志监视（A4）
# --------------------------------------------------------------------------
class HookMonitor:
    """每 60 秒一行汇总日志；挂钩数从 >0 变 0（或相反）时立刻补一行。"""

    INTERVAL_SECONDS = 60.0

    def __init__(self, app: Any, logger: Any = None, interval: float | None = None) -> None:
        self.app = app
        self.logger = logger
        self.interval = float(interval or self.INTERVAL_SECONDS)
        self.thread = threading.Thread(target=self.run, name="imsdk-hook-monitor", daemon=True)
        self.last_hooked: int | None = None
        self.last_pages = 0
        self.transitions = 0

    # -- 快照 ------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        browser = getattr(self.app, "browser", None)
        if browser is None:
            return {"pages": 0, "hooked": 0, "by_kind": {}, "chat_page_present": False}
        rows = browser.diagnostics_by_connection()
        return hook_summary(rows), rows

    def _emit(self, summary: dict[str, Any], rows: list[dict[str, Any]], prefix: str) -> None:
        reasons = reasons_counter(rows)
        line = (
            f"{prefix}pages={summary.get('pages', 0)} hooked={summary.get('hooked', 0)} "
            f"by_kind={summary.get('by_kind', {})} reasons={reasons}"
        )
        if self.logger is not None:
            self.logger.info(line)
        else:
            print(line, flush=True)

    # -- 循环 ------------------------------------------------------------
    def tick(self) -> None:
        summary, rows = self.snapshot()
        hooked = int(summary.get("hooked") or 0)
        pages = int(summary.get("pages") or 0)
        if self.last_hooked is None:
            self._emit(summary, rows, "imsdk hook: ")
        else:
            if (self.last_hooked > 0 and hooked == 0) or (self.last_hooked == 0 and hooked > 0) \
                    or (self.last_pages > 0 and pages == 0):
                self.transitions += 1
                self._emit(summary, rows, "imsdk hook changed: ")
            else:
                self._emit(summary, rows, "imsdk hook: ")
        self.last_hooked = hooked
        self.last_pages = pages

    def run(self) -> None:
        stop_event = getattr(self.app, "stop_event", None)
        while True:
            if stop_event is not None:
                if stop_event.wait(self.interval):
                    return
            else:
                time.sleep(self.interval)
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 诊断线程不允许影响主流程
                continue


__all__ = [
    "CHAT_KINDS",
    "PAGE_KINDS",
    "HOOK_ERROR_VALUES",
    "NO_CHAT_PAGE_HINT",
    "sanitize_page_url",
    "mask_text",
    "mask_title",
    "page_kind",
    "normalize_connection",
    "hook_summary",
    "chat_page_hooked",
    "unavailable_reason",
    "reasons_counter",
    "HookMonitor",
]
