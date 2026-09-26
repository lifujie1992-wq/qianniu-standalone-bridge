from __future__ import annotations

import argparse
import ctypes
import hashlib
import html
import json
import logging
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import frida
import psutil
import websocket

from app_version import VERSION
from client_support import ClientSupportWatcher
from config_defaults import apply_operational_defaults
from device_identity import (
    DeviceIdentityError,
    atomic_write_json,
    brain_workstation_token,
    prepare_device_identity,
    set_brain_workstation_token,
)
from websockets.sync.server import serve



def _build_hash() -> str:
    """Fingerprint this build without embedding any auth material.

    In a source checkout this hashes the module source itself, so every code
    change naturally produces a distinct build hash. Frozen (PyInstaller)
    builds fall back to hashing packaged executable metadata.
    """
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except Exception:
        pass
    try:
        executable = Path(sys.executable).resolve()
        stat = executable.stat()
        return hashlib.sha256(
            f"{stat.st_mtime_ns}:{stat.st_size}:{VERSION}".encode("utf-8")
        ).hexdigest()[:12]
    except Exception:
        return "unknown"


BUILD_HASH = _build_hash()


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_root()
LOG = logging.getLogger("qianniu_standalone")
EMPTY_VALUES = (None, "", [], {})


def buyer_nick_is_placeholder(nick: str, buyer_id: str = "") -> bool:
    value = str(nick or "").strip()
    if not value:
        return True
    return value.isdigit()


def preferred_buyer_nick(
    payload: dict[str, Any], buyer_id: str, fallback: str = ""
) -> str:
    candidates = [str(payload.get("nickname") or "").strip(), str(fallback or "").strip()]
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    for message in reversed(messages):
        if isinstance(message, dict):
            candidates.append(str(message.get("nickname") or "").strip())
    for candidate in candidates:
        if candidate and not buyer_nick_is_placeholder(candidate, buyer_id):
            return candidate
    return next((candidate for candidate in candidates if candidate), "")

OUTBOUND_BLOCK_PATTERNS = (
    (re.compile(r"桥接|openbot|BridgeAgent|QianniuBridge|local_send", re.I), "含调试/桥接字样"),
    (re.compile(r"请忽略|测试发送|发送测试|速度测试|联调|压测|测试文案", re.I), "含测试字样"),
    (re.compile(r"【[^】]{0,12}测试[^】]{0,12}】", re.I), "含测试标记"),
    (re.compile(r"(发送)?测试|test\s*msg|debug\s*send", re.I), "含测试字样"),
    (re.compile(r"微信|v信|vx\s*[:：]?|加\s*v|外部联系|脱离平台", re.I), "疑似引导站外联系"),
    (
        re.compile(
            r"https?://(?!item\.taobao\.com|detail\.tmall\.com|tmall\.com|taobao\.com)[^\s]+",
            re.I,
        ),
        "含外部链接",
    ),
)

HANDOFF_BUYER_PATTERNS = (
    (re.compile(r"投诉|曝光|315|消协|12315|律师|报警", re.I), "投诉维权"),
    (re.compile(r"退款|退货|仅退款|换货|质量问题|假货", re.I), "售后纠纷"),
    (re.compile(r"改地址|修改地址|换地址", re.I), "改地址"),
    (re.compile(r"骗子|骗人|垃圾|服了|差评|报警", re.I), "激烈情绪"),
)

TAOBAO_SYSTEM_TPS_ASSET = re.compile(
    r"!!600000000\d+-\d+-tps-\d+-\d+\.(?:png|gif|jpe?g|webp)$",
    re.IGNORECASE,
)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def is_nonempty(value: Any) -> bool:
    return value not in EMPTY_VALUES


def outbound_safety_result(content: Any) -> dict[str, Any] | None:
    text = str(content or "").strip()
    if not text:
        return {
            "ok": False,
            "status": "blocked",
            "error": "empty content",
            "error_user": "发送内容为空",
            "real_send": False,
            "via": "safety",
        }
    if text.replace("?", "").strip() == "" and set(text) <= {"?"}:
        return {
            "ok": False,
            "status": "blocked",
            "error": "content is only question marks",
            "error_user": "发送内容异常（只有 ???），已拦截",
            "real_send": False,
            "via": "safety",
        }
    for pattern, reason in OUTBOUND_BLOCK_PATTERNS:
        if pattern.search(text):
            return {
                "ok": False,
                "status": "blocked",
                "error": f"safety blocked: {reason}",
                "error_user": f"发送已拦截（{reason}）。请改成正常客服话术，勿提测试/桥接/站外联系",
                "real_send": False,
                "via": "safety",
                "safety_reason": reason,
            }
    return None


def suggested_handoff_reason(event: dict[str, Any]) -> str:
    raw_type = str(event.get("raw_type") or "").strip().lower()
    if bool(event.get("has_video")) or raw_type == "video":
        return "买家发送视频"
    has_image = bool(event.get("has_image") or event.get("image")) or raw_type in {
        "image", "img", "pic",
    }
    content = str(event.get("content") or "").strip()
    if has_image and (not content or len(content) < 2):
        return "买家发送图片"
    for pattern, reason in HANDOFF_BUYER_PATTERNS:
        if pattern.search(content):
            return reason
    return ""


def brain_event_suppression_reason(event: dict[str, Any]) -> str:
    """Exclude platform chrome while preserving real buyer-uploaded images."""
    content = str(event.get("content") or "").strip()
    media_values = {
        str(event.get(key) or "").strip()
        for key in ("content", "image_url", "media_url")
        if str(event.get(key) or "").strip()
    }
    if not content.startswith(("http://", "https://")) or len(media_values) != 1:
        return ""
    try:
        parsed = urllib.parse.urlsplit(content)
    except ValueError:
        return ""
    hostname = str(parsed.hostname or "").lower()
    if (
        (hostname == "alicdn.com" or hostname.endswith(".alicdn.com"))
        and TAOBAO_SYSTEM_TPS_ASSET.search(parsed.path)
    ):
        return "taobao_system_tps_asset"
    return ""




def token_fingerprint(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return f"sha256:{digest[:10]}"


def classify_brain_http_error(status_code: int, detail: str, *, token: str = "") -> str:
    text = str(detail or "").strip()
    lowered = text.lower()
    suffix = f" ({token_fingerprint(token)})" if token else ""
    if status_code == 401:
        return f"大脑 HTTP 401：令牌无效或已失效{suffix}"
    if status_code == 403:
        if any(key in lowered for key in ("device binding", "binding mismatch", "device_mismatch", "machine", "device_id")):
            return "大脑 HTTP 403：设备绑定不匹配"
        if any(key in lowered for key in ("invalid token", "token invalid", "token expired", "bad token", "forbidden token", "signature")):
            return f"大脑 HTTP 403：令牌无效{suffix}"
        return "大脑 HTTP 403：服务端拒绝访问"
    if text:
        return f"大脑 HTTP {status_code}: {text[:400]}"
    return f"大脑 HTTP {status_code}"


def backoff_delay(attempts: int, minimum: float = 0.5, maximum: float = 15.0) -> float:
    attempts = max(0, int(attempts))
    return min(maximum, minimum * (2 ** attempts))

def canonical_event_id(event: dict[str, Any]) -> str:
    platform = str(event.get("platform") or "taobao").strip().lower()
    if platform in {"cntaobao", "qn", "qianniu"}:
        platform = "taobao"
    message_id = str(event.get("original_msg_id") or event.get("msg_id") or "").strip()
    if platform == "taobao" and message_id and not (event.get("incomplete") and not event.get("original_msg_id")):
        return f"qn-msg-v1|taobao|{message_id}"
    previous = str(event.get("event_id") or event.get("idempotency_key") or "").strip()
    if previous:
        return previous
    stable = [
        platform,
        str(event.get("account") or ""),
        str(event.get("buyer_id") or ""),
        str(event.get("role") or ""),
        message_id,
        str(event.get("original_timestamp") or event.get("ts") or ""),
        str(event.get("content") or ""),
    ]
    digest = hashlib.sha256("\x1f".join(stable).encode("utf-8")).hexdigest()
    return f"qn-capture-v1|{digest}"


def merge_events(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in incoming.items():
        if not is_nonempty(value):
            continue
        previous = merged.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            nested = dict(previous)
            for child_key, child_value in value.items():
                if is_nonempty(child_value):
                    nested[child_key] = child_value
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(event)
    normalized["platform"] = "taobao"
    normalized.setdefault("source", "qianniu_standalone")
    normalized.setdefault("captured_at_ms", int(time.time() * 1000))
    event_id = canonical_event_id(normalized)
    previous = str(normalized.get("event_id") or "").strip()
    if previous and previous != event_id:
        normalized.setdefault("capture_event_id", previous)
    normalized["event_id"] = event_id
    normalized["idempotency_key"] = event_id
    return normalized


def event_timestamp(event: dict[str, Any]) -> float:
    value = event.get("original_timestamp") or event.get("ts") or event.get("captured_at_ms") or 0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if result > 1e11:
        result /= 1000.0
    return result


def decode_nested(value: Any) -> Any:
    current = value
    for _ in range(3):
        if not isinstance(current, str):
            break
        text = current.strip()
        if not text or text[0] not in "[{":
            break
        try:
            current = json.loads(text)
        except ValueError:
            break
    return current


def walk_message_rows(value: Any, output: list[dict[str, Any]], depth: int = 0) -> None:
    value = decode_nested(value)
    if depth > 9:
        return
    if isinstance(value, list):
        for item in value[:200]:
            walk_message_rows(item, output, depth + 1)
        return
    if not isinstance(value, dict):
        return
    has_identity = any(key in value for key in ("mcode", "messageId", "msgid", "clientId"))
    has_body = any(key in value for key in ("originalData", "content", "summary", "text", "jsview"))
    if has_identity and has_body:
        output.append(value)
    for key in ("data", "result", "msgDetail", "message", "msgs", "messages", "list", "items", "msgList"):
        if key in value:
            walk_message_rows(value[key], output, depth + 1)


def party_value(value: Any, *keys: str) -> str:
    if not isinstance(value, dict):
        return ""
    for key in keys:
        if is_nonempty(value.get(key)):
            return str(value[key])
    return ""


def content_value(row: dict[str, Any]) -> str:
    candidates = [row.get("content"), row.get("originalData"), row.get("summary"), row.get("text")]
    for candidate in candidates:
        candidate = decode_nested(candidate)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if not isinstance(candidate, dict):
            continue
        for key in ("text", "content", "summary", "title"):
            if isinstance(candidate.get(key), str) and candidate[key].strip():
                return candidate[key].strip()
        jsview = candidate.get("jsview")
        if isinstance(jsview, list):
            for item in jsview:
                value = item.get("value") if isinstance(item, dict) else None
                if isinstance(value, dict) and isinstance(value.get("text"), str) and value["text"].strip():
                    return value["text"].strip()
    return ""


def normalize_native_frame(raw: str) -> list[dict[str, Any]]:
    try:
        frame = decode_nested(raw)
    except ValueError:
        return []
    rows: list[dict[str, Any]] = []
    walk_message_rows(frame, rows)
    output: list[dict[str, Any]] = []
    for row in rows:
        mcode = row.get("mcode") if isinstance(row.get("mcode"), dict) else {}
        message_id = str(
            row.get("messageId") or row.get("msgid") or mcode.get("messageId") or mcode.get("msgid") or ""
        ).strip()
        content = content_value(row)
        if not message_id or not content:
            continue
        from_id = row.get("fromid") or row.get("fromId") or {}
        to_id = row.get("toid") or row.get("toId") or {}
        from_target = party_value(from_id, "targetId", "userid", "uid")
        to_target = party_value(to_id, "targetId", "userid", "uid")
        from_nick = party_value(from_id, "nick", "display")
        to_nick = party_value(to_id, "nick", "display")
        login = row.get("loginid") or row.get("loginId") or {}
        seller_target = party_value(login, "targetId", "userid", "uid")
        seller_nick = party_value(login, "nick", "display")
        is_self = bool(row.get("isself")) or bool(seller_target and from_target == seller_target)
        buyer_id = to_target if is_self else from_target
        buyer_nick = to_nick if is_self else from_nick
        timestamp = row.get("sendTime") or row.get("sendtime") or row.get("timestamp") or time.time()
        try:
            timestamp_value = float(timestamp)
            if timestamp_value > 1e11:
                timestamp_value /= 1000.0
        except (TypeError, ValueError):
            timestamp_value = time.time()
        output.append(normalize_event({
            "platform": "taobao",
            "role": "assistant" if is_self else "user",
            "content": content,
            "account": seller_nick,
            "buyer_id": buyer_id,
            "buyer_nick": buyer_nick,
            "msg_id": message_id,
            "original_msg_id": message_id,
            "ts": timestamp_value,
            "original_timestamp": timestamp_value,
            "source": "qianniu_standalone_native",
            "capture_mode": "native_callback",
            "raw_type": str(row.get("msgtype") or row.get("templateId") or ""),
        }))
    return output


@dataclass
class Config:
    path: Path
    raw: dict[str, Any]
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if apply_operational_defaults(raw):
            atomic_write_json(path, raw)
        for key in (
            "ws_host", "api_host", "api_token", "browser_token", "gateway_url",
            "workbench_host", "workbench_port", "workbench_token",
        ):
            if not str(raw.get(key) or "").strip():
                raise ValueError(f"missing config value: {key}")
        if any(raw[key] != "127.0.0.1" for key in ("ws_host", "api_host", "workbench_host")):
            raise ValueError("standalone listeners must bind to 127.0.0.1")
        return cls(path.resolve(), raw)

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            return self.raw.get(key, default)

    def resolve(self, key: str) -> Path:
        with self.lock:
            value = Path(str(self.raw.get(key) or ""))
        if not value.is_absolute():
            value = self.path.parent / value
        return value.resolve()

    def public_brain_settings(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": bool(self.raw.get("brain_enabled", False)),
                "server_url": str(self.raw.get("brain_server_url") or ""),
                "agent_id": str(
                    self.raw.get("brain_agent_id")
                    or self.raw.get("gateway_agent_id")
                    or ""
                ),
                "agent_name": str(self.raw.get("brain_agent_name") or "千牛本机工位"),
                "token_configured": bool(brain_workstation_token(self.raw)),
                "ai_reply_enabled": bool(self.raw.get("brain_ai_reply_enabled", True)),
                "remote_open_chat_enabled": bool(
                    self.raw.get("brain_remote_open_chat_enabled", False)
                ),
                "handoff_ttl_seconds": float(
                    self.raw.get("brain_handoff_ttl_seconds", 3600.0)
                ),
                "heartbeat_seconds": float(self.raw.get("brain_heartbeat_seconds", 2.0)),
                "command_poll_seconds": float(self.raw.get("brain_command_poll_seconds", 1.5)),
                "event_delay_seconds": float(self.raw.get("brain_event_delay_seconds", 0.2)),
            }

    def update_brain_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        server_url = str(values.get("server_url") or "").strip().rstrip("/")
        if server_url:
            parsed = urllib.parse.urlsplit(server_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("大脑地址必须是完整的 http:// 或 https:// 地址")
            if parsed.username or parsed.password:
                raise ValueError("大脑地址不能包含用户名或密码")
        token = str(values.get("agent_token") or "").strip()
        enabled = bool(values.get("enabled", False))
        with self.lock:
            current_token = brain_workstation_token(self.raw)
            if enabled and (not server_url or not (token or current_token)):
                raise ValueError("启用大脑前必须填写大脑地址和大脑工位令牌")
            updates = {
                "brain_enabled": enabled,
                "brain_server_url": server_url,
                "brain_agent_id": str(values.get("agent_id") or "").strip()
                or str(self.raw.get("gateway_agent_id") or ""),
                "brain_agent_name": str(values.get("agent_name") or "").strip()
                or "千牛本机工位",
                "brain_ai_reply_enabled": bool(values.get("ai_reply_enabled", True)),
                "brain_remote_open_chat_enabled": bool(
                    values.get("remote_open_chat_enabled", False)
                ),
            }
            if token:
                set_brain_workstation_token(updates, token)
            updated_raw = dict(self.raw)
            updated_raw.update(updates)
            atomic_write_json(self.path, updated_raw)
            self.raw.clear()
            self.raw.update(updated_raw)
        return self.public_brain_settings()

    def set_brain_agent_id(self, agent_id: str) -> None:
        value = str(agent_id or "").strip()
        if not value:
            return
        with self.lock:
            if str(self.raw.get("brain_agent_id") or "") == value:
                return
            updated_raw = dict(self.raw)
            updated_raw["brain_agent_id"] = value
            atomic_write_json(self.path, updated_raw)
            self.raw.clear()
            self.raw.update(updated_raw)


RETENTION_SECONDS = 30 * 24 * 3600
WORKBENCH_EVENT_LIMIT = 20000
WORKBENCH_MESSAGE_LIMIT = 10000
WORKBENCH_SESSION_LIMIT = 500
WORKBENCH_SESSION_CACHE_SECONDS = 2.0


class StateDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._session_cache: list[dict[str, Any]] = []
        self._session_cache_at = 0.0
        self._initialize()
        self.prune_old_events()

    def _invalidate_session_cache(self) -> None:
        self._session_cache_at = 0.0
        self._session_cache = []

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def prune_old_events(self, retention_seconds: float = RETENTION_SECONDS) -> int:
        cutoff = time.time() - max(0.0, retention_seconds)
        with self.lock, self.connect() as connection:
            events_cursor = connection.execute(
                """
                DELETE FROM events
                WHERE status='delivered' AND delivered_at IS NOT NULL AND delivered_at < ?
                """,
                (cutoff,),
            )
            brain_cursor = connection.execute(
                """
                DELETE FROM brain_events
                WHERE status='delivered' AND delivered_at IS NOT NULL AND delivered_at < ?
                """,
                (cutoff,),
            )
            if events_cursor.rowcount:
                self._invalidate_session_cache()
        return int(events_cursor.rowcount or 0) + int(brain_cursor.rowcount or 0)

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    delivered_at REAL
                );
                CREATE INDEX IF NOT EXISTS events_status_updated ON events(status, updated_at);
                CREATE TABLE IF NOT EXISTS sends (
                    request_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    response TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS brain_events (
                    event_id TEXT PRIMARY KEY,
                    revision TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    queued_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    delivered_at REAL
                );
                CREATE INDEX IF NOT EXISTS brain_events_status_updated
                    ON brain_events(status, updated_at);
                CREATE TABLE IF NOT EXISTS brain_commands (
                    command_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    acknowledged_at REAL
                );
                CREATE INDEX IF NOT EXISTS brain_commands_status_updated
                    ON brain_commands(status, updated_at);
                CREATE TABLE IF NOT EXISTS session_controls (
                    account TEXT NOT NULL,
                    buyer_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(account,buyer_id)
                );
                CREATE INDEX IF NOT EXISTS session_controls_expiry
                    ON session_controls(mode,expires_at);
                UPDATE events SET status='pending' WHERE status='sending';
                UPDATE brain_events SET status='pending' WHERE status='sending';
                """
            )
            send_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(sends)").fetchall()
            }
            if "status" not in send_columns:
                connection.execute(
                    "ALTER TABLE sends ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
                )
            if "updated_at" not in send_columns:
                connection.execute("ALTER TABLE sends ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
                connection.execute("UPDATE sends SET updated_at=created_at WHERE updated_at=0")
            send_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(sends)").fetchall()
            }
            send_column_migrations = {
                "buyer_cid": "TEXT NOT NULL DEFAULT ''",
                "content": "TEXT NOT NULL DEFAULT ''",
                "confirmation_event_id": "TEXT NOT NULL DEFAULT ''",
                "submitted_at": "REAL NOT NULL DEFAULT 0",
                "confirmed_at": "REAL NOT NULL DEFAULT 0",
            }
            for name, declaration in send_column_migrations.items():
                if name not in send_columns:
                    connection.execute(f"ALTER TABLE sends ADD COLUMN {name} {declaration}")
            legacy_rows = connection.execute(
                "SELECT request_id,response FROM sends WHERE status='completed'"
            ).fetchall()
            for row in legacy_rows:
                try:
                    response = json.loads(row["response"])
                except (TypeError, ValueError):
                    response = {}
                response.update({
                    "ok": False,
                    "status": "unknown",
                    "confirmed": False,
                    "error": "legacy native return had no Qianniu delivery confirmation; do not retry automatically",
                })
                connection.execute(
                    "UPDATE sends SET status='unknown',response=?,updated_at=? WHERE request_id=?",
                    (json_text(response), time.time(), row["request_id"]),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS sends_match_status ON sends(status,buyer_cid,created_at)"
            )

    def upsert_event(self, event: dict[str, Any]) -> tuple[str, bool]:
        normalized = normalize_event(event)
        event_id = normalized["event_id"]
        now = time.time()
        with self.lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT payload,revision,status,created_at FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            merged = normalized
            if existing:
                if existing["status"] == "sending":
                    return event_id, False
                if existing["status"] == "delivered":
                    # A browser cache replay may carry a more authoritative
                    # loginid after an older bridge reversed seller/buyer.
                    # Repair only local identity fields and never requeue it.
                    if normalized.get("seller_identity_source") != "loginid":
                        return event_id, False
                    current = json.loads(existing["payload"])
                    repaired = dict(current)
                    for key in (
                        "account", "buyer_id", "buyer_nick", "role",
                        "seller_identity_source",
                    ):
                        if is_nonempty(normalized.get(key)):
                            repaired[key] = normalized[key]
                    repaired = normalize_event(repaired)
                    payload = json_text(repaired)
                    revision = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    if revision != existing["revision"]:
                        connection.execute(
                            "UPDATE events SET payload=?,revision=?,updated_at=? WHERE event_id=?",
                            (payload, revision, now, event_id),
                        )
                        self._invalidate_session_cache()
                    return event_id, False
                merged = merge_events(json.loads(existing["payload"]), normalized)
                merged = normalize_event(merged)
            payload = json_text(merged)
            revision = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            changed = not existing or revision != existing["revision"]
            if existing and not changed:
                return event_id, False
            created = now if not existing else existing["created_at"]
            connection.execute(
                """
                INSERT INTO events(event_id,payload,revision,status,attempts,last_error,created_at,updated_at,delivered_at)
                VALUES(?,?,?,'pending',0,'',?,?,NULL)
                ON CONFLICT(event_id) DO UPDATE SET
                    payload=excluded.payload,
                    revision=excluded.revision,
                    status='pending',
                    last_error='',
                    updated_at=excluded.updated_at,
                    delivered_at=NULL
                """,
                (event_id, payload, revision, created, now),
            )
            self._invalidate_session_cache()
            return event_id, True

    def upsert_local_projection(self, event: dict[str, Any]) -> tuple[str, bool]:
        """Persist a center-side draft for local display without any delivery queue."""
        normalized = normalize_event(event)
        event_id = normalized["event_id"]
        now = time.time()
        with self.lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT payload,revision,created_at FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            merged = normalized
            if existing:
                merged = merge_events(json.loads(existing["payload"]), normalized)
                merged = normalize_event(merged)
            payload = json_text(merged)
            revision = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if existing and revision == str(existing["revision"]):
                return event_id, False
            created = now if not existing else float(existing["created_at"])
            connection.execute(
                """
                INSERT INTO events(
                    event_id,payload,revision,status,attempts,last_error,
                    created_at,updated_at,delivered_at
                ) VALUES(?,?,?,'delivered',0,'',?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    payload=excluded.payload,
                    revision=excluded.revision,
                    status='delivered',
                    last_error='',
                    updated_at=excluded.updated_at,
                    delivered_at=excluded.delivered_at
                """,
                (event_id, payload, revision, created, now, now),
            )
            self._invalidate_session_cache()
            return event_id, True

    def claim_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT event_id,payload,revision FROM events WHERE status='pending' ORDER BY updated_at LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                placeholders = ",".join("?" for _ in rows)
                connection.execute(
                    f"UPDATE events SET status='sending' WHERE status='pending' AND event_id IN ({placeholders})",
                    tuple(row["event_id"] for row in rows),
                )
            return [
                {"event_id": row["event_id"], "payload": json.loads(row["payload"]), "revision": row["revision"]}
                for row in rows
            ]

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT event_id,payload,revision FROM events WHERE status='pending' ORDER BY updated_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"event_id": row["event_id"], "payload": json.loads(row["payload"]), "revision": row["revision"]}
            for row in rows
        ]

    def mark_delivered(self, event_id: str, revision: str) -> bool:
        with self.lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE events SET status='delivered', delivered_at=?, last_error=''
                WHERE event_id=? AND revision=? AND status='sending'
                """,
                (time.time(), event_id, revision),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._invalidate_session_cache()
            return changed

    def mark_failed(self, rows: list[dict[str, Any]], error: str) -> None:
        with self.lock, self.connect() as connection:
            for row in rows:
                connection.execute(
                    """
                    UPDATE events SET status='pending',attempts=attempts+1,last_error=?
                    WHERE event_id=? AND revision=? AND status='sending'
                    """,
                    (error[:500], row["event_id"], row["revision"]),
                )
            if rows:
                self._invalidate_session_cache()

    def counts(self) -> dict[str, int]:
        with self.lock, self.connect() as connection:
            rows = connection.execute("SELECT status,COUNT(*) AS count FROM events GROUP BY status").fetchall()
        result = {"pending": 0, "delivered": 0}
        result.update({row["status"]: int(row["count"]) for row in rows})
        return result

    def event_status(self, event_id: str) -> str:
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        return str(row["status"]) if row else ""

    def brain_suppressed_message_ids(self, limit: int = WORKBENCH_EVENT_LIMIT) -> set[str]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM events ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 100000)),),
            ).fetchall()
        message_ids: set[str] = set()
        for row in rows:
            try:
                event = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not (
                event.get("brain_suppressed")
                or brain_event_suppression_reason(event)
            ):
                continue
            for key in ("original_msg_id", "msg_id"):
                value = str(event.get(key) or "").strip()
                if value:
                    message_ids.add(value)
        return message_ids

    def brain_handoff_controls(self) -> dict[tuple[str, str], str]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT account,buyer_id,reason FROM session_controls
                WHERE mode='human' AND source='brain'
                """
            ).fetchall()
        return {
            (str(row["account"]), str(row["buyer_id"])): str(row["reason"])
            for row in rows
        }

    def workbench_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            now_mono = time.monotonic()
            if (
                self._session_cache_at
                and now_mono - self._session_cache_at < WORKBENCH_SESSION_CACHE_SECONDS
            ):
                return [dict(item) for item in self._session_cache]
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT payload,status FROM events ORDER BY created_at DESC LIMIT ?",
                    (WORKBENCH_EVENT_LIMIT,),
                ).fetchall()
                controls = connection.execute(
                    "SELECT account,buyer_id,mode,reason,source,updated_at,expires_at FROM session_controls"
                ).fetchall()
            parsed_rows: list[tuple[dict[str, Any], str]] = []
            suppressed_parent_ids: set[str] = set()
            for row in rows:
                try:
                    event = json.loads(row["payload"])
                except (TypeError, ValueError):
                    continue
                parsed_rows.append((event, str(row["status"])))
                if event.get("brain_suppressed") or brain_event_suppression_reason(event):
                    for key in ("original_msg_id", "msg_id"):
                        value = str(event.get(key) or "").strip()
                        if value:
                            suppressed_parent_ids.add(value)
            now = time.time()
            control_map = {
                (str(row["account"]), str(row["buyer_id"])): {
                    "ai_mode": str(row["mode"]),
                    "handoff_reason": str(row["reason"]),
                    "handoff_source": str(row["source"]),
                    "control_updated_at": float(row["updated_at"]),
                    "handoff_expires_at": float(row["expires_at"]),
                }
                for row in controls
                if str(row["mode"]) == "human"
                and (not float(row["expires_at"]) or float(row["expires_at"]) > now)
            }
            sessions: dict[tuple[str, str], dict[str, Any]] = {}
            for event, status in parsed_rows:
                account = str(event.get("account") or "").strip()
                buyer_id = str(event.get("buyer_id") or "").strip()
                content = str(event.get("content") or "")
                if not account or not buyer_id or not content:
                    continue
                if (
                    str(event.get("role") or "").lower() == "assistant_simulated"
                    and str(event.get("parent_msg_id") or "") in suppressed_parent_ids
                ):
                    continue
                key = (account, buyer_id)
                timestamp = event_timestamp(event)
                session = sessions.setdefault(key, {
                    "account": account,
                    "buyer_id": buyer_id,
                    "buyer_nick": str(event.get("buyer_nick") or buyer_id),
                    "_buyer_nick_ts": timestamp,
                    "last_message": "",
                    "last_ts": 0.0,
                    "message_count": 0,
                    "last_status": "",
                })
                session["message_count"] += 1
                event_nick = str(event.get("buyer_nick") or "").strip()
                current_nick = str(session.get("buyer_nick") or "").strip()
                event_nick_is_usable = not buyer_nick_is_placeholder(event_nick, buyer_id)
                current_nick_is_usable = not buyer_nick_is_placeholder(current_nick, buyer_id)
                if event_nick and (
                    (event_nick_is_usable and not current_nick_is_usable)
                    or (
                        event_nick_is_usable == current_nick_is_usable
                        and timestamp >= float(session.get("_buyer_nick_ts") or 0.0)
                    )
                ):
                    session["buyer_nick"] = event_nick
                    session["_buyer_nick_ts"] = timestamp
                if timestamp >= float(session["last_ts"]):
                    session["last_ts"] = timestamp
                    session["last_message"] = content
                    session["last_status"] = status
            for key, session in sessions.items():
                session.pop("_buyer_nick_ts", None)
                session.update(control_map.get(key, {
                    "ai_mode": "ai",
                    "handoff_reason": "",
                    "handoff_source": "",
                    "control_updated_at": 0.0,
                    "handoff_expires_at": 0.0,
                }))
            result = sorted(
                sessions.values(), key=lambda item: float(item["last_ts"]), reverse=True
            )[:WORKBENCH_SESSION_LIMIT]
            self._session_cache = [dict(item) for item in result]
            self._session_cache_at = time.monotonic()
            return [dict(item) for item in result]

    def set_session_control(
        self,
        account: str,
        buyer_id: str,
        mode: str,
        reason: str = "",
        source: str = "manual",
        ttl_seconds: float = 0.0,
    ) -> dict[str, Any]:
        account = str(account or "").strip()
        buyer_id = str(buyer_id or "").strip()
        mode = str(mode or "").strip().lower()
        if not account or not buyer_id:
            raise ValueError("account and buyer_id are required")
        if mode not in {"ai", "human"}:
            raise ValueError("mode must be ai or human")
        now = time.time()
        expires_at = now + max(60.0, float(ttl_seconds)) if mode == "human" and ttl_seconds else 0.0
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO session_controls(account,buyer_id,mode,reason,source,updated_at,expires_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(account,buyer_id) DO UPDATE SET
                    mode=excluded.mode,reason=excluded.reason,source=excluded.source,
                    updated_at=excluded.updated_at,expires_at=excluded.expires_at
                """,
                (account, buyer_id, mode, str(reason or "")[:200], str(source or "")[:40], now, expires_at),
            )
            self._invalidate_session_cache()
        return {
            "account": account,
            "buyer_id": buyer_id,
            "ai_mode": mode,
            "handoff_reason": str(reason or "")[:200] if mode == "human" else "",
            "handoff_source": str(source or "")[:40],
            "control_updated_at": now,
            "handoff_expires_at": expires_at,
        }

    def session_control(self, account: str, buyer_id: str) -> dict[str, Any]:
        account = str(account or "").strip()
        buyer_id = str(buyer_id or "").strip()
        with self.lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT mode,reason,source,updated_at,expires_at FROM session_controls
                WHERE account=? AND buyer_id=?
                """,
                (account, buyer_id),
            ).fetchone()
            if row and str(row["mode"]) == "human":
                expires_at = float(row["expires_at"] or 0.0)
                if not expires_at or expires_at > time.time():
                    return {
                        "account": account,
                        "buyer_id": buyer_id,
                        "ai_mode": "human",
                        "handoff_reason": str(row["reason"]),
                        "handoff_source": str(row["source"]),
                        "control_updated_at": float(row["updated_at"]),
                        "handoff_expires_at": expires_at,
                    }
                connection.execute(
                    "DELETE FROM session_controls WHERE account=? AND buyer_id=?",
                    (account, buyer_id),
                )
        return {
            "account": account,
            "buyer_id": buyer_id,
            "ai_mode": "ai",
            "handoff_reason": "",
            "handoff_source": "",
            "control_updated_at": 0.0,
            "handoff_expires_at": 0.0,
        }

    def account_for_buyer(self, buyer_id: str) -> str:
        target = str(buyer_id or "").strip()
        if not target:
            return ""
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM events ORDER BY updated_at DESC LIMIT 1000"
            ).fetchall()
        for row in rows:
            try:
                event = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if str(event.get("buyer_id") or "").strip() == target:
                return str(event.get("account") or "").strip()
        return ""

    def workbench_messages(
        self, account: str, buyer_id: str, limit: int = 300
    ) -> list[dict[str, Any]]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT event_id,payload,status FROM events ORDER BY created_at DESC LIMIT ?",
                (WORKBENCH_MESSAGE_LIMIT,),
            ).fetchall()
            send_rows = connection.execute(
                """
                SELECT request_id,content,status,response,created_at,updated_at,confirmation_event_id
                FROM sends
                WHERE buyer_cid=? AND content<>''
                  AND status IN ('in_flight','submitted','confirmed','unknown','rejected')
                  AND (status<>'confirmed' OR confirmation_event_id='')
                ORDER BY created_at
                """,
                (buyer_id,),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        suppressed_parent_ids = self.brain_suppressed_message_ids()
        for row in rows:
            try:
                event = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if str(event.get("account") or "") != account:
                continue
            if str(event.get("buyer_id") or "") != buyer_id:
                continue
            content = str(event.get("content") or "")
            if not content:
                continue
            role = str(event.get("role") or "user").lower()
            if (
                role == "assistant_simulated"
                and str(event.get("parent_msg_id") or "") in suppressed_parent_ids
            ):
                continue
            context_keys = (
                "goods_id", "goods_name", "goods_url", "goods_thumb_url",
                "goods_price", "goods_spec", "order_id", "order_price",
                "create_time", "pay_time", "consign_time", "express_company",
                "express_order_number", "after_sale_text", "category",
                "buyer_encrypt_id", "chat_scene", "context_enrich",
                "order_info", "order_context", "local_context_lookup",
                "recent_orders", "inquiry_goods",
            )
            messages.append({
                "event_id": str(row["event_id"]),
                "msg_id": str(event.get("original_msg_id") or event.get("msg_id") or ""),
                "role": (
                    "draft"
                    if role == "assistant_simulated"
                    else "seller" if role in {"assistant", "mall_cs", "seller", "self"} else "user"
                ),
                "content": content,
                "ts": event_timestamp(event),
                "status": "ai_draft" if role == "assistant_simulated" else str(row["status"]),
                "source": str(event.get("source") or ""),
                "capture_mode": str(event.get("capture_mode") or ""),
                "raw_type": str(event.get("raw_type") or ""),
                "context": {
                    key: event.get(key) for key in context_keys if is_nonempty(event.get(key))
                },
            })
        for row in send_rows:
            try:
                response = json.loads(row["response"])
            except (TypeError, ValueError):
                response = {}
            messages.append({
                "event_id": str(row["request_id"]),
                "msg_id": str(row["confirmation_event_id"] or row["request_id"]),
                "role": "seller",
                "content": str(row["content"]),
                "ts": float(row["created_at"]),
                "status": str(row["status"]),
                "source": "local-send",
                "error": str(response.get("error") or ""),
                "capture_mode": "",
                "raw_type": "text",
                "context": {},
            })
        messages.sort(key=lambda item: (float(item["ts"]), item["event_id"]))
        return messages[-max(1, min(limit, 1000)):]

    def get_send(self, request_id: str) -> sqlite3.Row | None:
        with self.lock, self.connect() as connection:
            return connection.execute(
                "SELECT * FROM sends WHERE request_id=?", (request_id,)
            ).fetchone()

    def claim_send(
        self,
        request_id: str,
        payload_hash: str,
        buyer_cid: str,
        content: str,
    ) -> tuple[bool, sqlite3.Row | None]:
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM sends WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                return False, existing
            response = {
                "ok": False,
                "request_id": request_id,
                "status": "in_flight",
                "submitted": False,
                "confirmed": False,
                "error": "send outcome is pending or unknown; automatic retry is blocked",
            }
            now = time.time()
            connection.execute(
                """
                INSERT INTO sends(
                    request_id,payload_hash,status,response,created_at,updated_at,
                    buyer_cid,content,confirmation_event_id,submitted_at,confirmed_at
                )
                VALUES(?,?,'in_flight',?,?,?,?,?,'',0,0)
                """,
                (request_id, payload_hash, json_text(response), now, now, buyer_cid, content),
            )
            return True, None

    def mark_send_submitted(
        self,
        request_id: str,
        payload_hash: str,
    ) -> dict[str, Any]:
        now = time.time()
        response = {
            "ok": True,
            "request_id": request_id,
            "status": "submitted",
            "submitted": True,
            "confirmed": False,
        }
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE sends SET status='submitted',response=?,submitted_at=?,updated_at=?
                WHERE request_id=? AND payload_hash=? AND status='in_flight'
                """,
                (json_text(response), now, now, request_id, payload_hash),
            )
            if cursor.rowcount:
                return response
            existing = connection.execute(
                "SELECT payload_hash,response FROM sends WHERE request_id=?", (request_id,)
            ).fetchone()
            if not existing or existing["payload_hash"] != payload_hash:
                raise RuntimeError("submitted send receipt could not be finalized")
            return json.loads(existing["response"])

    def mark_send_rejected(
        self,
        request_id: str,
        payload_hash: str,
        error: str,
    ) -> dict[str, Any]:
        response = {
            "ok": False,
            "request_id": request_id,
            "status": "rejected",
            "submitted": False,
            "confirmed": False,
            "error": error[:500],
        }
        with self.lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sends SET status='rejected',response=?,updated_at=?
                WHERE request_id=? AND payload_hash=? AND status='in_flight'
                """,
                (json_text(response), time.time(), request_id, payload_hash),
            )
            if cursor.rowcount:
                return response
            existing = connection.execute(
                "SELECT payload_hash,response FROM sends WHERE request_id=?", (request_id,)
            ).fetchone()
            if not existing or existing["payload_hash"] != payload_hash:
                raise RuntimeError("rejected send receipt could not be finalized")
            return json.loads(existing["response"])

    def expire_unconfirmed_sends(self, timeout_seconds: float, now: float | None = None) -> int:
        current = time.time() if now is None else now
        cutoff = current - max(1.0, timeout_seconds)
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id,response FROM sends
                WHERE status IN ('in_flight','submitted') AND created_at<=?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                try:
                    response = json.loads(row["response"])
                except (TypeError, ValueError):
                    response = {}
                response.update({
                    "ok": False,
                    "request_id": row["request_id"],
                    "status": "unknown",
                    "submitted": True,
                    "confirmed": False,
                    "error": "Qianniu did not emit a delivery confirmation before timeout; do not retry automatically",
                })
                connection.execute(
                    "UPDATE sends SET status='unknown',response=?,updated_at=? WHERE request_id=?",
                    (json_text(response), current, row["request_id"]),
                )
            return len(rows)

    def confirm_send_from_event(
        self,
        event: dict[str, Any],
        event_id: str,
        max_age_seconds: float = 300.0,
    ) -> str:
        role = str(event.get("role") or "").lower()
        if role not in {"assistant", "mall_cs", "seller", "self"}:
            return ""
        buyer_cid = str(event.get("buyer_id") or "").strip()
        content = str(event.get("content") or "")
        if not buyer_cid or not content or not event_id:
            return ""
        observed_at = event_timestamp(event) or time.time()
        lower_bound = observed_at - max(1.0, max_age_seconds)
        upper_bound = observed_at + 5.0
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_id,payload_hash FROM sends
                WHERE (
                    status IN ('in_flight','submitted','unknown')
                    OR (status='confirmed' AND confirmation_event_id='')
                )
                  AND buyer_cid=? AND content=?
                  AND created_at BETWEEN ? AND ?
                ORDER BY created_at,request_id
                LIMIT 1
                """,
                (buyer_cid, content, lower_bound, upper_bound),
            ).fetchone()
            if not row:
                return ""
            now = time.time()
            response = {
                "ok": True,
                "request_id": row["request_id"],
                "status": "confirmed",
                "submitted": True,
                "confirmed": True,
                "event_id": event_id,
                "message_id": str(event.get("original_msg_id") or event.get("msg_id") or ""),
            }
            cursor = connection.execute(
                """
                UPDATE sends
                SET status='confirmed',response=?,confirmation_event_id=?,confirmed_at=?,updated_at=?
                WHERE request_id=? AND payload_hash=?
                  AND (
                      status IN ('in_flight','submitted','unknown')
                      OR (status='confirmed' AND confirmation_event_id='')
                  )
                """,
                (
                    json_text(response), event_id, now, now,
                    row["request_id"], row["payload_hash"],
                ),
            )
            return str(row["request_id"]) if cursor.rowcount else ""

    def confirm_send_from_callback(
        self,
        request_id: str,
        payload_hash: str,
        result_code: int,
    ) -> dict[str, Any]:
        if result_code != 0:
            return self.mark_send_rejected(
                request_id,
                payload_hash,
                f"Qianniu MessageSDK rejected send with result code {result_code}",
            )
        now = time.time()
        response = {
            "ok": True,
            "request_id": request_id,
            "status": "confirmed",
            "submitted": True,
            "confirmed": True,
            "confirmation_source": "messagesdk_callback",
        }
        with self.lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sends
                SET status='confirmed',response=?,confirmed_at=?,updated_at=?
                WHERE request_id=? AND payload_hash=?
                  AND status IN ('in_flight','submitted','unknown')
                """,
                (json_text(response), now, now, request_id, payload_hash),
            )
            if cursor.rowcount:
                return response
            existing = connection.execute(
                "SELECT payload_hash,response FROM sends WHERE request_id=?", (request_id,)
            ).fetchone()
            if not existing or existing["payload_hash"] != payload_hash:
                raise RuntimeError("callback send receipt could not be finalized")
            return json.loads(existing["response"])

    def send_counts(self) -> dict[str, int]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM sends GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def enqueue_brain_event(self, event_id: str) -> bool:
        now = time.time()
        with self.lock, self.connect() as connection:
            event = connection.execute(
                "SELECT revision FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if event is None:
                return False
            existing = connection.execute(
                "SELECT revision,status FROM brain_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing and existing["status"] in {"sending", "delivered"}:
                return False
            connection.execute(
                """
                INSERT INTO brain_events(
                    event_id,revision,status,attempts,last_error,queued_at,updated_at,delivered_at
                ) VALUES(?,?,'pending',0,'',?,?,NULL)
                ON CONFLICT(event_id) DO UPDATE SET
                    revision=excluded.revision,
                    status='pending',
                    last_error='',
                    updated_at=excluded.updated_at,
                    delivered_at=NULL
                """,
                (event_id, str(event["revision"]), now, now),
            )
            return True

    def max_event_rowid(self) -> int:
        """Row watermark of the newest locally stored event."""
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM events"
            ).fetchone()
        return int(row[0] or 0)

    def enqueue_untracked_brain_events_after_rowid(
        self, after_rowid: int, limit: int = WORKBENCH_EVENT_LIMIT
    ) -> int:
        """Queue local captures made after a session watermark.

        Row ids are strictly increasing, so this stays correct even when the
        clock is coarser than the gap between two captures. On Windows
        ``time.time()`` only ticks about every 15ms, and comparing timestamps
        with ``>=`` let a capture made just before a reconnect be queued again,
        which would upload an old buyer message a second time.
        """
        now = time.time()
        queued = 0
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.event_id,e.revision,e.payload
                FROM events e
                LEFT JOIN brain_events b ON b.event_id=e.event_id
                WHERE b.event_id IS NULL AND e.rowid>?
                ORDER BY e.rowid
                LIMIT ?
                """,
                (max(0, int(after_rowid)), max(1, min(int(limit), WORKBENCH_EVENT_LIMIT))),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError):
                    continue
                if (
                    payload.get("brain_suppressed")
                    or brain_event_suppression_reason(payload)
                    or str(payload.get("capture_mode") or "") == "brain_projection"
                    or str(payload.get("source") or "") == "brain_shadow"
                ):
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO brain_events(
                        event_id,revision,status,attempts,last_error,queued_at,updated_at,delivered_at
                    ) VALUES(?,?,'pending',0,'',?,?,NULL)
                    """,
                    (str(row["event_id"]), str(row["revision"]), now, now),
                )
                queued += int(connection.execute("SELECT changes()").fetchone()[0] or 0)
        return queued

    def enrich_event(self, event_id: str, enrichment: dict[str, Any]) -> bool:
        if not enrichment:
            return False
        now = time.time()
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload,revision FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(row["payload"])
            merged = merge_events(payload, enrichment)
            merged["event_id"] = event_id
            encoded = json_text(merged)
            revision = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if revision == str(row["revision"]):
                return False
            connection.execute(
                "UPDATE events SET payload=?,revision=?,updated_at=? WHERE event_id=?",
                (encoded, revision, now, event_id),
            )
            connection.execute(
                """
                UPDATE brain_events
                SET revision=?,status='pending',attempts=0,last_error='',updated_at=?,delivered_at=NULL
                WHERE event_id=? AND revision<>?
                """,
                (revision, now, event_id, revision),
            )
            self._invalidate_session_cache()
            return True

    def event_payload(self, event_id: str) -> dict[str, Any] | None:
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except (TypeError, ValueError):
            return {}

    def replace_event_payload(self, event_id: str, payload: dict[str, Any]) -> bool:
        event_id = str(event_id or "").strip()
        if not event_id:
            return False
        normalized = normalize_event(payload)
        normalized["event_id"] = event_id
        encoded = json_text(normalized)
        revision = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = time.time()
        with self.lock, self.connect() as connection:
            row = connection.execute(
                "SELECT revision FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE events SET payload=?,revision=?,updated_at=? WHERE event_id=?",
                (encoded, revision, now, event_id),
            )
            connection.execute(
                """
                UPDATE brain_events
                SET revision=?,status='pending',attempts=0,last_error='',updated_at=?,delivered_at=NULL
                WHERE event_id=? AND revision<>?
                """,
                (revision, now, event_id, revision),
            )
            self._invalidate_session_cache()
            return True

    def claim_brain_events(self, min_age_seconds: float, limit: int = 100) -> list[dict[str, Any]]:
        cutoff = time.time() - max(0.0, min_age_seconds)
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT b.event_id,b.revision,e.payload
                FROM brain_events b JOIN events e ON e.event_id=b.event_id
                WHERE b.status='pending' AND b.queued_at<=?
                ORDER BY b.queued_at LIMIT ?
                """,
                (cutoff, max(1, min(limit, 100))),
            ).fetchall()
            if rows:
                placeholders = ",".join("?" for _ in rows)
                connection.execute(
                    f"UPDATE brain_events SET status='sending',updated_at=? "
                    f"WHERE status='pending' AND event_id IN ({placeholders})",
                    (time.time(), *(row["event_id"] for row in rows)),
                )
        return [
            {
                "event_id": str(row["event_id"]),
                "revision": str(row["revision"]),
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def finish_brain_events(
        self,
        rows: list[dict[str, Any]],
        committed: set[str],
        error: str = "",
    ) -> None:
        now = time.time()
        with self.lock, self.connect() as connection:
            for row in rows:
                event_id = str(row["event_id"])
                revision = str(row["revision"])
                if event_id in committed:
                    connection.execute(
                        """
                        UPDATE brain_events
                        SET status='delivered',delivered_at=?,updated_at=?,last_error=''
                        WHERE event_id=? AND revision=? AND status='sending'
                        """,
                        (now, now, event_id, revision),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE brain_events
                        SET status='pending',attempts=attempts+1,last_error=?,updated_at=?
                        WHERE event_id=? AND revision=? AND status='sending'
                        """,
                        ((error or "大脑未确认该事件")[:500], now, event_id, revision),
                    )

    def brain_event_counts(self) -> dict[str, Any]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM brain_events GROUP BY status"
            ).fetchall()
            oldest = connection.execute(
                "SELECT MIN(queued_at) AS queued_at FROM brain_events WHERE status='pending'"
            ).fetchone()
        result: dict[str, Any] = {"pending": 0, "sending": 0, "delivered": 0}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        queued_at = float(oldest["queued_at"] or 0) if oldest else 0.0
        result["oldest_pending_seconds"] = round(max(0.0, time.time() - queued_at), 3) if queued_at else 0.0
        return result

    def claim_brain_command(self, command: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        command_id = str(command.get("id") or command.get("command_id") or "").strip()
        if not command_id or len(command_id) > 256:
            raise ValueError("brain command has no valid id")
        payload = json_text(command)
        semantic = dict(command)
        for key in (
            "command_lease_token", "lease_token", "state", "leased_at",
            "lease_expires_at", "updated_at", "attempts",
        ):
            semantic.pop(key, None)
        payload_hash = hashlib.sha256(json_text(semantic).encode("utf-8")).hexdigest()
        now = time.time()
        with self.lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM brain_commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if existing:
                if str(existing["payload_hash"]) != payload_hash:
                    raise ValueError("brain command id was reused with a different payload")
                connection.execute(
                    "UPDATE brain_commands SET payload=?,updated_at=? WHERE command_id=?",
                    (payload, now, command_id),
                )
                refreshed = connection.execute(
                    "SELECT * FROM brain_commands WHERE command_id=?", (command_id,)
                ).fetchone()
                return False, dict(refreshed)
            connection.execute(
                """
                INSERT INTO brain_commands(
                    command_id,payload_hash,payload,status,result,attempts,last_error,
                    created_at,updated_at,acknowledged_at
                ) VALUES(?,?,?,'in_flight','{}',0,'',?,?,NULL)
                """,
                (command_id, payload_hash, payload, now, now),
            )
            row = connection.execute(
                "SELECT * FROM brain_commands WHERE command_id=?", (command_id,)
            ).fetchone()
        return True, dict(row)

    def store_brain_command_result(self, command_id: str, result: dict[str, Any]) -> None:
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE brain_commands
                SET status='result_pending',result=?,last_error='',updated_at=?
                WHERE command_id=?
                """,
                (json_text(result), time.time(), command_id),
            )

    def fail_brain_command_report(self, command_id: str, error: str) -> None:
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE brain_commands
                SET attempts=attempts+1,last_error=?,updated_at=?
                WHERE command_id=? AND status='result_pending'
                """,
                (error[:500], time.time(), command_id),
            )

    def acknowledge_brain_command(self, command_id: str) -> None:
        now = time.time()
        with self.lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE brain_commands
                SET status='acknowledged',last_error='',updated_at=?,acknowledged_at=?
                WHERE command_id=?
                """,
                (now, now, command_id),
            )

    def pending_brain_command_results(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT command_id,result FROM brain_commands
                WHERE status='result_pending' ORDER BY updated_at LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [
            {"command_id": str(row["command_id"]), "result": json.loads(row["result"])}
            for row in rows
        ]

    def brain_command_counts(self) -> dict[str, int]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM brain_commands GROUP BY status"
            ).fetchall()
        result = {"in_flight": 0, "result_pending": 0, "acknowledged": 0}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    def recent_brain_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT command_id,payload,status,result,attempts,last_error,created_at,updated_at
                FROM brain_commands ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        output = []
        for row in rows:
            payload = json.loads(row["payload"])
            output.append({
                "command_id": str(row["command_id"]),
                "type": str(payload.get("type") or ""),
                "buyer_id": str(payload.get("buyer_id") or ""),
                "status": str(row["status"]),
                "result": json.loads(row["result"]),
                "attempts": int(row["attempts"]),
                "last_error": str(row["last_error"]),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
            })
        return output


class DeliveryWorker:
    def __init__(self, app: "StandaloneBridge"):
        self.app = app
        self.wakeup = threading.Event()
        self.thread = threading.Thread(target=self.run, name="gateway-delivery", daemon=True)
        self.last_error = ""
        self.last_success_at = 0.0

    def start(self) -> None:
        self.thread.start()

    def post(self, batch: list[dict[str, Any]]) -> set[str]:
        cfg = self.app.config
        url = str(cfg.get("gateway_url")).rstrip("/") + "/api/local-seat/v1/events"
        body = json.dumps(
            {"agent_id": cfg.get("gateway_agent_id"), "events": batch}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Agent-Token": str(cfg.get("gateway_agent_token") or ""),
                "X-Agent-Id": str(cfg.get("gateway_agent_id") or ""),
                "X-Device-Id": str(cfg.get("device_id") or cfg.get("gateway_agent_id") or ""),
            },
        )
        with urllib.request.urlopen(
            request, timeout=float(cfg.get("delivery_timeout_seconds", 1.5))
        ) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(str((payload or {}).get("error") or "gateway rejected event batch"))
        acknowledgements = payload.get("event_acks")
        if isinstance(acknowledgements, list):
            return {
                str(item.get("event_id") or "")
                for item in acknowledgements
                if isinstance(item, dict) and item.get("committed") is True
            }
        if int(payload.get("local_ingested") or 0) >= len(batch):
            return {canonical_event_id(event) for event in batch}
        if "accepted" in payload and not (payload.get("errors") or []):
            return {canonical_event_id(event) for event in batch}
        raise RuntimeError("gateway did not return a durable acknowledgement")

    def run(self) -> None:
        delay = 0.2
        last_prune_at = 0.0
        while not self.app.stop_event.is_set():
            now = time.time()
            if now - last_prune_at >= 3600.0:
                try:
                    self.app.db.prune_old_events()
                except Exception as error:
                    self.last_error = str(error)[:500]
                last_prune_at = now
            if not self.app.config.get("delivery_enabled", True):
                self.wakeup.wait(1.0)
                self.wakeup.clear()
                continue
            rows = self.app.db.claim_pending(100)
            if not rows:
                self.wakeup.wait(1.0)
                self.wakeup.clear()
                continue
            try:
                committed = self.post([row["payload"] for row in rows])
                if not committed:
                    raise RuntimeError("gateway acknowledged no events")
                for row in rows:
                    if row["event_id"] in committed:
                        self.app.db.mark_delivered(row["event_id"], row["revision"])
                self.last_error = ""
                self.last_success_at = time.time()
                delay = 0.2
            except Exception as error:
                self.last_error = str(error)[:500]
                self.app.db.mark_failed(rows, self.last_error)
                self.app.stop_event.wait(delay)
                delay = min(5.0, delay * 2.0)


class BrainConnector:
    def __init__(self, app: "StandaloneBridge"):
        self.app = app
        self.session_started_at = time.time()
        # Row watermark, not the clock: captures made in the same clock tick as
        # the reconnect must not be backfilled as if they were new.
        db = getattr(app, "db", None)
        self.session_start_rowid = int(db.max_event_rowid()) if db is not None else 0
        self.wakeup = threading.Event()
        # 事件上报专用唤醒：control/commands 与 events 原本共用一个事件，事件
        # worker 一多会互相 clear，导致别的循环空等。拆开互不打扰。
        self.event_wakeup = threading.Event()
        self.control_thread = threading.Thread(
            target=self.run_control, name="brain-control", daemon=True
        )
        self.command_thread = threading.Thread(
            target=self.run_commands, name="brain-commands", daemon=True
        )
        self.draft_thread = threading.Thread(
            target=self.run_drafts, name="brain-drafts", daemon=True
        )
        self.event_thread = threading.Thread(
            target=self.run_events, name="brain-events", daemon=True
        )
        self.draft_wakeup = threading.Event()
        # Outbound send pool. A reply waits for the platform receipt (appbiz
        # callback 2s typical, send_confirmation_timeout 15s worst case), so a
        # single-threaded egress lets the previous command's confirmation hold
        # the fetch loop hostage and caps throughput at a few messages/minute.
        self._sender_queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=64)
        self._sender_threads: list[threading.Thread] = []
        self._sender_conv_locks: dict[str, threading.Lock] = {}
        self._sender_conv_locks_guard = threading.Lock()
        self._sender_pool_guard = threading.Lock()
        self._sender_pool_running = False
        self.lock = threading.RLock()
        self.registered = False
        self.state = "disabled"
        self.last_error = ""
        self.last_register_at = 0.0
        self.last_heartbeat_at = 0.0
        self.last_event_upload_at = 0.0
        self.last_command_poll_at = 0.0
        self.last_command_at = 0.0
        self.last_command_id = ""
        self.last_command_type = ""
        self.last_command_poll_count = 0
        self.last_draft_poll_at = 0.0
        self.last_draft_sync_at = 0.0
        self.last_draft_error = ""
        self.draft_imported_count = 0
        self.last_handoff_poll_at = 0.0
        self.last_handoff_sync_at = 0.0
        self.last_handoff_error = ""
        self.brain_handoff_count = 0
        self.last_heartbeat_response: dict[str, Any] = {}
        self.last_event_response: dict[str, Any] = {}
        self.server_status: dict[str, Any] = {}
        self.last_server_status_at = 0.0
        self.server_status_error = ""
        self.last_request_ms = 0.0
        self.request_count = 0
        self.reconnect_count = 0
        self.allowed_shop_ids: list[str] = []
        self._configuration_key = ""
        self._activity: list[dict[str, Any]] = []
        self._draft_watch: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._draft_seeded = False

    def start(self) -> None:
        self.start_sender_pool()
        self.control_thread.start()
        self.command_thread.start()
        self.draft_thread.start()
        self.event_thread.start()

    def configured(self) -> bool:
        return bool(
            self.app.config.get("brain_enabled", False)
            and str(self.app.config.get("brain_server_url") or "").strip()
            and brain_workstation_token(self.app.config)
        )

    def event_upload_ready(self) -> bool:
        with self.lock:
            return self.registered and self.last_heartbeat_at > 0

    def configuration_key(self) -> str:
        stable = json_text({
            "server": str(self.app.config.get("brain_server_url") or ""),
            "token": brain_workstation_token(self.app.config),
            "agent": self.agent_id(),
            "enabled": bool(self.app.config.get("brain_enabled", False)),
        })
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def agent_id(self) -> str:
        return str(
            self.app.config.get("brain_agent_id")
            or self.app.config.get("gateway_agent_id")
            or self.app.config.get("device_id")
            or ""
        ).strip()

    @staticmethod
    def account_shop_id(account: str) -> str:
        value = str(account or "").strip()
        if not value:
            return ""
        if value.startswith(("mall_", "tb_nick_", "tb_")):
            return value
        if value.startswith("cs_"):
            mall = value[3:].split(":", 1)[0].strip()
            if mall.isdigit():
                return f"mall_{mall}"
            return mall if mall.startswith("mall_") else ""
        return f"tb_nick_{value}"

    def account_allowed(self, account: str) -> bool:
        with self.lock:
            allowed = {str(value).strip() for value in self.allowed_shop_ids if str(value).strip()}
        if not allowed:
            return True
        value = str(account or "").strip()
        return bool(value and (value in allowed or self.account_shop_id(value) in allowed))

    def record(
        self,
        stage: str,
        state: str,
        detail: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        row = {
            "id": uuid.uuid4().hex,
            "ts": time.time(),
            "stage": stage,
            "state": state,
            "detail": detail[:500],
            "evidence": evidence or {},
        }
        with self.lock:
            self._activity.append(row)
            self._activity = self._activity[-80:]

    def activity(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in reversed(self._activity)]

    def configuration_changed(self) -> None:
        with self.lock:
            self.registered = False
            self.last_heartbeat_at = 0.0
            self.state = "connecting" if self.configured() else "disabled"
            self.last_error = ""
            self._configuration_key = ""
            self._draft_watch.clear()
            self._draft_seeded = False
            self.last_handoff_poll_at = 0.0
        self.record("config", "ok", "大脑配置已保存，连接器正在重新加载")
        self.wakeup.set()
        self.event_wakeup.set()
        self.draft_wakeup.set()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "X-Agent-Token": brain_workstation_token(self.app.config),
            "X-Agent-Id": self.agent_id(),
            "X-Device-Id": str(
                self.app.config.get("device_id") or self.agent_id()
            ),
        }

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        base = str(self.app.config.get("brain_server_url") or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("大脑地址未配置")
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers=self._headers(),
        )
        started = time.perf_counter()
        request_timeout = timeout or float(
            self.app.config.get("brain_request_timeout_seconds", 8.0)
        )
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"大脑 HTTP {error.code}: {detail or error.reason}") from error
        finally:
            with self.lock:
                self.last_request_ms = round((time.perf_counter() - started) * 1000.0, 2)
                self.request_count += 1
        try:
            payload = json.loads(raw or "{}")
        except ValueError as error:
            raise RuntimeError("大脑返回了无效 JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("大脑返回值不是 JSON 对象")
        if payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error") or "大脑拒绝了请求"))
        return payload

    def register(self) -> dict[str, Any]:
        with self.lock:
            self.registered = False
            self.last_heartbeat_at = 0.0
        payload = self.request("POST", "/api/bridge/v1/register", {
            "agent_id": self.agent_id(),
            "agent_name": str(self.app.config.get("brain_agent_name") or "千牛本机工位"),
            "version": VERSION,
            "build_hash": BUILD_HASH,
        })
        agent_payload = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
        assigned_agent_id = str(
            payload.get("agent_id") or agent_payload.get("agent_id") or agent_payload.get("id") or ""
        ).strip()
        if assigned_agent_id:
            self.app.config.set_brain_agent_id(assigned_agent_id)
        allowed = payload.get("allowed_shop_ids") or agent_payload.get("allowed_shop_ids") or []
        if isinstance(allowed, list):
            self.allowed_shop_ids = sorted({str(value) for value in allowed if str(value).strip()})
        now = time.time()
        with self.lock:
            self.registered = True
            self.state = "online"
            self.last_register_at = now
            self.last_error = ""
        self.record("register", "ok", "工位注册成功", {
            "agent_id": self.agent_id(), "request_ms": self.last_request_ms,
        })
        backfilled = self.app.db.enqueue_untracked_brain_events_after_rowid(
            self.session_start_rowid
        )
        if backfilled:
            self.record("events", "ok", f"queued {backfilled} local events after brain reconnect")
        self.wakeup.set()
        self.event_wakeup.set()
        return payload

    def heartbeat_status(self) -> dict[str, Any]:
        local = self.app.status(include_brain=False)
        sessions = self.app.db.workbench_sessions()
        now = time.time()
        accounts = sorted({
            str(session.get("account") or "").strip()
            for session in sessions
            if str(session.get("account") or "").strip()
            and now - float(session.get("last_ts") or 0.0) < 3600.0
        })
        shops = [
            {
                "shop_id": self.account_shop_id(account),
                "account": account,
                "platform": "taobao",
                "source": "bridge_heartbeat",
                "shop_name": account,
            }
            for account in accounts
            if self.account_shop_id(account) and self.account_allowed(account)
        ]
        browser = local.get("browser") if isinstance(local.get("browser"), dict) else {}
        browser_diagnostics = (
            browser.get("diagnostics")
            if isinstance(browser.get("diagnostics"), dict)
            else {}
        )
        appbiz = local.get("appbiz_send") if isinstance(local.get("appbiz_send"), dict) else {}
        delivery = local.get("delivery") if isinstance(local.get("delivery"), dict) else {}
        send_ready = bool(appbiz.get("ready") and local.get("send_enabled"))
        send_route_ready = bool(appbiz.get("route_ready") and local.get("send_enabled"))
        openbot_connected = bool(
            int(browser.get("connected") or 0) > 0
            and browser_diagnostics.get("websocket_connected", True)
        )
        with self.lock:
            allowed_shop_ids = sorted({
                str(value).strip() for value in self.allowed_shop_ids if str(value).strip()
            })
        return {
            "version": VERSION,
            "build_hash": BUILD_HASH,
            "platform": "taobao",
            "platform_label": "淘宝/千牛",
            "watching": openbot_connected,
            "attached": {"aliworkbench_pid": int(appbiz.get("pid") or 0)},
            "accounts_seen": accounts,
            "shops": shops,
            "allowed_shop_ids": allowed_shop_ids,
            "scope_filtered_events": 0,
            "scope_blocked_commands": 0,
            "pending_events": int(delivery.get("pending") or 0),
            "pending_command_acks": int(
                self.app.db.brain_command_counts().get("result_pending") or 0
            ),
            "watcher_stats": browser_diagnostics,
            "dll_ready": send_ready,
            "dll_route_ready": send_route_ready,
            "openbot_connected": openbot_connected,
            "dll_port": None,
            "cdp_port": int((local.get("cdp") or {}).get("port") or 0),
            "workbench_pid": int((local.get("cdp") or {}).get("host_pid") or 0),
            "port_discovery": {},
            "channel_hint": "openbot 桥已连接，可发送" if openbot_connected else "千牛消息桥未连接",
            "hint": "openbot 桥已连接，可发送" if send_ready else "千牛发送链路尚未就绪",
            "last_error": str(browser.get("error") or appbiz.get("last_error") or ""),
            "dry_run": not bool(self.app.config.get("brain_ai_reply_enabled", True)),
            "openbot": browser_diagnostics,
            "send_ready": send_ready,
            "send_route_ready": send_route_ready,
            "standalone_status": local,
        }

    def heartbeat(self) -> None:
        status = self.heartbeat_status()
        payload = self.request("POST", "/api/bridge/v1/heartbeat", {
            "agent_id": self.agent_id(),
            "agent_name": str(self.app.config.get("brain_agent_name") or "千牛本机工位"),
            "status": status,
        })
        with self.lock:
            self.last_heartbeat_at = time.time()
            self.last_heartbeat_response = {
                "agent_id": str(payload.get("agent_id") or ""),
                "queued_commands": int(payload.get("queued_commands") or 0),
                "merged_shops": list(payload.get("merged_shops") or []),
                "scope_filtered": int(payload.get("scope_filtered") or 0),
            }
            self.state = "online"
            self.last_error = ""

    def refresh_server_status(self) -> None:
        try:
            payload = self.request("GET", "/api/status")
            shadow = payload.get("shadow") if isinstance(payload.get("shadow"), dict) else {}
            status = {
                "brain_mode": str(payload.get("brain_mode") or ""),
                "send_mode": str(payload.get("send_mode") or ""),
                "own_auto_send_enabled": bool(payload.get("own_auto_send_enabled")),
                "armed_shop_count": int(payload.get("armed_shop_count") or 0),
                "last_auto_send": payload.get("last_auto_send"),
                "shadow_enabled": bool(shadow.get("enabled")),
                "shadow_running": bool(shadow.get("running")),
                "shadow_last_success_at": str(shadow.get("last_success_at") or ""),
            }
            with self.lock:
                self.server_status = status
                self.last_server_status_at = time.time()
                self.server_status_error = ""
        except Exception as error:
            # Older brain servers may not expose /api/status. This diagnostic
            # endpoint must never degrade register, heartbeat, or command pull.
            with self.lock:
                self.server_status_error = str(error)[:500]

    def watch_draft(
        self,
        account: str,
        buyer_id: str,
        parent_msg_id: str = "",
        buyer_nick: str = "",
        watch_seconds: float = 120.0,
    ) -> None:
        account = str(account or "").strip()
        buyer_id = str(buyer_id or "").strip()
        parent_msg_id = str(parent_msg_id or "").strip()
        if not account or not buyer_id:
            return
        now = time.time()
        key = (account, buyer_id, parent_msg_id)
        with self.lock:
            current = self._draft_watch.get(key) or {}
            self._draft_watch[key] = {
                "account": account,
                "buyer_id": buyer_id,
                "parent_msg_id": parent_msg_id,
                "buyer_nick": str(buyer_nick or current.get("buyer_nick") or ""),
                "deadline": max(float(current.get("deadline") or 0.0), now + watch_seconds),
                "next_poll": min(float(current.get("next_poll") or now), now),
            }
        self.draft_wakeup.set()

    def seed_draft_watch(self) -> None:
        with self.lock:
            if self._draft_seeded:
                return
            self._draft_seeded = True
        now = time.time()
        for session in self.app.db.workbench_sessions()[:20]:
            if now - float(session.get("last_ts") or 0.0) > 3600.0:
                continue
            self.watch_draft(
                str(session.get("account") or ""),
                str(session.get("buyer_id") or ""),
                buyer_nick=str(session.get("buyer_nick") or ""),
                watch_seconds=30.0,
            )

    def sync_session_drafts(
        self, account: str, buyer_id: str, buyer_nick: str = ""
    ) -> tuple[int, set[str]]:
        query = urllib.parse.urlencode({"account": account, "lite": "1", "limit": "100"})
        quoted_buyer = urllib.parse.quote(buyer_id, safe="")
        payload = self.request("GET", f"/api/session/{quoted_buyer}?{query}")
        if "handoff" in payload:
            self.apply_brain_handoff(
                account,
                str(payload.get("buyer_id") or buyer_id),
                bool(payload.get("handoff")),
                str(payload.get("handoff_reason") or ""),
            )
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        session_nick = preferred_buyer_nick(payload, buyer_id, buyer_nick)
        suppressed_parent_ids = self.app.db.brain_suppressed_message_ids()
        imported = 0
        parent_ids: set[str] = set()
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "").strip().lower() != "assistant_simulated":
                continue
            content = str(message.get("content") or "").strip()
            msg_id = str(message.get("msg_id") or "").strip()
            if not content or not msg_id:
                continue
            parent_msg_id = str(message.get("parent_msg_id") or "").strip()
            if parent_msg_id:
                parent_ids.add(parent_msg_id)
            if parent_msg_id in suppressed_parent_ids:
                continue
            message_ts = message.get("ts") or time.time()
            try:
                captured_at_ms = int(float(message_ts) * (1 if float(message_ts) > 1e11 else 1000))
            except (TypeError, ValueError):
                captured_at_ms = int(time.time() * 1000)
            _event_id, changed = self.app.db.upsert_local_projection({
                "platform": "taobao",
                "msg_id": msg_id,
                "original_msg_id": msg_id,
                "account": account,
                "shop_id": self.account_shop_id(account),
                "buyer_id": str(payload.get("buyer_id") or buyer_id),
                "buyer_nick": session_nick,
                "role": "assistant_simulated",
                "content": content,
                "ts": message_ts,
                "captured_at_ms": captured_at_ms,
                "source": "brain_shadow",
                "capture_mode": "brain_projection",
                "delivery_status": "simulated",
                "shadow_status": str(message.get("shadow_status") or ""),
                "parent_msg_id": parent_msg_id,
                "brain_agent_id": self.agent_id(),
            })
            if changed:
                imported += 1
        return imported, parent_ids

    def apply_brain_handoff(
        self,
        account: str,
        buyer_id: str,
        handoff: bool,
        reason: str = "",
    ) -> bool:
        account = str(account or "").strip()
        buyer_id = str(buyer_id or "").strip()
        if not account or not buyer_id:
            return False
        current = self.app.db.session_control(account, buyer_id)
        if handoff:
            normalized_reason = str(reason or "大脑判断需要人工介入")[:200]
            if current["ai_mode"] == "human" and current["handoff_source"] != "brain":
                return False
            if (
                current["ai_mode"] == "human"
                and current["handoff_source"] == "brain"
                and current["handoff_reason"] == normalized_reason
            ):
                return False
            self.app.db.set_session_control(
                account, buyer_id, "human", normalized_reason, "brain"
            )
            return True
        if current["ai_mode"] == "human" and current["handoff_source"] == "brain":
            self.app.db.set_session_control(account, buyer_id, "ai", "", "brain")
            return True
        return False

    def sync_brain_handoffs(self) -> dict[str, int]:
        remote: dict[tuple[str, str], str] = {}
        page = 1
        while True:
            query = urllib.parse.urlencode({
                "paged": "1", "scope": "handoff", "page": page, "limit": 100,
            })
            payload = self.request("GET", f"/api/sessions?{query}")
            sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
            for session in sessions:
                if not isinstance(session, dict) or not session.get("handoff"):
                    continue
                account = str(session.get("account") or "").strip()
                buyer_id = str(session.get("buyer_id") or "").strip()
                if account and buyer_id:
                    remote[(account, buyer_id)] = str(session.get("handoff_reason") or "")
            pages = max(1, int(payload.get("pages") or 1))
            if page >= pages:
                break
            page += 1

        changed = 0
        for (account, buyer_id), reason in remote.items():
            changed += int(self.apply_brain_handoff(account, buyer_id, True, reason))
        existing = self.app.db.brain_handoff_controls()
        cleared = 0
        for account, buyer_id in set(existing) - set(remote):
            if self.apply_brain_handoff(account, buyer_id, False):
                cleared += 1
        return {"remote": len(remote), "changed": changed, "cleared": cleared}

    def run_drafts(self) -> None:
        while not self.app.stop_event.is_set():
            with self.lock:
                ready = self.registered
            if (
                not self.configured()
                or not ready
                or not self.app.config.get("brain_draft_sync_enabled", True)
            ):
                self.draft_wakeup.wait(1.0)
                self.draft_wakeup.clear()
                continue
            now = time.time()
            handoff_poll_seconds = max(
                1.0, float(self.app.config.get("brain_handoff_poll_seconds", 3.0))
            )
            if now - self.last_handoff_poll_at >= handoff_poll_seconds:
                with self.lock:
                    self.last_handoff_poll_at = now
                try:
                    result = self.sync_brain_handoffs()
                    with self.lock:
                        self.last_handoff_sync_at = time.time()
                        self.last_handoff_error = ""
                        self.brain_handoff_count = int(result["remote"])
                    if result["changed"] or result["cleared"]:
                        self.record(
                            "handoff_sync",
                            "warn" if result["remote"] else "ok",
                            f"大脑待人工 {result['remote']} 个，新增/更新 {result['changed']} 个，解除 {result['cleared']} 个",
                            result,
                        )
                except Exception as error:
                    with self.lock:
                        self.last_handoff_error = str(error)[:500]
            self.seed_draft_watch()
            now = time.time()
            with self.lock:
                for key, row in list(self._draft_watch.items()):
                    if float(row.get("deadline") or 0.0) <= now:
                        self._draft_watch.pop(key, None)
                due = next(
                    (
                        (key, dict(row))
                        for key, row in self._draft_watch.items()
                        if float(row.get("next_poll") or 0.0) <= now
                    ),
                    None,
                )
            if due is None:
                self.draft_wakeup.wait(0.5)
                self.draft_wakeup.clear()
                continue
            key, row = due
            try:
                imported, parent_ids = self.sync_session_drafts(
                    str(row["account"]), str(row["buyer_id"]), str(row.get("buyer_nick") or "")
                )
                now = time.time()
                parent_msg_id = str(row.get("parent_msg_id") or "")
                with self.lock:
                    self.last_draft_poll_at = now
                    self.last_draft_error = ""
                    self.draft_imported_count += imported
                    if imported:
                        self.last_draft_sync_at = now
                    if not parent_msg_id or parent_msg_id in parent_ids:
                        self._draft_watch.pop(key, None)
                    elif key in self._draft_watch:
                        self._draft_watch[key]["next_poll"] = now + max(
                            0.5, float(self.app.config.get("brain_draft_poll_seconds", 1.5))
                        )
                if imported:
                    self.record("ai_draft", "ok", f"同步 {imported} 条 AI 草稿到本地工作台", {
                        "account": row["account"], "buyer_id": row["buyer_id"],
                    })
            except Exception as error:
                now = time.time()
                with self.lock:
                    self.last_draft_poll_at = now
                    self.last_draft_error = str(error)[:500]
                    if key in self._draft_watch:
                        self._draft_watch[key]["next_poll"] = now + 3.0
                self.draft_wakeup.wait(0.5)
                self.draft_wakeup.clear()

    def pull_commands(self) -> list[dict[str, Any]]:
        wait_seconds = max(
            0.0,
            min(10.0, float(self.app.config.get("brain_command_poll_seconds", 1.5))),
        )
        query = urllib.parse.urlencode({
            "agent_id": self.agent_id(), "wait_seconds": wait_seconds,
        })
        payload = self.request(
            "GET",
            f"/api/bridge/v1/commands?{query}",
            timeout=max(8.0, wait_seconds + 5.0),
        )
        with self.lock:
            self.last_command_poll_at = time.time()
        commands = payload.get("commands") or []
        output = [row for row in commands if isinstance(row, dict)] if isinstance(commands, list) else []
        with self.lock:
            self.last_command_poll_count = len(output)
        return output

    def report_command_result(self, command_id: str, result: dict[str, Any]) -> None:
        quoted = urllib.parse.quote(command_id, safe="")
        payload = self.request(
            "POST",
            f"/api/bridge/v1/commands/{quoted}/result",
            {"agent_id": self.agent_id(), "result": result},
        )
        acknowledgement = payload.get("command_ack")
        if isinstance(acknowledgement, dict) and acknowledgement.get("committed") is False:
            raise RuntimeError(str(acknowledgement.get("error") or "大脑尚未持久化命令结果"))
        self.app.db.acknowledge_brain_command(command_id)
        self.record("command_ack", "ok", f"命令结果已确认：{command_id}", {
            "command_id": command_id, "request_ms": self.last_request_ms,
        })

    def execute_command(self, command: dict[str, Any]) -> dict[str, Any]:
        command_id = str(command.get("id") or command.get("command_id") or "").strip()
        command_type = str(command.get("type") or "send_text").strip().lower()
        meta = command.get("meta") if isinstance(command.get("meta"), dict) else {}
        buyer_id = str(command.get("buyer_id") or meta.get("buyer_id") or "").strip()
        account = str(command.get("account") or meta.get("account") or "").strip()
        if not account:
            account = self.app.db.account_for_buyer(buyer_id)
        content = str(command.get("content") or meta.get("content") or "")
        lease_token = str(command.get("command_lease_token") or command.get("lease_token") or "")
        started = time.perf_counter()
        base_result: dict[str, Any] = {
            "command_id": command_id,
            "command_lease_token": lease_token,
            "lease_token": lease_token,
        }
        if not self.account_allowed(account):
            return {
                **base_result,
                "ok": False,
                "status": "blocked",
                "error": "agent_shop_scope_denied",
                "error_user": "该店铺不在本机桥接授权范围内，已拒绝执行",
                "via": "client_shop_scope_guard",
                "real_send": False,
            }
        if command_type == "send_text":
            if not self.app.config.get("brain_ai_reply_enabled", True):
                return {
                    **base_result,
                    "ok": False,
                    "status": "blocked",
                    "error": "brain_ai_reply_disabled",
                    "error_user": "本机已暂停 AI 自动回复",
                    "real_send": False,
                }
            safety = outbound_safety_result(content)
            if safety:
                return {**base_result, **safety}
            control = self.app.db.session_control(account, buyer_id)
            if control["ai_mode"] == "human":
                return {
                    **base_result,
                    "ok": False,
                    "status": "blocked",
                    "error": "session in human handoff",
                    "error_user": "该会话已转人工，AI/自动回复已暂停；请人工处理或解除接管后再发",
                    "via": "handoff",
                    "real_send": False,
                    "handoff_reason": control["handoff_reason"],
                }
            request_id = "brain-command-" + hashlib.sha256(
                command_id.encode("utf-8", "surrogatepass")
            ).hexdigest()
            response = self.app.send_text({
                "request_id": request_id,
                "buyer_cid": buyer_id,
                "content": content,
            }, brain_authorized=True)
            status = str(response.get("status") or "unknown")
            return {
                **base_result,
                **response,
                "ok": status in {"in_flight", "submitted", "confirmed"},
                "via": "qianniu_appbiz",
                "real_send": True,
                "command_wall_ms": round((time.perf_counter() - started) * 1000.0, 2),
            }
        if command_type == "open_chat":
            if not self.app.config.get("brain_remote_open_chat_enabled", False):
                return {
                    **base_result,
                    "ok": False,
                    "status": "blocked",
                    "error": "remote_open_chat_disabled",
                    "error_user": "本机未允许大脑远程打开千牛会话",
                }
            nick = str(command.get("buyer_nick") or command.get("nick") or meta.get("buyer_nick") or "")
            security_uid = ContextEnricher.buyer_encrypt_id({"buyer_id": buyer_id})
            opened = self.app.browser.open_conversation(
                nick,
                expected_ccode=buyer_id,
                security_uid=security_uid,
            )
            return {
                **base_result,
                **opened,
                "status": "opened",
                "command_wall_ms": round((time.perf_counter() - started) * 1000.0, 2),
            }
        return {
            **base_result,
            "ok": False,
            "status": "unsupported",
            "error": f"unsupported_command:{command_type}",
        }

    # ------------------------------------------------------------------ 出站并发池
    # 单线程出站时「取指令」会被上一条的发送确认占住（appbiz 回调默认 2s，最坏
    # send_confirmation_timeout 15s），吞吐上限只有几条/分钟。这里把取指令与发送
    # 解耦：取指令只入队，发送在池里并发；同一 (account, buyer_id) 由逐会话锁保证
    # 有序；池满退回同步发送形成背压。开关关闭时行为与改动前完全一致。
    def sender_worker_count(self) -> int:
        try:
            workers = int(self.app.config.get("command_sender_workers", 6) or 6)
        except (TypeError, ValueError):
            workers = 6
        return max(1, min(workers, 8))

    def sender_pool_ready(self) -> bool:
        return self._sender_pool_running and not self.app.stop_event.is_set()

    def sender_conversation_lock(self, command: dict[str, Any]) -> threading.Lock:
        """同一买家保持串行，避免并发发送打乱回复顺序。"""
        key = "{}|{}".format(
            str(command.get("account") or command.get("seller") or "").strip(),
            str(command.get("buyer_id") or command.get("buyer_cid") or "").strip(),
        )
        with self._sender_conv_locks_guard:
            lock = self._sender_conv_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._sender_conv_locks[key] = lock
            return lock

    def sender_worker_loop(self, index: int) -> None:
        while not self.app.stop_event.is_set():
            try:
                command = self._sender_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with self.sender_conversation_lock(command):
                    self.handle_command(command)
            except Exception:
                # 单条指令异常不能打死发送线程，否则出站会整体停摆。
                LOG.exception("command sender worker %s failed", index)
            finally:
                self._sender_queue.task_done()

    def start_sender_pool(self) -> None:
        """幂等启动发送池；关闭开关时一个线程都不建。"""
        if not bool(self.app.config.get("command_sender_pool_enabled", True)):
            return
        with self._sender_pool_guard:
            if self._sender_threads:
                self._sender_pool_running = True
                return
            workers = self.sender_worker_count()
            for index in range(workers):
                thread = threading.Thread(
                    target=self.sender_worker_loop,
                    args=(index + 1,),
                    name="brain-command-sender-{}".format(index + 1),
                    daemon=True,
                )
                thread.start()
                self._sender_threads.append(thread)
            self._sender_pool_running = True
            LOG.info("command sender pool started workers=%s", workers)

    def dispatch_command(self, command: dict[str, Any]) -> None:
        """指令交给发送池；池未启用或已满时退回同步执行（与改动前一致）。"""
        if not self.sender_pool_ready():
            self.handle_command(command)
            return
        try:
            self._sender_queue.put_nowait(command)
        except queue.Full:
            # 背压：池子写满时同步发送，宁慢不丢。
            LOG.warning("command sender pool is full, falling back to inline send")
            self.handle_command(command)

    def handle_command(self, command: dict[str, Any]) -> None:
        command_id = str(command.get("id") or command.get("command_id") or "").strip()
        command_type = str(command.get("type") or "send_text").strip().lower()
        is_new, stored = self.app.db.claim_brain_command(command)
        stored_status = str(stored.get("status") or "")
        if not is_new and stored_status in {"result_pending", "acknowledged"}:
            result = json.loads(str(stored.get("result") or "{}"))
        else:
            try:
                result = self.execute_command(command)
            except Exception as error:
                result = {
                    "ok": False,
                    "status": "indeterminate",
                    "error": str(error)[:500],
                    "command_id": command_id,
                    "command_lease_token": str(
                        command.get("command_lease_token") or command.get("lease_token") or ""
                    ),
                    "lease_token": str(
                        command.get("command_lease_token") or command.get("lease_token") or ""
                    ),
                }
            self.app.db.store_brain_command_result(command_id, result)
        current_lease = str(
            command.get("command_lease_token") or command.get("lease_token") or ""
        )
        if stored_status != "acknowledged" and current_lease:
            result = {
                **result,
                "command_lease_token": current_lease,
                "lease_token": current_lease,
            }
            self.app.db.store_brain_command_result(command_id, result)
        with self.lock:
            self.last_command_at = time.time()
            self.last_command_id = command_id
            self.last_command_type = command_type
        self.record("command", "ok" if result.get("ok") else "error", f"{command_type}: {command_id}", {
            "command_id": command_id,
            "type": command_type,
            "result_status": str(result.get("status") or ""),
        })
        if stored_status != "acknowledged":
            try:
                self.report_command_result(command_id, result)
            except Exception as error:
                self.app.db.fail_brain_command_report(command_id, str(error))
                raise

    def retry_command_results(self) -> None:
        for row in self.app.db.pending_brain_command_results(10):
            self.report_command_result(row["command_id"], row["result"])

    @staticmethod
    def compatible_context_event(source: dict[str, Any]) -> dict[str, Any]:
        """Publish context in both current and legacy brain envelope shapes."""
        event = dict(source)
        product_fields = (
            "goods_id", "goods_name", "goods_url", "goods_thumb_url",
            "goods_price", "goods_spec",
        )
        inquiry_goods = [
            dict(item) for item in (event.get("inquiry_goods") or [])
            if isinstance(item, dict)
        ]
        recent_orders = [
            dict(item) for item in (event.get("recent_orders") or [])
            if isinstance(item, dict)
        ]
        order_info = (
            dict(event.get("order_info") or {})
            if isinstance(event.get("order_info"), dict) else {}
        )
        order_context = (
            dict(event.get("order_context") or {})
            if isinstance(event.get("order_context"), dict) else {}
        )

        for field in product_fields:
            alias = field.replace("goods_", "product_", 1)
            value = event.get(field) or event.get(alias)
            if is_nonempty(value):
                event[field] = value
                event[alias] = value
                order_info.setdefault(field, value)
                order_context.setdefault(field, value)

        if inquiry_goods:
            for target in (order_info, order_context):
                target.setdefault("inquiry_goods", inquiry_goods)
                target.setdefault("products", inquiry_goods)
                target.setdefault("product_count", len(inquiry_goods))
        if recent_orders:
            for target in (order_info, order_context):
                target.setdefault("recent_orders", recent_orders)
                target.setdefault("orders", recent_orders)
                target.setdefault("order_count", len(recent_orders))
                target.setdefault("total_order_count", len(recent_orders))
            if len(recent_orders) == 1:
                order_info.setdefault("selected_order", recent_orders[0])
                order_context.setdefault("selected_order", recent_orders[0])

        if order_info:
            order_info.setdefault("source", "taobao_mtop_context")
            event["order_info"] = order_info
            for key, value in order_info.items():
                order_context.setdefault(key, value)
        if order_context:
            order_context.setdefault("source", "taobao_mtop_context")
            event["order_context"] = order_context

        lookup = (
            dict(event.get("local_context_lookup") or {})
            if isinstance(event.get("local_context_lookup"), dict) else {}
        )
        if order_info or inquiry_goods or recent_orders:
            lookup.setdefault("source", "taobao_mtop_context")
            lookup.setdefault("order_count", len(recent_orders))
            lookup.setdefault("product_count", len(inquiry_goods))
            event["local_context_lookup"] = lookup
        return event

    def upload_events(self, rows: list[dict[str, Any]]) -> set[str]:
        suppressed_rows = [
            row for row in rows
            if brain_event_suppression_reason(row.get("payload", {}))
        ]
        suppressed_ids = {str(row["event_id"]) for row in suppressed_rows}
        if suppressed_rows:
            self.record("event_filter", "ok", f"过滤 {len(suppressed_rows)} 条千牛系统图片", {
                "event_ids": sorted(suppressed_ids),
                "reason": "taobao_system_tps_asset",
            })
        allowed_rows = [
            row for row in rows
            if str(row["event_id"]) not in suppressed_ids
            if self.account_allowed(str(row.get("payload", {}).get("account") or ""))
        ]
        scope_blocked = {
            str(row["event_id"]) for row in rows if row not in allowed_rows
        }
        if scope_blocked:
            self.record("scope", "warn", f"店铺范围过滤 {len(scope_blocked)} 条消息", {
                "event_ids": sorted(scope_blocked),
            })
        if not allowed_rows:
            return scope_blocked | suppressed_ids
        events = []
        for row in allowed_rows:
            event = self.compatible_context_event(row["payload"])
            account = str(event.get("account") or "").strip()
            captured_at = event.get("captured_at")
            if not captured_at:
                try:
                    captured_at = float(event.get("captured_at_ms") or 0) / 1000.0
                except (TypeError, ValueError):
                    captured_at = 0.0
            event.update({
                "type": str(event.get("type") or "message"),
                "shop_id": str(event.get("shop_id") or self.account_shop_id(account)),
                "agent_id": self.agent_id(),
                "captured_at": float(captured_at or time.time()),
            })
            events.append(event)
        payload = self.request("POST", "/api/bridge/v1/events", {
            "agent_id": self.agent_id(),
            "events": events,
        })
        with self.lock:
            self.last_event_response = {
                "accepted": int(payload.get("accepted") or 0),
                "acknowledged": int(payload.get("acknowledged") or 0),
                "ingested_local": int(payload.get("ingested_local") or 0),
                "context_updated": int(payload.get("context_updated") or 0),
                "shadow_queued": int(payload.get("shadow_queued") or 0),
                "shadow_stale_skipped": int(payload.get("shadow_stale_skipped") or 0),
                "scope_rejected": int(payload.get("scope_rejected") or 0),
                "shop_rejected": int(payload.get("shop_rejected") or 0),
                "event_acks": list(payload.get("event_acks") or []),
            }
        for event in events:
            if str(event.get("role") or "").strip().lower() not in {"user", "buyer", "customer"}:
                continue
            self.watch_draft(
                str(event.get("account") or ""),
                str(event.get("buyer_id") or ""),
                str(event.get("original_msg_id") or event.get("msg_id") or ""),
                str(event.get("buyer_nick") or ""),
            )
        acknowledgements = payload.get("event_acks")
        if isinstance(acknowledgements, list):
            return scope_blocked | suppressed_ids | {
                str(item.get("event_id") or "")
                for item in acknowledgements
                if isinstance(item, dict)
                and (
                    item.get("committed") is True
                    or item.get("retryable") is False
                )
            }
        return scope_blocked | suppressed_ids | {str(row["event_id"]) for row in allowed_rows}

    def run_control(self) -> None:
        backoff = 1.0
        next_heartbeat = 0.0
        next_server_status = 0.0
        while not self.app.stop_event.is_set():
            if not self.configured():
                with self.lock:
                    self.registered = False
                    self.state = "disabled"
                self.wakeup.wait(1.0)
                self.wakeup.clear()
                continue
            key = self.configuration_key()
            try:
                if key != self._configuration_key or not self.registered:
                    if self._configuration_key:
                        self.reconnect_count += 1
                    self.register()
                    self._configuration_key = key
                    next_heartbeat = 0.0
                if time.time() >= next_heartbeat:
                    self.heartbeat()
                    next_heartbeat = time.time() + max(
                        1.0, float(self.app.config.get("brain_heartbeat_seconds", 2.0))
                    )
                if time.time() >= next_server_status:
                    self.refresh_server_status()
                    next_server_status = time.time() + 15.0
                backoff = 1.0
                self.app.stop_event.wait(max(0.05, min(0.5, next_heartbeat - time.time())))
            except Exception as error:
                with self.lock:
                    self.last_error = str(error)[:500]
                    self.state = "degraded"
                self.record("brain", "error", self.last_error)
                self.app.stop_event.wait(backoff)
                backoff = min(15.0, backoff * 2.0)

    def run_commands(self) -> None:
        backoff = 0.5
        while not self.app.stop_event.is_set():
            with self.lock:
                ready = self.registered
            if not self.configured() or not ready:
                self.wakeup.wait(0.5)
                self.wakeup.clear()
                continue
            try:
                # 取指令是时延关键路径，必须排在补报结果之前：一条结果 POST 最多
                # 等 brain_request_timeout_seconds，原来它跑在 pull_commands 前面，
                # 一挂住就把本轮取指令一起推迟（表现为"中心派发到桥接收差好几秒"）。
                for command in self.pull_commands():
                    self.dispatch_command(command)
                with self.lock:
                    self.state = "online"
                    self.last_error = ""
                backoff = 0.5
            except Exception as error:
                with self.lock:
                    self.last_error = str(error)[:500]
                    self.state = "degraded"
                self.record("command_poll", "error", self.last_error)
                self.app.stop_event.wait(backoff)
                backoff = min(15.0, backoff * 2.0)
                continue
            # 补报独立隔离：失败只记一条，绝不拖慢取指令循环。
            try:
                self.retry_command_results()
            except Exception as error:
                self.record("command_ack", "error", str(error)[:500])

    # ------------------------------------------------------------------ 事件上报并发池
    # 单线程上报时，一批要等一次完整 HTTP 往返（认领→上传→落库）才开始下一批，
    # 突发进线时尾条要等十几到几十秒。claim_brain_events 的 BEGIN IMMEDIATE +
    # status='sending' 本身就是原子认领，多个 worker 不会领到同一行；
    # finish_brain_events 按 event_id+revision+status 幂等更新，因此并发是安全的。
    def event_upload_worker_count(self) -> int:
        """事件上报并发度。默认 4，收敛到 1..8；=1 时与旧单线程实现等价。"""
        try:
            workers = int(self.app.config.get("event_upload_concurrency", 4) or 4)
        except (TypeError, ValueError):
            workers = 4
        return max(1, min(workers, 8))

    def event_upload_batch_size(self) -> int:
        """单批上送条数。claim_brain_events 内部上限是 100。"""
        try:
            size = int(self.app.config.get("event_upload_batch_size", 100) or 100)
        except (TypeError, ValueError):
            size = 100
        return max(1, min(size, 100))

    def event_upload_loop(self, index: int) -> None:
        """单个上传 worker：原子认领一批 → 上传 → 落库。

        排空语义：只要领到过事件就立刻再领一次，不再等下一次唤醒，突发时多个
        worker 把头批排空；只有队列空（或大脑未就绪）时才短暂挂起。
        """
        wakeup = getattr(self, "event_wakeup", None) or self.wakeup
        backoff = 0.5
        while not self.app.stop_event.is_set():
            if not self.configured() or not self.event_upload_ready():
                wakeup.wait(1.0)
                wakeup.clear()
                continue
            rows = self.app.db.claim_brain_events(
                float(self.app.config.get("brain_event_delay_seconds", 0.2)),
                self.event_upload_batch_size(),
            )
            if not rows:
                # 空转：短暂挂起，enqueue_brain_event 会 set(event_wakeup)。
                wakeup.wait(0.2)
                wakeup.clear()
                continue
            try:
                committed = self.upload_events(rows)
                self.app.db.finish_brain_events(rows, committed)
                if committed:
                    with self.lock:
                        self.last_event_upload_at = time.time()
                        self.last_error = ""
                    self.record("events", "ok", f"大脑确认 {len(committed)} 条消息", {
                        "event_ids": sorted(committed), "request_ms": self.last_request_ms,
                    })
                backoff = 0.5
            except Exception as error:
                message = str(error)[:500]
                self.app.db.finish_brain_events(rows, set(), message)
                with self.lock:
                    self.last_error = message
                    self.state = "degraded"
                self.record("events", "error", message, {
                    "event_ids": [str(row["event_id"]) for row in rows],
                })
                self.app.stop_event.wait(backoff)
                backoff = min(15.0, backoff * 2.0)

    def run_events(self) -> None:
        """事件上报总入口：并发度=1 走旧单线程；>1 起 worker 池，本线程只做监督。"""
        workers = self.event_upload_worker_count()
        if workers <= 1:
            self.event_upload_loop(0)
            return
        for index in range(workers):
            thread = threading.Thread(
                target=self.event_upload_loop,
                args=(index + 1,),
                name=f"brain-event-upload-{index + 1}",
                daemon=True,
            )
            thread.start()
        LOG.info("event upload pool started workers=%s", workers)
        while not self.app.stop_event.is_set():
            self.app.stop_event.wait(1.0)

    def status(self) -> dict[str, Any]:
        with self.lock:
            snapshot = {
                "configured": self.configured(),
                "enabled": bool(self.app.config.get("brain_enabled", False)),
                "state": self.state,
                "registered": self.registered,
                "agent_id": self.agent_id(),
                "server_url": str(self.app.config.get("brain_server_url") or ""),
                "token_configured": bool(brain_workstation_token(self.app.config)),
                "ai_reply_enabled": bool(self.app.config.get("brain_ai_reply_enabled", True)),
                "remote_open_chat_enabled": bool(
                    self.app.config.get("brain_remote_open_chat_enabled", False)
                ),
                "allowed_shop_ids": list(self.allowed_shop_ids),
                "last_register_at": self.last_register_at,
                "last_heartbeat_at": self.last_heartbeat_at,
                "last_event_upload_at": self.last_event_upload_at,
                "last_command_poll_at": self.last_command_poll_at,
                "last_command_at": self.last_command_at,
                "last_command_id": self.last_command_id,
                "last_command_type": self.last_command_type,
                "last_command_poll_count": self.last_command_poll_count,
                "command_thread_alive": self.command_thread.is_alive(),
                "draft_thread_alive": self.draft_thread.is_alive(),
                "draft_watch_count": len(self._draft_watch),
                "last_draft_poll_at": self.last_draft_poll_at,
                "last_draft_sync_at": self.last_draft_sync_at,
                "last_draft_error": self.last_draft_error,
                "draft_imported_count": self.draft_imported_count,
                "last_handoff_poll_at": self.last_handoff_poll_at,
                "last_handoff_sync_at": self.last_handoff_sync_at,
                "last_handoff_error": self.last_handoff_error,
                "brain_handoff_count": self.brain_handoff_count,
                "last_heartbeat_response": dict(self.last_heartbeat_response),
                "last_event_response": dict(self.last_event_response),
                "server_status": dict(self.server_status),
                "last_server_status_at": self.last_server_status_at,
                "server_status_error": self.server_status_error,
                "last_request_ms": self.last_request_ms,
                "request_count": self.request_count,
                "reconnect_count": self.reconnect_count,
                "last_error": self.last_error,
            }
        snapshot["events"] = self.app.db.brain_event_counts()
        snapshot["commands"] = self.app.db.brain_command_counts()
        return snapshot


class BrowserServer:
    def __init__(self, app: "StandaloneBridge"):
        self.app = app
        self.thread = threading.Thread(target=self.run, name="browser-websocket", daemon=True)
        self.connected = 0
        self.total_events = 0
        self.last_seen_at = 0.0
        self.last_diagnostics: dict[str, Any] = {}
        self.connection_diagnostics: dict[Any, dict[str, Any]] = {}
        self.error = ""
        self.connection_lock = threading.Lock()
        self.connection_ready = threading.Event()
        self.connections: dict[Any, threading.Lock] = {}
        self.pending: dict[str, queue.Queue[dict[str, Any]]] = {}

    def start(self) -> None:
        self.thread.start()

    def wait_until_connected(self, timeout: float) -> bool:
        return self.connection_ready.wait(max(0.0, timeout))

    def record_diagnostics(self, connection: Any, diagnostics: dict[str, Any]) -> None:
        allowed = (
            "version", "started_at_ms", "websocket_connected", "imsdk_hooked",
            "last_event_at_ms", "last_capture_at_ms", "pending_events",
            "reconnect_count", "self_heal_count", "known_conversations",
            "event_callbacks", "last_event_arg_count", "last_event_arg_types",
            "last_event_shape", "last_event_ccode_count", "local_lookup_attempts",
            "local_lookup_hits", "local_lookup_misses", "local_db_hits",
            "background_notifications", "has_db", "has_msgDataMap",
            "msgDataMap_size", "msgDataMap_error", "msgdb_last_arrlen",
            "msgdb_last_normalized", "msgdb_last_reason", "msgdb_dump",
            "passive_dom_interval_ms", "passive_cache_interval_ms",
            "recovery_scan_requests", "recovery_scan_runs",
            "recovery_scan_last_reason",
        )
        snapshot = {key: diagnostics.get(key) for key in allowed if key in diagnostics}
        with self.connection_lock:
            self.last_diagnostics = snapshot
            self.connection_diagnostics[connection] = snapshot

    def diagnostics_by_connection(self) -> list[dict[str, Any]]:
        with self.connection_lock:
            return [dict(value) for value in self.connection_diagnostics.values()]

    def allowed(self, connection: Any) -> bool:
        path = str(getattr(getattr(connection, "request", None), "path", ""))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        return query.get("token", [""])[0] == str(self.app.config.get("browser_token"))

    def send(self, connection: Any, payload: dict[str, Any]) -> None:
        with self.connection_lock:
            send_lock = self.connections.get(connection)
        if send_lock is None:
            raise RuntimeError("browser connection is no longer available")
        with send_lock:
            connection.send(json.dumps(payload, ensure_ascii=False))

    def execute(self, expression: str, timeout: float = 3.0) -> Any:
        request_id = uuid.uuid4().hex
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.connection_lock:
            connections = list(self.connections)
            self.pending[request_id] = response_queue
        if not connections:
            with self.connection_lock:
                self.pending.pop(request_id, None)
            raise RuntimeError("Qianniu browser bridge is not connected")
        try:
            sent = 0
            for connection in connections:
                try:
                    self.send(connection, {
                        "method": "execute",
                        "request_id": request_id,
                        "expression": expression,
                    })
                    sent += 1
                except Exception:
                    continue
            if not sent:
                raise RuntimeError("Qianniu browser command could not be sent")
            try:
                response = response_queue.get(timeout=timeout)
            except queue.Empty as error:
                raise RuntimeError("Qianniu browser command timed out") from error
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "Qianniu browser command failed"))
            return response.get("value")
        finally:
            with self.connection_lock:
                self.pending.pop(request_id, None)

    def runtime_snapshot(self) -> dict[str, Any]:
        expression = r"""
(() => {
  const ownKeys = value => {
    try { return value ? Object.getOwnPropertyNames(value).slice(0, 300) : []; }
    catch (_) { return []; }
  };
  const interestingGlobals = ownKeys(window).filter(key =>
    /(conversation|session|im|sdk|dialog|buyer|route|chat)/i.test(key)
  ).slice(0, 200);
  const elements = Array.from(document.querySelectorAll('*')).slice(0, 2500)
    .map(element => {
      const attrs = {};
      for (const attr of Array.from(element.attributes || [])) {
        if (/(ccode|conversation|session|buyer|nick|user|id|key)/i.test(attr.name + '=' + attr.value)) {
          attrs[attr.name] = attr.value;
        }
      }
      const text = String(element.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 100);
      if (!Object.keys(attrs).length && !/(会话|接待|消息)/.test(text)) return null;
      return { tag: element.tagName, attrs, text };
    }).filter(Boolean).slice(0, 250);
  return {
    href: location.href,
    title: document.title,
    currentConversation: window._conversationId || window.__conversationId || null,
    interestingGlobals,
    imsdkKeys: ownKeys(window.imsdk || window.imsdk2 || window._qn_im_sdk),
    qnAbilityCenterType: typeof window.QNAbilityCenter,
    qnAbilityCenterKeys: ownKeys(window.QNAbilityCenter),
    qnAbilityKeys: ownKeys(window.QNAbilityCenter && window.QNAbilityCenter.ability),
    abilityCenterType: typeof window.abilitycenter,
    abilityCenterKeys: ownKeys(window.abilitycenter),
    workbenchType: typeof window.workbench,
    workbenchKeys: ownKeys(window.workbench),
    bodyText: String(document.body && document.body.innerText || '').slice(0, 2000),
    elements,
  };
})()
"""
        value = self.execute(expression, timeout=5.0)
        return value if isinstance(value, dict) else {"value": value}

    def current_conversation(self) -> dict[str, Any]:
        value = self.execute(
            "(() => window._conversationId || window.__conversationId || null)()",
            timeout=3.0,
        )
        return value if isinstance(value, dict) else {}

    def open_conversation(
        self,
        buyer_nick: str,
        expected_ccode: str = "",
        security_uid: str = "",
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        nick = buyer_nick.strip()
        if not nick:
            raise ValueError("buyer_nick is required")
        ability_nick = nick if nick.startswith("cntaobao") else f"cntaobao{nick}"
        ability_param = {
            "nick": ability_nick,
            "bizDomain": "taobao",
            "sceneParam": json.dumps({"toRole": "buyer"}, separators=(",", ":")),
        }
        if security_uid.strip():
            ability_param["securityUID"] = security_uid.strip()
        expression = """
(() => new Promise(resolve => {
  const invoke = () => {
    const center = window.QNAbilityCenter;
    if (!center || !center.ability || typeof center.ability.invoke !== 'function') {
      resolve({ok: false, err: 'Qianniu openChat ability is unavailable'});
      return;
    }
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const timer = setTimeout(() => finish({ok: false, err: 'Qianniu openChat callback timed out'}), 6000);
    try {
      center.ability.invoke({
        cmd: 'openChat',
        param: __PARAM__,
        success(info) {
          clearTimeout(timer);
          finish({ok: true, result: info === undefined ? null : info});
        },
        error(error) {
          clearTimeout(timer);
          let detail = '';
          try { detail = typeof error === 'string' ? error : JSON.stringify(error); }
          catch (_) { detail = String(error); }
          finish({ok: false, err: detail || 'Qianniu openChat rejected'});
        }
      });
    } catch (error) {
      clearTimeout(timer);
      finish({ok: false, err: String(error && error.message ? error.message : error)});
    }
  };
  if (window.QNAbilityCenter) {
    invoke();
    return;
  }
  if (!window.abilitycenter) {
    resolve({ok: false, err: 'Qianniu native abilitycenter object is unavailable'});
    return;
  }
  const existing = document.querySelector('script[data-qn-ability-center]');
  if (existing) {
    existing.addEventListener('load', invoke, {once: true});
    existing.addEventListener('error', () => resolve({ok: false, err: 'Qianniu ability script failed to load'}), {once: true});
    return;
  }
  const script = document.createElement('script');
  script.dataset.qnAbilityCenter = 'v1';
  script.src = '/test1/jssdk-qnability.js';
  script.onload = invoke;
  script.onerror = () => resolve({ok: false, err: 'Qianniu ability script failed to load'});
  document.head.appendChild(script);
}))()
""".replace("__PARAM__", json.dumps(ability_param, ensure_ascii=False))
        result = self.execute(expression, timeout=timeout)
        if not isinstance(result, dict) or not result.get("ok"):
            detail = result.get("err") if isinstance(result, dict) else "invalid openChat response"
            raise RuntimeError(str(detail or "Qianniu openChat failed"))
        current: dict[str, Any] = {}
        if expected_ccode:
            deadline = time.time() + 3.5
            while time.time() < deadline:
                try:
                    current = self.current_conversation()
                except RuntimeError:
                    current = {}
                if str(current.get("ccode") or "") == expected_ccode:
                    break
                time.sleep(0.15)
            if str(current.get("ccode") or "") != expected_ccode:
                raise RuntimeError("Qianniu acknowledged openChat but did not switch to the requested conversation")
        return {
            "ok": True,
            "buyer_nick": nick,
            "ability_nick": ability_nick,
            "opened_ccode": str(current.get("ccode") or ""),
            "result": result.get("result"),
        }

    def handler(self, connection: Any) -> None:
        if not self.allowed(connection):
            connection.close(1008, "invalid token")
            return
        with self.connection_lock:
            self.connections[connection] = threading.Lock()
            self.connection_diagnostics[connection] = {}
            self.connected = len(self.connections)
            self.connection_ready.set()
            self.error = ""
        self.last_seen_at = time.time()
        try:
            for raw in connection:
                self.last_seen_at = time.time()
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                kind = str(message.get("type") or message.get("method") or "")
                if kind == "execute" and message.get("request_id"):
                    request_id = str(message.get("request_id"))
                    try:
                        value = json.loads(str(message.get("response") or "null"))
                    except ValueError:
                        value = message.get("response")
                    ok = not (isinstance(value, dict) and value.get("ok") is False)
                    response = {
                        "ok": ok,
                        "value": value,
                        "error": value.get("err") if isinstance(value, dict) else "",
                    }
                    with self.connection_lock:
                        pending = self.pending.get(request_id)
                    if pending is not None:
                        try:
                            pending.put_nowait(response)
                        except queue.Full:
                            pass
                    continue
                if kind == "chat_event" and isinstance(message.get("payload"), dict):
                    if message["payload"].get("__qn_health__") is True:
                        diagnostics = message["payload"].get("diagnostics") or {}
                        if isinstance(diagnostics, dict):
                            self.record_diagnostics(connection, diagnostics)
                        continue
                    wire_id = str(message.get("event_id") or message["payload"].get("event_id") or "")
                    required = ("account", "buyer_id", "content")
                    if any(not is_nonempty(message["payload"].get(key)) for key in required):
                        self.error = "browser event missing account, buyer_id or content"
                        continue
                    event_id, _changed = self.app.ingest_event(message["payload"])
                    self.total_events += 1
                    self.send(connection, {
                        "type": "chat_ack",
                        "event_id": wire_id or event_id,
                        "canonical_event_id": event_id,
                        "committed": True,
                    })
                elif kind in {"heartbeat", "hi"}:
                    diagnostics = message.get("diagnostics") or message.get("response") or {}
                    if isinstance(diagnostics, dict):
                        self.record_diagnostics(connection, diagnostics)
        except Exception as error:
            self.error = str(error)[:500]
        finally:
            with self.connection_lock:
                self.connections.pop(connection, None)
                self.connection_diagnostics.pop(connection, None)
                self.connected = len(self.connections)
                if not self.connections:
                    self.connection_ready.clear()

    def run(self) -> None:
        try:
            with serve(
                self.handler,
                str(self.app.config.get("ws_host")),
                int(self.app.config.get("ws_port")),
                max_size=4 * 1024 * 1024,
            ) as server:
                server.serve_forever()
        except Exception as error:
            self.error = str(error)[:500]
            LOG.exception("browser WebSocket server stopped")


class ContextEnricher:
    ITEM_METHOD = "mtop.taobao.qianniu.cs.item.record.query"
    ORDER_METHOD = "mtop.taobao.qianniu.cs.trade.query"
    EMPTY_ORDER_RETRIES = 2
    ORDER_CACHE_TTL_SECONDS = 300.0
    MAX_PENDING = 500

    def __init__(self, app: "StandaloneBridge"):
        self.app = app
        self.pending: queue.Queue[str] = queue.Queue(maxsize=self.MAX_PENDING)
        self.lock = threading.RLock()
        self.queued: set[str] = set()
        self.thread = threading.Thread(target=self.run, name="context-enrich", daemon=True)
        self.requests = 0
        self.successes = 0
        self.skipped = 0
        self.last_success_at = 0.0
        self.last_error = ""
        self.last_event_id = ""
        self.last_buyer_encrypt_id = ""
        self.order_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def start(self) -> None:
        self.thread.start()

    @staticmethod
    def buyer_encrypt_id(event: dict[str, Any]) -> str:
        explicit = str(event.get("buyer_encrypt_id") or "").strip()
        if explicit:
            return explicit
        for key in ("buyer_id", "ccode", "account"):
            value = str(event.get(key) or "").strip()
            match = re.match(r"^(\d{6,22})\.1-", value)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def event_order_id(event: dict[str, Any]) -> str:
        for key in ("biz_order_id", "order_id", "bizOrderId", "orderId"):
            value = str(event.get(key) or "").strip()
            if value:
                return value
        order_info = event.get("order_info")
        if isinstance(order_info, dict):
            for key in ("order_id", "biz_order_id", "bizOrderId", "orderId"):
                value = str(order_info.get(key) or "").strip()
                if value:
                    return value
        return ""

    def enqueue(self, event_id: str, event: dict[str, Any]) -> None:
        if not self.app.config.get("context_enrich_enabled", True):
            return
        role = str(event.get("role") or "user").strip().lower()
        if role not in {"user", "buyer", "customer"}:
            return
        encrypt_id = self.buyer_encrypt_id(event)
        if not encrypt_id:
            self.skipped += 1
            return
        with self.lock:
            if event_id in self.queued:
                return
            self.queued.add(event_id)
        # Bound memory without dropping enrichment: a full queue applies
        # backpressure while the original message remains durable in SQLite.
        while True:
            try:
                self.pending.put(event_id, timeout=0.5)
                return
            except queue.Full:
                stop_event = getattr(self.app, "stop_event", None)
                if stop_event is not None and stop_event.is_set():
                    with self.lock:
                        self.queued.discard(event_id)
                    return

    @staticmethod
    def decode(value: Any) -> Any:
        current = value
        for _ in range(5):
            if not isinstance(current, str):
                break
            text = current.strip()
            if not text or text[0] not in "[{":
                break
            try:
                current = json.loads(text)
            except ValueError:
                break
        return current

    @classmethod
    def collect_lists(cls, value: Any, keys: set[str], depth: int = 0) -> list[list[Any]]:
        value = cls.decode(value)
        if depth > 7:
            return []
        output: list[list[Any]] = []
        if isinstance(value, dict):
            for key, child in value.items():
                decoded = cls.decode(child)
                if key in keys and isinstance(decoded, list):
                    output.append(decoded)
                if isinstance(decoded, (dict, list, str)):
                    output.extend(cls.collect_lists(decoded, keys, depth + 1))
        elif isinstance(value, list):
            for child in value[:100]:
                output.extend(cls.collect_lists(child, keys, depth + 1))
        return output

    @staticmethod
    def first(row: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = row.get(key)
            if is_nonempty(value):
                return value
        return ""

    @classmethod
    def parse_items(cls, payload: Any) -> list[dict[str, Any]]:
        lists = cls.collect_lists(payload, {
            "underInquiryItemList", "recentlyBoughtItemList", "footPointItemList",
            "itemList", "items",
        })
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bucket in lists:
            for raw in bucket[:40]:
                row = cls.decode(raw)
                if not isinstance(row, dict):
                    continue
                goods_id = str(cls.first(
                    row, "itemId", "item_id", "auctionId", "auction_id", "goods_id", "id"
                ) or "").strip()
                name = str(cls.first(
                    row, "title", "itemTitle", "auctionTitle", "name", "goods_name"
                ) or "").strip()
                if not goods_id and not name:
                    continue
                identity = goods_id or name
                if identity in seen:
                    continue
                seen.add(identity)
                url = str(cls.first(
                    row, "itemUrl", "auctionUrl", "url", "pcUrl", "goods_url"
                ) or "").strip()
                if goods_id and not url:
                    url = f"https://item.taobao.com/item.htm?id={goods_id}"
                output.append({
                    key: value for key, value in {
                        "goods_id": goods_id,
                        "goods_name": name,
                        "goods_url": url,
                        "goods_thumb_url": str(cls.first(
                            row, "picUrl", "pictUrl", "pic", "image", "goods_thumb_url"
                        ) or ""),
                        "goods_price": str(cls.first(
                            row, "price", "auctionPrice", "itemPrice", "zkFinalPrice", "goods_price"
                        ) or ""),
                        "goods_spec": str(cls.first(
                            row, "sku", "skuText", "props", "goods_spec"
                        ) or ""),
                        "source_list": "item_record",
                    }.items() if is_nonempty(value)
                })
        return output[:8]

    @classmethod
    def parse_orders(cls, payload: Any) -> list[dict[str, Any]]:
        lists = cls.collect_lists(payload, {"orders", "orderList", "list"})
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for bucket in lists:
            for raw in bucket[:30]:
                row = cls.decode(raw)
                if not isinstance(row, dict):
                    continue
                order_id = str(cls.first(row, "bizOrderId", "orderId", "tid", "id") or "").strip()
                if not order_id or order_id in seen:
                    continue
                seen.add(order_id)
                items: list[dict[str, Any]] = []
                raw_items = cls.first(row, "itemList", "items")
                if isinstance(raw_items, list):
                    for raw_item in raw_items[:12]:
                        item = cls.decode(raw_item)
                        if not isinstance(item, dict):
                            continue
                        items.append({
                            key: value for key, value in {
                                "goods_id": str(cls.first(item, "auctionId", "itemId") or ""),
                                "goods_name": str(cls.first(item, "auctionTitle", "title", "itemTitle") or ""),
                                "goods_url": str(cls.first(item, "auctionUrl", "itemUrl", "url") or ""),
                                "goods_thumb_url": str(cls.first(item, "picUrl", "pictUrl", "pic") or ""),
                                "goods_price": str(cls.first(item, "price", "auctionPrice") or ""),
                                "goods_spec": str(cls.first(item, "sku", "skuText") or ""),
                                "refund_status": cls.first(item, "refundStatus"),
                                "pay_status": cls.first(item, "payStatus"),
                                "logistics_status": cls.first(item, "logisticsStatus"),
                                "sub_order_id": str(cls.first(item, "subOrderId") or ""),
                            }.items() if is_nonempty(value)
                        })
                order = {
                    "order_id": order_id,
                    "create_time": cls.first(row, "createTime", "gmtCreate"),
                    "pay_time": cls.first(row, "payTime"),
                    "consign_time": cls.first(row, "consignTime"),
                    "end_time": cls.first(row, "endTime"),
                    "order_price": cls.first(row, "orderPrice", "totalFee"),
                    "post_fee": cls.first(row, "postFee"),
                    "express_company": cls.first(row, "expressCompany"),
                    "express_order_number": cls.first(row, "expressOrderNumber", "invoiceNo"),
                    "after_sale_text": cls.first(row, "afterSaleText"),
                    "category": cls.first(row, "cardTypeText", "category"),
                    "seller_memo": cls.first(row, "sellerMemo"),
                    "under_inquiry": cls.first(row, "underInquiry"),
                    "risk_order": cls.first(row, "riskOrder"),
                    "item_count": len(items),
                    "items": items,
                    "source": "mtop_trade_query",
                }
                output.append({key: value for key, value in order.items() if is_nonempty(value)})
        return output[:6]

    def fetch(self, encrypt_id: str, biz_order_id: str = "", account: str = "") -> dict[str, Any]:
        account = str(account or "").strip()
        cache_key = (account, encrypt_id)
        attempts: list[dict[str, Any]] = []
        found: dict[str, Any] | None = None
        for attempt in range(1 + self.EMPTY_ORDER_RETRIES):
            snapshot = self._fetch_once(encrypt_id, biz_order_id)
            attempts.append(snapshot)
            if snapshot["orders_ok"] and snapshot["orders"]:
                found = snapshot
                break
            if attempt < self.EMPTY_ORDER_RETRIES:
                time.sleep(min(0.6, 0.15 * (attempt + 1)))

        if found is not None:
            status = "found"
            orders = list(found["orders"])
            items = list(found["items"])
            from_cache = False
            self._remember_order(cache_key, orders)
        else:
            status = "confirmed_empty" if (attempts and attempts[-1]["orders_ok"]) else "error_unknown"
            cached = self._cached_order(cache_key) if status == "confirmed_empty" else None
            if cached is not None:
                orders = cached
                items = list(attempts[-1]["items"]) if attempts else []
                from_cache = True
                status = "found"
            else:
                orders = []
                items = list(attempts[-1]["items"]) if attempts else []
                from_cache = False

        last_attempt = (found or attempts[-1]) if attempts else {}
        items_ok = bool(last_attempt.get("items_ok"))
        raw_order_count = max((int(item.get("raw_order_count") or 0) for item in attempts), default=0)
        elapsed_ms = round(sum(float(item.get("elapsed_ms") or 0.0) for item in attempts), 2)
        retry_count = max(0, len(attempts) - 1)
        trace_id = str(last_attempt.get("trace_id") or "")
        ret_code = self._ret_code_for(attempts, found)
        errors = self._errors_for(attempts)
        ok = status in {"found", "confirmed_empty"}

        context_status = {
            "ok": ok,
            "status": status,
            "cache": from_cache,
            "orders_count": len(orders),
            "raw_order_count": raw_order_count,
            "goods_count": len(items),
            "error": errors,
            "provider": "qianniu_standalone_mtop",
            "build_hash": BUILD_HASH,
            "trace_id": trace_id,
            "mtop_api": self.ORDER_METHOD,
            "ret_code": ret_code,
            "elapsed_ms": elapsed_ms,
            "retry_count": retry_count,
        }
        order_info: dict[str, Any] = {
            "source": "taobao_mtop_context",
            "lookup_scope": "buyer_shop_recent_orders",
            "inquiry_goods": items,
            "products": items,
            "recent_orders": orders,
            "orders": orders,
            "product_count": len(items),
            "order_count": len(orders),
            "total_order_count": len(orders),
        }
        if status != "error_unknown":
            order_info.update({
                "context_received": True,
                "no_orders": status == "confirmed_empty",
                "ambiguous": len(orders) > 1,
                "order_status": status,
                "orders_from_cache": from_cache,
            })
        else:
            order_info.update({
                "context_received": False,
                "order_status": status,
            })
        lookup: dict[str, Any] = {
            "ok": ok,
            "status": status,
            "source": "taobao_mtop_context",
            "order_count": len(orders),
            "product_lookup_ok": items_ok,
            "product_count": len(items),
            "orders_from_cache": from_cache,
            "build_hash": BUILD_HASH,
            "trace_id": trace_id,
            "mtop_api": self.ORDER_METHOD,
            "ret_code": ret_code,
            "raw_order_count": raw_order_count,
            "elapsed_ms": elapsed_ms,
            "retry_count": retry_count,
        }
        if errors:
            lookup["error"] = errors[:300]
        enrichment: dict[str, Any] = {
            "buyer_encrypt_id": encrypt_id,
            "context_enrich": context_status,
            "order_info": order_info,
            "order_context": dict(order_info),
            "local_context_lookup": lookup,
        }
        if items:
            enrichment["inquiry_goods"] = items
            enrichment.update(items[0])
            enrichment["chat_scene"] = "item_consult"
            for key, value in items[0].items():
                order_info.setdefault(key, value)
                enrichment["order_context"].setdefault(key, value)
        if orders:
            enrichment["recent_orders"] = orders
            if len(orders) == 1:
                selected = orders[0]
                order_info["selected_order"] = selected
                enrichment["order_context"]["selected_order"] = selected
                for key in (
                    "order_id", "order_price", "create_time", "pay_time", "consign_time",
                    "express_company", "express_order_number", "after_sale_text", "category",
                ):
                    if is_nonempty(selected.get(key)):
                        enrichment[key] = selected[key]
                        order_info.setdefault(key, selected[key])
                        enrichment["order_context"].setdefault(key, selected[key])
            enrichment["chat_scene"] = "order_consult"
        return enrichment

    def _fetch_once(self, encrypt_id: str, biz_order_id: str = "") -> dict[str, Any]:
        trace_id = uuid.uuid4().hex
        started = time.perf_counter()
        expression = """
 (() => {
   if (!window.imsdk || typeof window.imsdk.invoke !== 'function') {
     return {ok: false, err: 'Qianniu imsdk is unavailable'};
   }
   const encryptId = __ENCRYPT_ID__;
   const call = (method, param) => Promise.resolve(window.imsdk.invoke(
     'application.invokeMTopChannelService',
     {method, param, httpMethod: 'post', version: '1.0'}
   )).then(value => ({ok: true, value}), error => ({
     ok: false, error: String(error && error.message ? error.message : error)
   }));
   return Promise.all([
     call('__ITEM_METHOD__', {encryptId}),
     call('__ORDER_METHOD__', {securityBuyerUid: encryptId, bizOrderId: __BIZ_ORDER_ID__})
   ]).then(values => ({ok: true, items: values[0], orders: values[1]}));
 })()
""".replace("__ENCRYPT_ID__", json.dumps(encrypt_id, ensure_ascii=False))
        expression = expression.replace("__BIZ_ORDER_ID__", json.dumps(biz_order_id or "", ensure_ascii=False))
        expression = expression.replace("__ITEM_METHOD__", self.ITEM_METHOD)
        expression = expression.replace("__ORDER_METHOD__", self.ORDER_METHOD)
        result = self.app.browser.execute(
            expression,
            timeout=float(self.app.config.get("context_enrich_timeout_seconds", 6.0)),
        )
        if not isinstance(result, dict):
            raise RuntimeError("Qianniu context response is not an object")
        items_result = result.get("items") if isinstance(result.get("items"), dict) else {}
        orders_result = result.get("orders") if isinstance(result.get("orders"), dict) else {}
        items_ok = items_result.get("ok") is True
        orders_ok = orders_result.get("ok") is True
        items = self.parse_items(items_result.get("value")) if items_ok else []
        orders = self.parse_orders(orders_result.get("value")) if orders_ok else []
        raw_order_count = self._raw_order_count(orders_result.get("value")) if orders_ok else 0
        ret_code = self._mtop_ret_code(orders_result.get("value"))
        errors = [
            str(value.get("error") or "") for value in (items_result, orders_result)
            if value and not value.get("ok")
        ]
        return {
            "trace_id": trace_id,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "items": items,
            "orders": orders,
            "items_ok": items_ok,
            "orders_ok": orders_ok,
            "errors": errors,
            "raw_order_count": raw_order_count,
            "ret_code": ret_code,
        }

    @classmethod
    def _raw_order_count(cls, payload: Any) -> int:
        total = 0
        for bucket in cls.collect_lists(payload, {"orders", "orderList", "list"}):
            for raw in bucket:
                if isinstance(cls.decode(raw), dict):
                    total += 1
        return total

    @classmethod
    def _mtop_ret_code(cls, payload: Any) -> str:
        decoded = cls.decode(payload)
        if not isinstance(decoded, dict):
            return ""

        def pick(container: dict[str, Any]) -> str:
            for key in ("ret", "code", "retCode", "resultCode", "errorCode", "mapping_code"):
                value = container.get(key)
                if not is_nonempty(value):
                    continue
                if isinstance(value, list):
                    joined = ",".join(str(item) for item in value if item not in (None, "", [], {}))
                    return joined[:200] if joined else str(value)[:200]
                return str(value)[:200]
            return ""

        result = pick(decoded)
        if result:
            return result
        data = decoded.get("data")
        if isinstance(data, dict):
            return pick(data)
        return ""

    @staticmethod
    def _errors_for(attempts: list[dict[str, Any]]) -> str:
        return "; ".join(
            error for item in attempts for error in item.get("errors", []) if error
        )

    @classmethod
    def _ret_code_for(cls, attempts: list[dict[str, Any]], found: dict[str, Any] | None) -> str:
        if found is not None:
            return str(found.get("ret_code") or "")
        for item in reversed(attempts):
            if item.get("ret_code"):
                return str(item["ret_code"])
        return ""

    def _remember_order(self, key: tuple[str, str], orders: list[dict[str, Any]]) -> None:
        if not orders:
            return
        with self.lock:
            self.order_cache[key] = {"orders": list(orders), "ts": time.time()}
            if len(self.order_cache) > 512:
                stale = sorted(
                    self.order_cache,
                    key=lambda item: float(self.order_cache[item].get("ts") or 0.0),
                )[: len(self.order_cache) - 512]
                for item in stale:
                    self.order_cache.pop(item, None)

    def _cached_order(self, key: tuple[str, str]) -> list[dict[str, Any]] | None:
        with self.lock:
            entry = self.order_cache.get(key)
            if entry is None:
                return None
            if time.time() - float(entry.get("ts") or 0.0) > self.ORDER_CACHE_TTL_SECONDS:
                return None
            orders = entry.get("orders")
            return list(orders) if isinstance(orders, list) else None

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            try:
                event_id = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                event = self.app.db.event_payload(event_id) or {}
                encrypt_id = self.buyer_encrypt_id(event)
                biz_order_id = self.event_order_id(event)
                account = str(event.get("account") or "").strip()
                self.requests += 1
                enrichment = self.fetch(encrypt_id, biz_order_id, account)
                self.app.db.enrich_event(event_id, enrichment)
                status = str(enrichment.get("context_enrich", {}).get("status") or "")
                if status != "error_unknown":
                    self.successes += 1
                    self.last_success_at = time.time()
                    self.last_error = ""
                else:
                    self.last_error = str(
                        enrichment.get("context_enrich", {}).get("error") or "unknown context error"
                    )[:500]
                self.last_event_id = event_id
                self.last_buyer_encrypt_id = encrypt_id
                brain = getattr(self.app, "brain", None)
                if brain is not None:
                    brain.wakeup.set()
                    getattr(brain, "event_wakeup", brain.wakeup).set()
                    brain.record(
                        "context",
                        "ok" if status != "error_unknown" else "error",
                        f"商品/订单上下文已补全：{event_id}",
                        {
                            "event_id": event_id,
                            "buyer_encrypt_id": encrypt_id,
                            "context_enrich": enrichment.get("context_enrich"),
                        },
                    )
            except Exception as error:
                self.last_error = str(error)[:500]
            finally:
                with self.lock:
                    self.queued.discard(event_id)
                self.pending.task_done()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.app.config.get("context_enrich_enabled", True)),
            "pending": self.pending.qsize(),
            "requests": self.requests,
            "successes": self.successes,
            "skipped": self.skipped,
            "last_success_at": self.last_success_at,
            "last_event_id": self.last_event_id,
            "last_buyer_encrypt_id": self.last_buyer_encrypt_id,
            "last_error": self.last_error,
        }


class CdpInjector:
    def __init__(self, app: "StandaloneBridge"):
        self.app = app
        self.thread = threading.Thread(target=self.run, name="cdp-injector", daemon=True)
        self.cdp_port = 0
        self.cdp_pid = 0
        self.port_owners: dict[int, int] = {}
        self.injected_contexts = 0
        self.last_success_at = 0.0
        self.last_error = ""
        self.launched_pid = 0
        self.host_pid = 0

    def start(self) -> None:
        self.thread.start()

    def qianniu_root(self) -> Path:
        return self.app.config.resolve("qianniu_exe").parent

    def _under_qianniu_root(self, process: psutil.Process) -> bool:
        exe = str(process.info.get("exe") or "")
        if not exe:
            return False
        try:
            Path(exe).resolve().relative_to(self.qianniu_root())
            return True
        except (ValueError, OSError):
            return False

    def host_processes(self) -> list[psutil.Process]:
        """Host renderers belonging to the bundled runtime only."""
        result = []
        for process in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                if str(process.info.get("name") or "").lower() != "alirender.exe":
                    continue
                command = " ".join(process.info.get("cmdline") or [])
                if "--type=" in command:
                    continue
                if not self._under_qianniu_root(process):
                    continue
                result.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return result

    def foreign_qianniu_running(self) -> str:
        """Return the exe path of a non-bundled AliWorkbench.exe, if any."""
        expected = str(self.app.config.resolve("qianniu_exe")).lower()
        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if str(process.info.get("name") or "").lower() != "aliworkbench.exe":
                    continue
                exe = str(process.info.get("exe") or "")
                if exe and exe.lower() != expected:
                    return exe
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return ""

    def maybe_launch(self) -> None:
        if not self.app.config.get("auto_launch_qianniu", False):
            return
        if self.host_processes():
            return
        foreign = self.foreign_qianniu_running()
        if foreign:
            # ponytail: two Qianniu installs on one desktop confuse users and
            # adapters alike — refuse with guidance instead of racing it.
            self.last_error = (
                f"检测到其他安装的千牛正在运行：{foreign}；"
                "请退出该千牛后再启动客服助手"
            )
            return
        executable = self.app.config.resolve("qianniu_exe")
        if not executable.is_file():
            self.last_error = f"Qianniu executable not found: {executable}"
            return
        expected = str(executable).lower()
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                if str(process.info.get("exe") or "").lower() == expected:
                    self.launched_pid = process.pid
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        process = subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.launched_pid = process.pid

    def candidate_ports(self) -> list[int]:
        process_ids = {process.pid for process in self.host_processes()}
        ports = set()
        self.port_owners = {}
        for connection in psutil.net_connections(kind="tcp"):
            try:
                if connection.pid not in process_ids or connection.status != psutil.CONN_LISTEN:
                    continue
                if connection.laddr and connection.laddr.ip in {"127.0.0.1", "0.0.0.0", "::"}:
                    port = int(connection.laddr.port)
                    ports.add(port)
                    self.port_owners[port] = int(connection.pid or 0)
            except (AttributeError, psutil.AccessDenied):
                continue
        return sorted(ports)

    @staticmethod
    def targets(port: int) -> list[dict[str, Any]]:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=0.6) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return payload if isinstance(payload, list) else []

    def inject_target(self, target: dict[str, Any], source: str) -> bool:
        socket_url = str(target.get("webSocketDebuggerUrl") or "")
        if not socket_url:
            return False
        ws_url = (
            f"ws://127.0.0.1:{int(self.app.config.get('ws_port'))}/"
            f"?token={urllib.parse.quote(str(self.app.config.get('browser_token')), safe='')}"
        )
        expression = (
            "(function(){window.__qn_standalone_ws_url=" + json.dumps(ws_url) + ";"
            + "(0,eval)(" + json.dumps(source) + ");"
            + "return !!window.__qn_standalone_bridge_v1_installed;})()"
        )
        connection = websocket.create_connection(socket_url, timeout=2, suppress_origin=True)
        try:
            connection.send(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True, "awaitPromise": False},
            }))
            deadline = time.time() + 2.0
            while time.time() < deadline:
                result = json.loads(connection.recv())
                if result.get("id") != 1:
                    continue
                return bool(result.get("result", {}).get("result", {}).get("value"))
            return False
        finally:
            connection.close()

    def inject_once(self) -> int:
        source = (ROOT / "browser_bridge.js").read_text(encoding="utf-8-sig")
        errors = []
        for port in self.candidate_ports():
            try:
                targets = self.targets(port)
            except Exception:
                continue
            chat_targets = [
                target for target in targets
                if "web_chat-packer/recent.html" in str(target.get("url") or "")
            ]
            if not chat_targets:
                continue
            injected = 0
            for target in chat_targets:
                try:
                    injected += int(self.inject_target(target, source))
                except Exception as error:
                    errors.append(str(error))
            if injected:
                self.cdp_port = port
                self.cdp_pid = self.port_owners.get(port, 0)
                self.injected_contexts = injected
                self.last_success_at = time.time()
                self.last_error = ""
                return injected
        self.injected_contexts = 0
        self.last_error = errors[-1][:500] if errors else "Qianniu chat CDP target not found"
        return 0

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            try:
                self.maybe_launch()
                hosts = self.host_processes()
                self.host_pid = max(hosts, key=lambda item: item.create_time()).pid if hosts else 0
                if self.app.config.get("cdp_injection_enabled", False):
                    self.inject_once()
                else:
                    self.cdp_port = 0
                    self.cdp_pid = 0
                    self.injected_contexts = 0
                    self.last_error = ""
            except Exception as error:
                self.last_error = str(error)[:500]
            self.app.stop_event.wait(float(self.app.config.get("poll_interval_seconds", 2.0)))


class UpdaterGuard:
    def __init__(self, app: "StandaloneBridge"):
        self.app = app
        self.thread = threading.Thread(target=self.run, name="updater-guard", daemon=True)
        self.last_success_at = 0.0
        self.last_error = ""
        self.disable_attempts = 0

    def start(self) -> None:
        self.thread.start()

    def disable_once(self) -> bool:
        task_name = str(self.app.config.get("updater_task_name", "AliUpdater") or "").strip()
        if not task_name:
            raise ValueError("updater_task_name is empty")
        result = subprocess.run(
            ["schtasks.exe", "/Change", "/TN", task_name, "/Disable"],
            capture_output=True,
            text=True,
            timeout=10.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        self.disable_attempts += 1
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            raise RuntimeError(detail[:500])
        self.last_success_at = time.time()
        self.last_error = ""
        return True

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            if not self.app.config.get("disable_updater_task", True):
                self.app.stop_event.wait(5.0)
                continue
            try:
                self.disable_once()
            except Exception as error:
                self.last_error = str(error)[:500]
            self.app.stop_event.wait(float(self.app.config.get("updater_guard_interval_seconds", 30.0)))


class AppBizSendAdapter:
    def __init__(self, app: "StandaloneBridge"):
        self.app = app
        self.thread = threading.Thread(target=self.run, name="appbiz-send-adapter", daemon=True)
        self.session: Any = None
        self.script: Any = None
        self.pid = 0
        self.process_started_at = 0.0
        self.candidate_count = 0
        self.routable_candidate_count = 0
        self.selected_service = ""
        self.selection_reason = ""
        self.last_selection_at = 0.0
        self.observed_calls = 0
        self.observed_ccode_count = 0
        self.received_calls = 0
        self.received_messages = 0
        self.last_receive_at = 0.0
        self.helper_loaded = False
        self.last_observation_at = 0.0
        self.last_arg_lengths: list[int] = []
        self.next_send_token = 1
        self.last_callback_at = 0.0
        self.last_result_code: int | None = None
        self.last_error = ""

    def start(self) -> None:
        self.thread.start()

    def detach(self) -> None:
        script, session = self.script, self.session
        self.script = None
        self.session = None
        self.pid = 0
        self.process_started_at = 0.0
        self.candidate_count = 0
        self.routable_candidate_count = 0
        self.selected_service = ""
        self.selection_reason = ""
        self.last_selection_at = 0.0
        self.helper_loaded = False
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass

    def target_process(self) -> psutil.Process | None:
        expected = str(self.app.config.resolve("qianniu_exe")).lower()

        def is_expected_qianniu(process: psutil.Process) -> bool:
            try:
                return (
                    process.name().lower() == "aliworkbench.exe"
                    and str(process.exe() or "").lower() == expected
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False

        host_pid = self.app.cdp.host_pid
        if host_pid and psutil.pid_exists(host_pid):
            try:
                current = psutil.Process(host_pid).parent()
                root = None
                while current is not None and is_expected_qianniu(current):
                    root = current
                    current = current.parent()
                if root is not None:
                    return root
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        candidates = []
        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if str(process.info.get("name") or "").lower() != "aliworkbench.exe":
                    continue
                if str(process.info.get("exe") or "").lower() == expected:
                    candidates.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return min(candidates, key=lambda item: item.create_time()) if candidates else None

    def on_message(self, message: dict[str, Any], _data: Any) -> None:
        if message.get("type") != "send":
            self.last_error = str(message)[:500]
            return
        payload = message.get("payload") or {}
        event = str(payload.get("event") or "")
        if event == "appbiz_ready":
            self.candidate_count = int(payload.get("candidate_count") or 0)
            self.routable_candidate_count = int(payload.get("routable_candidate_count") or 0)
            self.last_error = ""
        elif event == "appbiz_service_selected":
            self.selected_service = str(payload.get("service") or "")
            self.selection_reason = str(payload.get("reason") or "")
            selected_at_ms = float(payload.get("selected_at_ms") or 0.0)
            self.last_selection_at = selected_at_ms / 1000.0 if selected_at_ms else time.time()
            selection = {
                "pid": self.pid,
                "process_started_at": self.process_started_at,
                "service": self.selected_service,
                "reason": self.selection_reason,
                "candidate_index": int(payload.get("candidate_index") or 0),
                "candidate_count": int(payload.get("candidate_count") or self.candidate_count),
                "selected_at": self.last_selection_at,
            }
            try:
                (ROOT / "state" / "appbiz-current-selection.json").write_text(
                    json.dumps(selection, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError as error:
                self.last_error = f"could not persist AppBiz selection: {error}"[:500]
        elif event in {"appbiz_receive", "appbiz_outgoing"}:
            event_payload = payload.get("message")
            if not isinstance(event_payload, dict):
                return
            if event == "appbiz_receive":
                self.received_messages += 1
                self.last_receive_at = time.time()
            elif getattr(getattr(self.app, "browser", None), "connected", 0) > 0:
                return
            clean_event = dict(event_payload)
            if not clean_event.get("account"):
                clean_event["account"] = self.app.db.account_for_buyer(
                    str(clean_event.get("buyer_id") or "")
                )
            try:
                self.app.ingest_event(clean_event)
                self.last_error = ""
            except Exception as error:
                self.last_error = f"AppBiz message ingest failed: {error}"[:500]
        elif event == "appbiz_receive_error":
            self.last_error = str(payload.get("error") or event)[:500]
        elif event == "appbiz_send_observed":
            self.selected_service = str(payload.get("service") or "")
            self.observed_calls += 1
            values = payload.get("args") if isinstance(payload.get("args"), list) else []
            self.last_observation_at = time.time()
            self.last_arg_lengths = [len(value.encode("utf-8")) if isinstance(value, str) else -1 for value in values]
            observation = {
                "observed_at": self.last_observation_at,
                "pid": self.pid,
                "process_started_at": self.process_started_at,
                "service": self.selected_service,
                "candidate_index": int(payload.get("candidate_index") or 0),
                "candidate_count": int(payload.get("candidate_count") or self.candidate_count),
                "args": [
                    {
                        "index": index + 1,
                        "utf8_length": len(value.encode("utf-8")) if isinstance(value, str) else -1,
                        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else "",
                    }
                    for index, value in enumerate(values)
                ],
                "metadata_first_96_bytes": payload.get("metadata_first_96_bytes"),
                "callback_first_64_bytes": payload.get("callback_first_64_bytes"),
                "plaintext_stored": False,
            }
            target = ROOT / "state" / "appbiz-last-observation.json"
            target.write_text(json.dumps(observation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.last_error = ""

    def attach(self, process: psutil.Process) -> None:
        helper_path = self.app.config.resolve("appbiz_adapter_path")
        if not helper_path.is_file():
            raise FileNotFoundError(f"AppBiz adapter not found: {helper_path}")
        template = (ROOT / "appbiz_agent.js").read_text(encoding="utf-8")
        selected_service_hint = None
        process_started_at = process.create_time()
        for selection_path in (
            ROOT / "state" / "appbiz-current-selection.json",
            ROOT / "state" / "appbiz-last-observation.json",
        ):
            try:
                selection = json.loads(selection_path.read_text(encoding="utf-8"))
                if (
                    int(selection.get("pid") or 0) == process.pid
                    and abs(float(selection.get("process_started_at") or 0.0) - process_started_at) < 0.5
                    and str(selection.get("reason") or "") in {
                        "message_arrive",
                        "official_send",
                        "singlemsg_getnewmsg",
                    }
                ):
                    selected_service_hint = str(selection.get("service") or "") or None
                    if selected_service_hint:
                        break
            except (OSError, ValueError, TypeError):
                continue
        source = template.replace(
            "__APPBIZ_ADAPTER_PATH__", json.dumps(str(helper_path))
        ).replace("__SELECTED_SERVICE__", json.dumps(selected_service_hint))
        session = frida.get_local_device().attach(process.pid)
        script = None
        try:
            script = session.create_script(source)
            script.on("message", self.on_message)
            script.load()
        except Exception:
            if script is not None:
                try:
                    script.unload()
                except Exception:
                    pass
            try:
                session.detach()
            except Exception:
                pass
            raise
        self.pid = process.pid
        self.process_started_at = process_started_at
        self.session = session
        self.script = script
        self.refresh_status()

    def refresh_status(self) -> None:
        if self.script is None:
            return
        status = self.script.exports_sync.status()
        self.candidate_count = int(status.get("candidate_count") or 0)
        self.routable_candidate_count = int(status.get("routable_candidate_count") or 0)
        self.selected_service = str(status.get("selected_service") or "")
        self.selection_reason = str(status.get("selection_reason") or "")
        selected_at_ms = float(status.get("selected_at_ms") or 0.0)
        self.last_selection_at = selected_at_ms / 1000.0 if selected_at_ms else 0.0
        self.observed_calls = int(status.get("observed_calls") or self.observed_calls)
        self.observed_ccode_count = int(status.get("observed_ccode_count") or 0)
        self.received_calls = int(status.get("received_calls") or self.received_calls)
        self.received_messages = int(status.get("received_messages") or self.received_messages)
        received_at_ms = float(status.get("last_receive_at_ms") or 0.0)
        if received_at_ms:
            self.last_receive_at = received_at_ms / 1000.0
        self.helper_loaded = bool(status.get("helper_loaded"))

    @property
    def ready(self) -> bool:
        return bool(
            self.script is not None
            and self.candidate_count > 0
            and self.app.config.get("appbiz_send_abi_validated", False)
        )

    @property
    def route_ready(self) -> bool:
        trusted_selection = bool(
            self.selected_service
            and self.selection_reason in {
                "message_arrive",
                "official_send",
                "same_process_persisted",
                "singlemsg_getnewmsg",
            }
        )
        return bool(
            self.script is not None
            and trusted_selection
            and self.app.config.get("appbiz_send_abi_validated", False)
        )

    def send_text(self, ccode: str, content: str, pcsource: str) -> dict[str, Any]:
        if not self.app.config.get("appbiz_send_abi_validated", False):
            raise RuntimeError("AppBiz send ABI has not passed a tagged acceptance test")
        if not self.route_ready or self.script is None:
            raise RuntimeError(self.last_error or "AppBiz send service is not selected")
        token = ((self.pid & 0xFFFFFFFF) << 32) | (self.next_send_token & 0xFFFFFFFF)
        self.next_send_token = (self.next_send_token + 1) & 0xFFFFFFFF
        if self.next_send_token == 0:
            self.next_send_token = 1
        result = int(self.script.exports_sync.sendtext(ccode, content, pcsource, str(token)))
        self.refresh_status()
        if result != 0:
            raise RuntimeError(f"AppBiz adapter rejected send with code {result}")
        callback_received = False
        result_code: int | None = None
        wait_seconds = max(
            0.0,
            float(self.app.config.get("appbiz_callback_wait_seconds", 2.0)),
        )
        deadline = time.monotonic() + wait_seconds
        started = time.monotonic()
        while self.script is not None and time.monotonic() <= deadline:
            receipt = self.script.exports_sync.pollsend(str(token))
            state = int(receipt.get("state") or 0)
            if state == 1:
                callback_received = True
                result_code = int(receipt.get("result_code"))
                self.last_callback_at = time.time()
                self.last_result_code = result_code
                break
            if state < 0:
                break
            # Each poll is a Frida RPC into the client process, and the send pool
            # can have several senders waiting at once: poll tightly only for the
            # first half second, then back off so the adapter never becomes a
            # cross-process busy loop.
            elapsed = time.monotonic() - started
            time.sleep(0.025 if elapsed < 0.5 else 0.1)
        if not callback_received and self.script is not None:
            try:
                self.script.exports_sync.cancelsend(str(token))
            except Exception:
                pass
        if callback_received and result_code not in {0, -2147483648}:
            if result_code == 5:
                raise RuntimeError(
                    "Qianniu MessageSDK rejected send: conversation context is not ready "
                    "(result code 5, CheckConvExist_error)"
                )
            raise RuntimeError(f"Qianniu MessageSDK rejected send with result code {result_code}")
        return {
            "native_submitted": True,
            "callback_received": callback_received,
            "result_code": result_code,
        }

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            try:
                if not self.app.config.get("appbiz_send_adapter_enabled", True):
                    self.detach()
                    self.app.stop_event.wait(2.0)
                    continue
                if self.pid and psutil.pid_exists(self.pid) and self.script is not None:
                    self.refresh_status()
                    self.app.stop_event.wait(1.0)
                    continue
                self.detach()
                target = self.target_process()
                if target is None:
                    self.last_error = "active AliWorkbench process not found"
                    self.app.stop_event.wait(2.0)
                    continue
                self.attach(target)
            except Exception as error:
                self.last_error = str(error)[:500]
                self.detach()
                self.app.stop_event.wait(2.0)
        self.detach()


class NativeAdapter:
    def __init__(self, app: "StandaloneBridge"):
        self.app = app
        self.thread = threading.Thread(target=self.run, name="native-adapter", daemon=True)
        self.session: Any = None
        self.script: Any = None
        self.pid = 0
        self.ready = False
        self.conflict = False
        self.last_error = ""
        self.last_event_at = 0.0

    def start(self) -> None:
        self.thread.start()

    @staticmethod
    def foreign_plugin_present(process: psutil.Process) -> bool:
        try:
            return any(
                Path(item.path).name.lower() == "insideplugin_taobao_x64.dll"
                for item in process.memory_maps(grouped=False)
                if item.path
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def detach(self) -> None:
        script, session = self.script, self.session
        self.script = None
        self.session = None
        self.ready = False
        self.pid = 0
        if script is not None:
            try:
                script.exports_sync.shutdown()
            except Exception:
                pass
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass

    def on_message(self, message: dict[str, Any], _data: Any) -> None:
        if message.get("type") != "send":
            self.last_error = str(message)[:500]
            return
        payload = message.get("payload") or {}
        event = str(payload.get("event") or "")
        if event == "native_receive":
            self.last_event_at = time.time()
            self.app.ingest_native_raw(str(payload.get("raw") or ""))
        elif event == "native_ready":
            self.ready = True
            self.conflict = False
            self.last_error = ""
        elif event == "native_conflict":
            self.conflict = True
            self.ready = False
            self.last_error = "foreign plugin already owns qnmsg callback"
        elif event in {"native_error", "native_receive_error"}:
            self.last_error = str(payload.get("error") or event)[:500]

    def attach(self, process: psutil.Process) -> None:
        plugin_path = self.app.config.resolve("native_plugin_path")
        if not plugin_path.is_file():
            raise FileNotFoundError(f"native plugin not found: {plugin_path}")
        template = (ROOT / "native_agent.js").read_text(encoding="utf-8")
        source = template.replace("__QN_PLUGIN_PATH__", json.dumps(str(plugin_path)))
        source = source.replace(
            "__QN_ALLOW_EXISTING_PLUGIN__",
            "true" if self.app.config.get("allow_existing_foreign_plugin", False) else "false",
        )
        session = frida.get_local_device().attach(process.pid)
        script = None
        try:
            script = session.create_script(source)
            script.on("message", self.on_message)
            script.load()
        except Exception:
            if script is not None:
                try:
                    script.unload()
                except Exception:
                    pass
            try:
                session.detach()
            except Exception:
                pass
            raise
        self.session = session
        self.script = script
        self.pid = process.pid
        self.conflict = False

    def send_text(self, seller: str, buyer: str, content: str, is_set_time: bool) -> bool:
        if not self.ready or self.script is None:
            raise RuntimeError(self.last_error or "native adapter not ready")
        return bool(self.script.exports_sync.sendtext(seller, buyer, content, is_set_time))

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            try:
                if not self.app.config.get("native_adapter_enabled", True):
                    self.detach()
                    self.app.stop_event.wait(2.0)
                    continue
                if self.pid and psutil.pid_exists(self.pid):
                    self.app.stop_event.wait(1.0)
                    continue
                self.detach()
                hosts = self.app.cdp.host_processes()
                if not hosts:
                    self.last_error = "Qianniu AliRender host not found"
                    self.app.stop_event.wait(2.0)
                    continue
                host = next(
                    (item for item in hosts if item.pid in {self.app.cdp.cdp_pid, self.app.cdp.host_pid}),
                    max(hosts, key=lambda item: item.create_time()),
                )
                if self.foreign_plugin_present(host) and not self.app.config.get(
                    "allow_existing_foreign_plugin", False
                ):
                    self.conflict = True
                    self.last_error = "foreign plugin already owns qnmsg callback"
                    self.app.stop_event.wait(2.0)
                    continue
                self.attach(host)
            except Exception as error:
                self.last_error = str(error)[:500]
                self.detach()
                self.app.stop_event.wait(2.0)
        self.detach()


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "QianniuStandaloneWorkbench/1.0"

    @property
    def app(self) -> "StandaloneBridge":
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args: Any) -> None:
        LOG.debug("workbench %s", format_string % args)

    def send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        self.send_bytes(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 128 * 1024:
            raise ValueError("invalid request body length")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def workbench_authorized(self) -> bool:
        expected = str(self.app.config.get("workbench_token") or "")
        return bool(expected and self.headers.get("X-Workbench-Token") == expected)

    def gateway_authorized(self) -> bool:
        expected_token = str(self.app.config.get("gateway_agent_token") or "")
        expected_agent = str(self.app.config.get("gateway_agent_id") or "")
        return bool(
            expected_token
            and expected_agent
            and self.headers.get("X-Agent-Token") == expected_token
            and self.headers.get("X-Agent-Id") == expected_agent
        )

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            try:
                page = (ROOT / "workbench.html").read_text(encoding="utf-8")
                token = html.escape(str(self.app.config.get("workbench_token")), quote=True)
                page = page.replace("__WORKBENCH_TOKEN__", token)
                self.send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")
            except OSError as error:
                self.send_json(500, {"ok": False, "error": str(error)[:300]})
            return
        if path == "/favicon.ico":
            self.send_bytes(204, b"", "image/x-icon")
            return
        if not self.workbench_authorized():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        if path == "/api/v1/status":
            sessions = self.app.db.workbench_sessions()
            self.send_json(200, {
                "ok": True,
                "version": VERSION,
                "build_hash": BUILD_HASH,
                "bridge": self.app.status(workbench_session_count=len(sessions)),
                "sessions": len(sessions),
            })
            return
        if path == "/api/v1/sessions":
            sessions = self.app.db.workbench_sessions()
            self.send_json(200, {"ok": True, "sessions": sessions, "total": len(sessions)})
            return
        if path == "/api/v1/session":
            query = urllib.parse.parse_qs(parsed.query)
            account = str(query.get("account", [""])[0]).strip()
            buyer_id = str(query.get("buyer_id", [""])[0]).strip()
            if not account or not buyer_id:
                self.send_json(400, {"ok": False, "error": "account and buyer_id are required"})
                return
            self.app.expire_unconfirmed_sends()
            messages = self.app.db.workbench_messages(account, buyer_id)
            self.send_json(200, {
                "ok": True,
                "account": account,
                "buyer_id": buyer_id,
                "messages": messages,
                "total": len(messages),
            })
            return
        if path == "/api/v1/brain/config":
            self.send_json(200, {
                "ok": True,
                "config": self.app.config.public_brain_settings(),
            })
            return
        if path == "/api/v1/diagnostics":
            self.send_json(200, {
                "ok": True,
                "bridge": self.app.status(),
                "brain_activity": self.app.brain.activity(),
                "recent_commands": self.app.db.recent_brain_commands(),
            })
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/local-seat/v1/events":
            if not self.gateway_authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                body = self.read_json()
                events = body.get("events")
                if not isinstance(events, list) or not events:
                    raise ValueError("events must be a non-empty list")
                acknowledgements = []
                for event in events[:100]:
                    if not isinstance(event, dict):
                        continue
                    event_id = canonical_event_id(event)
                    status = self.app.db.event_status(event_id)
                    acknowledgements.append({
                        "event_id": event_id,
                        "committed": status in {"sending", "delivered"},
                    })
                if not acknowledgements or not all(row["committed"] for row in acknowledgements):
                    raise RuntimeError("event batch is not durable in the standalone database")
                self.send_json(200, {"ok": True, "event_acks": acknowledgements})
            except (ValueError, RuntimeError) as error:
                self.send_json(400, {"ok": False, "error": str(error)})
            return
        if path == "/api/v1/brain/config":
            if not self.workbench_authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                settings = self.app.config.update_brain_settings(self.read_json())
                self.app.brain.configuration_changed()
                self.send_json(200, {"ok": True, "config": settings})
            except (OSError, ValueError) as error:
                self.send_json(400, {"ok": False, "error": str(error)})
            return
        if path == "/api/v1/brain/test":
            if not self.workbench_authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                payload = self.app.brain.register()
                self.send_json(200, {
                    "ok": True,
                    "message": "大脑连接和大脑工位令牌验证成功",
                    "brain": self.app.brain.status(),
                    "register": payload,
                })
            except (OSError, RuntimeError, ValueError) as error:
                self.send_json(502, {"ok": False, "error": str(error)})
            return
        if path == "/api/v1/session/control":
            if not self.workbench_authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                body = self.read_json()
                mode = str(body.get("mode") or "").strip().lower()
                control = self.app.db.set_session_control(
                    str(body.get("account") or ""),
                    str(body.get("buyer_id") or ""),
                    mode,
                    str(body.get("reason") or ("人工接管" if mode == "human" else "")),
                    "manual",
                )
                self.app.brain.record(
                    "handoff",
                    "warn" if mode == "human" else "ok",
                    "会话已转人工" if mode == "human" else "会话已恢复 AI",
                    control,
                )
                self.send_json(200, {"ok": True, "control": control})
            except ValueError as error:
                self.send_json(400, {"ok": False, "error": str(error)})
            return
        if path == "/api/v1/reply":
            if not self.workbench_authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            if not self.app.config.get("send_enabled", False):
                self.send_json(403, {"ok": False, "error": "send_disabled"})
                return
            try:
                body = self.read_json()
                body.setdefault("request_id", f"workbench-{uuid.uuid4().hex}")
                response = self.app.send_text(body)
                response_status = str(response.get("status") or "")
                status_code = 202 if response_status in {"in_flight", "submitted"} else (
                    200 if response.get("ok") else 502
                )
                self.send_json(status_code, response)
            except (ValueError, RuntimeError) as error:
                self.send_json(400, {"ok": False, "error": str(error)})
            return
        if path == "/api/v1/open-conversation":
            if not self.workbench_authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                body = self.read_json()
                account = str(body.get("account") or "").strip()
                buyer_id = str(body.get("buyer_id") or "").strip()
                requested_nick = str(body.get("buyer_nick") or "").strip()
                if not account or not buyer_id:
                    raise ValueError("account and buyer_id are required")
                session = next(
                    (
                        item for item in self.app.db.workbench_sessions()
                        if str(item.get("account") or "") == account
                        and str(item.get("buyer_id") or "") == buyer_id
                    ),
                    None,
                )
                if session is None:
                    raise ValueError("conversation is not present in the local workbench")
                buyer_nick = str(session.get("buyer_nick") or requested_nick or "").strip()
                if not buyer_nick:
                    raise ValueError("conversation has no buyer nickname")
                focused = self.app.focus_qianniu()
                if self.app.browser.connected:
                    security_uid = ContextEnricher.buyer_encrypt_id({"buyer_id": buyer_id})
                    response = self.app.browser.open_conversation(
                        buyer_nick,
                        buyer_id,
                        security_uid=security_uid,
                    )
                else:
                    response = self.app.open_conversation_protocol(account, buyer_nick)
                response["focused"] = focused
                response.update(account=account, buyer_id=buyer_id)
                self.send_json(200, response)
            except ValueError as error:
                self.send_json(400, {"ok": False, "error": str(error)})
            except RuntimeError as error:
                self.send_json(502, {"ok": False, "error": str(error)})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "QianniuStandaloneBridge/1.0"

    @property
    def app(self) -> "StandaloneBridge":
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args: Any) -> None:
        LOG.debug("api %s", format_string % args)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        expected = str(self.app.config.get("api_token"))
        bearer = str(self.headers.get("Authorization") or "")
        token = bearer[7:] if bearer.startswith("Bearer ") else str(self.headers.get("X-Bridge-Token") or "")
        return bool(expected and token == expected)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 128 * 1024:
            raise ValueError("invalid request body length")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/health", "/api/v1/status"}:
            self.send_json(200, self.app.status())
        else:
            self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/v1/shutdown":
            if not self.authorized():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self.send_json(202, {"ok": True, "status": "stopping"})
            self.app.stop_event.set()
            return
        if path != "/api/v1/send/text":
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self.authorized():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        if not self.app.config.get("send_enabled", False):
            self.send_json(403, {"ok": False, "error": "send_disabled"})
            return
        try:
            body = self.read_json()
            response = self.app.send_text(body)
            response_status = str(response.get("status") or "")
            status_code = 202 if response_status in {"in_flight", "submitted"} else (
                200 if response.get("ok") else 502
            )
            self.send_json(status_code, response)
        except (ValueError, RuntimeError) as error:
            self.send_json(400, {"ok": False, "error": str(error)})


class StandaloneBridge:
    def __init__(self, config: Config):
        self.config = config
        self.stop_event = threading.Event()
        self.started_at = time.time()
        self.db = StateDB(ROOT / "state" / "bridge.sqlite3")
        self.delivery = DeliveryWorker(self)
        self.brain = BrainConnector(self)
        self.browser = BrowserServer(self)
        self.context = ContextEnricher(self)
        self.cdp = CdpInjector(self)
        self.updater = UpdaterGuard(self)
        self.appbiz = AppBizSendAdapter(self)
        self.native = NativeAdapter(self)
        # Qianniu upgrades itself in place; this watch keeps a re-injected
        # webui.zip and surfaces builds the agent has no profile for.
        self.client_support = ClientSupportWatcher(
            ROOT,
            config,
            self.stop_event,
            interval_seconds=float(config.get("client_support_interval_seconds", 600.0)),
        )
        self.api_thread = threading.Thread(target=self.run_api, name="local-api", daemon=True)
        self.workbench_thread = threading.Thread(
            target=self.run_workbench, name="local-workbench", daemon=True
        )
        self.api_error = ""
        self.workbench_error = ""
        self.native_frames = 0

    def open_conversation_protocol(self, account: str, buyer_nick: str) -> dict[str, Any]:
        account = str(account or "").strip()
        buyer_nick = str(buyer_nick or "").strip()
        if not account or not buyer_nick:
            raise ValueError("account and buyer_nick are required")
        if buyer_nick_is_placeholder(buyer_nick):
            raise RuntimeError("conversation has no usable buyer nickname")
        runtime_root = self.config.resolve("qianniu_exe").parent
        candidates = sorted(runtime_root.rglob("WWCmd.exe"), reverse=True)
        if not candidates:
            raise RuntimeError("Qianniu WWCmd.exe is unavailable")
        query = urllib.parse.urlencode({
            "uid": f"cntaobao{account}",
            "touid": f"cntaobao{buyer_nick}",
            "siteid": "cntaobao",
            "status": "1",
        })
        completed = subprocess.run(
            [str(candidates[0]), f"aliim:sendmsg?{query}"],
            cwd=str(candidates[0].parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5.0,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Qianniu WWCmd failed with code {completed.returncode}")
        return {
            "ok": True,
            "buyer_nick": buyer_nick,
            "via": "qianniu_wwcmd",
            "verified": False,
        }

    def focus_qianniu(self) -> bool:
        if os.name != "nt":
            return False
        from ctypes import wintypes

        expected = str(self.config.resolve("qianniu_exe")).lower()
        pids: set[int] = set()
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                if str(process.info.get("exe") or "").lower() == expected:
                    pids.add(int(process.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not pids:
            return False

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        candidates: list[tuple[int, str, int]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def visit(hwnd: int, _lparam: int) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) not in pids or not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area >= 350_000:
                candidates.append((int(hwnd), title_buffer.value, area))
            return True

        user32.EnumWindows(visit, 0)
        if not candidates:
            return False
        preferred = [item for item in candidates if "接待中心" in item[1]]
        target = max(preferred or candidates, key=lambda item: item[2])
        hwnd = target[0]
        user32.ShowWindow(hwnd, 9)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))

    def ingest_event(self, event: dict[str, Any]) -> tuple[str, bool]:
        normalized = normalize_event(event)
        if not self.config.get("delivery_enabled", True):
            return canonical_event_id(normalized), False
        role = str(normalized.get("role") or "user").strip().lower()
        account = str(normalized.get("account") or "").strip()
        buyer_id = str(normalized.get("buyer_id") or "").strip()
        brain_suppression = brain_event_suppression_reason(normalized)
        if brain_suppression:
            normalized.update({
                "brain_suppressed": True,
                "brain_suppression_reason": brain_suppression,
            })
        if role in {"user", "buyer", "customer"} and account and buyer_id:
            reason = suggested_handoff_reason(normalized)
            control = self.db.session_control(account, buyer_id)
            if reason and control["ai_mode"] == "ai":
                self.db.set_session_control(
                    account,
                    buyer_id,
                    "human",
                    reason,
                    "automatic",
                    float(self.config.get("brain_handoff_ttl_seconds", 3600.0)),
                )
                control = self.db.session_control(account, buyer_id)
            if control["ai_mode"] == "human":
                normalized.update({
                    "handoff": True,
                    "handoff_reason": control["handoff_reason"],
                    "ai_suppressed": True,
                })
        event_id, changed = self.db.upsert_event(normalized)
        confirmed_request = self.db.confirm_send_from_event(normalized, event_id)
        if confirmed_request:
            LOG.info("Qianniu confirmed send request_id=%s event_id=%s", confirmed_request, event_id)
        if changed:
            self.delivery.wakeup.set()
            context = getattr(self, "context", None)
            if context is not None:
                context.enqueue(event_id, normalized)
            brain = getattr(self, "brain", None)
            if brain is not None and brain.configured():
                if brain_suppression:
                    brain.record("event_filter", "ok", "千牛系统图片未上报大脑", {
                        "event_id": event_id,
                        "msg_id": str(normalized.get("original_msg_id") or normalized.get("msg_id") or ""),
                        "reason": brain_suppression,
                        "content": str(normalized.get("content") or "")[:300],
                    })
                elif self.db.enqueue_brain_event(event_id):
                    brain.wakeup.set()
                    getattr(brain, "event_wakeup", brain.wakeup).set()
        return event_id, changed

    def ingest_native_raw(self, raw: str) -> None:
        self.native_frames += 1
        for event in normalize_native_frame(raw):
            self.ingest_event(event)

    def send_text(
        self, body: dict[str, Any], *, brain_authorized: bool = False
    ) -> dict[str, Any]:
        if not self.config.get("send_enabled", False):
            raise RuntimeError("real sending is disabled by configuration")
        if not brain_authorized:
            raise RuntimeError("real sending requires a backend brain command")
        ccode = str(body.get("buyer_cid") or body.get("ccode") or "").strip()
        content = str(body.get("content") or body.get("text") or "")
        request_id = str(body.get("request_id") or "").strip()
        if not ccode or not content or not request_id:
            raise ValueError("buyer_cid (full ccode), content and request_id are required")
        if "#" not in ccode or "@" not in ccode:
            raise ValueError("buyer_cid must be a full Qianniu ccode")
        if len(content) > 4000 or len(ccode) > 512 or len(request_id) > 256:
            raise ValueError("send request exceeds length limit")
        stable = json_text({"buyer_cid": ccode, "content": content})
        payload_hash = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        self.expire_unconfirmed_sends()
        claimed, existing = self.db.claim_send(request_id, payload_hash, ccode, content)
        if not claimed and existing:
            if existing["payload_hash"] != payload_hash:
                raise ValueError("request_id was already used with a different payload")
            return json.loads(existing["response"])
        if not claimed:
            raise RuntimeError("send request could not be claimed")
        pcsource = f"QianniuStandaloneBridge/{VERSION}"
        try:
            # Never fetch new messages to prepare a send: GetNewMsg can advance
            # Qianniu's UI cursor. The passive AppBiz observation must already
            # have a route, otherwise sending fails safely.
            native_receipt = self.appbiz.send_text(ccode, content, pcsource)
        except Exception as error:
            self.db.mark_send_rejected(request_id, payload_hash, str(error))
            raise
        response = self.db.mark_send_submitted(request_id, payload_hash)
        if (
            isinstance(native_receipt, dict)
            and native_receipt.get("callback_received") is True
            and native_receipt.get("result_code") == 0
        ):
            response = self.db.confirm_send_from_callback(request_id, payload_hash, 0)
        return response

    def expire_unconfirmed_sends(self) -> int:
        return self.db.expire_unconfirmed_sends(
            float(self.config.get("send_confirmation_timeout_seconds", 15.0))
        )

    def run_api(self) -> None:
        try:
            server = ThreadingHTTPServer(
                (str(self.config.get("api_host")), int(self.config.get("api_port"))), ApiHandler
            )
            server.app = self  # type: ignore[attr-defined]
            server.serve_forever(poll_interval=0.5)
        except Exception as error:
            self.api_error = str(error)[:500]
            LOG.exception("local API stopped")

    def run_workbench(self) -> None:
        try:
            server = ThreadingHTTPServer(
                (
                    str(self.config.get("workbench_host")),
                    int(self.config.get("workbench_port")),
                ),
                WorkbenchHandler,
            )
            server.app = self  # type: ignore[attr-defined]
            server.serve_forever(poll_interval=0.5)
        except Exception as error:
            self.workbench_error = str(error)[:500]
            LOG.exception("local workbench stopped")

    def start(self) -> None:
        self.workbench_thread.start()
        self.delivery.start()
        self.brain.start()
        self.browser.start()
        self.context.start()
        self.cdp.start()
        self.updater.start()
        self.appbiz.start()
        self.native.start()
        self.client_support.start()
        self.api_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        # These workers own Frida sessions and perform their detach cleanup
        # after stop_event interrupts their retry waits.
        self.appbiz.thread.join(timeout=3.0)
        self.native.thread.join(timeout=3.0)

    def status(
        self,
        include_brain: bool = True,
        workbench_session_count: int | None = None,
    ) -> dict[str, Any]:
        self.expire_unconfirmed_sends()
        if workbench_session_count is None:
            workbench_session_count = len(self.db.workbench_sessions())
        payload = {
            "ok": not bool(self.api_error or self.workbench_error or self.browser.error),
            "version": VERSION,
            "build_hash": BUILD_HASH,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "browser": {
                "connected": self.browser.connected,
                "total_events": self.browser.total_events,
                "last_seen_at": self.browser.last_seen_at,
                "diagnostics": self.browser.last_diagnostics,
                "connections": self.browser.diagnostics_by_connection(),
                "error": self.browser.error,
            },
            "context_enrich": self.context.status(),
            "cdp": {
                "enabled": bool(self.config.get("cdp_injection_enabled", False)),
                "port": self.cdp.cdp_port,
                "pid": self.cdp.cdp_pid,
                "host_pid": self.cdp.host_pid,
                "injected_contexts": self.cdp.injected_contexts,
                "last_success_at": self.cdp.last_success_at,
                "last_error": self.cdp.last_error,
            },
            "updater_guard": {
                "enabled": bool(self.config.get("disable_updater_task", True)),
                "task_name": str(self.config.get("updater_task_name", "AliUpdater")),
                "disable_attempts": self.updater.disable_attempts,
                "last_success_at": self.updater.last_success_at,
                "last_error": self.updater.last_error,
            },
            "client_support": self.client_support.status(),
            "appbiz_send": {
                "enabled": bool(self.config.get("appbiz_send_adapter_enabled", True)),
                "abi_validated": bool(self.config.get("appbiz_send_abi_validated", False)),
                "ready": self.appbiz.ready,
                "route_ready": self.appbiz.route_ready,
                "pid": self.appbiz.pid,
                "candidate_count": self.appbiz.candidate_count,
                "routable_candidate_count": self.appbiz.routable_candidate_count,
                "service_selected": bool(self.appbiz.selected_service),
                "selection_reason": self.appbiz.selection_reason,
                "last_selection_at": self.appbiz.last_selection_at,
                "observed_calls": self.appbiz.observed_calls,
                "observed_ccode_count": self.appbiz.observed_ccode_count,
                "received_calls": self.appbiz.received_calls,
                "received_messages": self.appbiz.received_messages,
                "last_receive_at": self.appbiz.last_receive_at,
                "receive_ready": bool(self.appbiz.script is not None),
                "helper_loaded": self.appbiz.helper_loaded,
                "last_observation_at": self.appbiz.last_observation_at,
                "last_arg_lengths": self.appbiz.last_arg_lengths,
                "last_callback_at": self.appbiz.last_callback_at,
                "last_result_code": self.appbiz.last_result_code,
                "last_error": self.appbiz.last_error,
            },
            "native": {
                "enabled": bool(self.config.get("native_adapter_enabled", True)),
                "ready": self.native.ready,
                "conflict": self.native.conflict,
                "pid": self.native.pid,
                "last_error": self.native.last_error,
                "last_event_at": self.native.last_event_at,
                "frames": self.native_frames,
            },
            "delivery": {
                **self.db.counts(),
                "enabled": bool(self.config.get("delivery_enabled", True)),
                "last_success_at": self.delivery.last_success_at,
                "last_error": self.delivery.last_error,
            },
            "send_enabled": bool(self.config.get("send_enabled", False)),
            "send_receipts": self.db.send_counts(),
            "api": {
                "url": f"http://127.0.0.1:{int(self.config.get('api_port'))}",
                "error": self.api_error,
            },
            "workbench": {
                "url": f"http://127.0.0.1:{int(self.config.get('workbench_port'))}",
                "sessions": workbench_session_count,
                "error": self.workbench_error,
            },
        }
        if include_brain:
            payload["brain"] = self.brain.status()
        return payload


def configure_logging() -> None:
    log_path = ROOT / "state" / "standalone_bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone Qianniu receive/send bridge")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--status-once", action="store_true")
    args = parser.parse_args()
    configure_logging()
    config_path = Path(args.config)
    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        prepare_device_identity(raw_config, config_path)
    except (OSError, ValueError, DeviceIdentityError) as error:
        LOG.error("device identity preparation failed: %s", error)
        if sys.stderr is not None:
            print(f"设备身份迁移失败：{error}", file=sys.stderr)
        return 6
    config = Config.load(config_path)
    app = StandaloneBridge(config)
    app.start()

    def stop(_signum: int, _frame: Any) -> None:
        app.stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    LOG.info(
        "standalone bridge started version=%s api_port=%s ws_port=%s workbench_port=%s",
        VERSION, config.get("api_port"), config.get("ws_port"), config.get("workbench_port"),
    )
    if args.status_once:
        time.sleep(3.0)
        if sys.stdout is not None:
            print(json.dumps(app.status(), ensure_ascii=False, indent=2))
        app.stop()
        return 0
    while not app.stop_event.wait(0.5):
        pass
    app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
