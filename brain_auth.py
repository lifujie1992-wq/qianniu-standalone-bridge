"""大脑鉴权失败的结构化诊断（1.6.84）。

现场（2026-11-04，DESKTOP-FHQHG6C）：助手在大脑注册处被 `HTTP 403
{"error":"invalid agent token","ok":false}` 顶住，面板只显示一句英文
`invalid agent token`。运维看到这句话无法判断三件事：

* 是哪一串令牌被拒（这台机器上同时存在大脑工位令牌和本机网关令牌）；
* 服务端到底是 401 还是 403，还是连"设备绑定"都没过；
* 该找谁修——重装客户端、换网络、关代理全都不起作用。

本模块把「HTTP 状态码 + 响应体」归一化成结构化诊断（code / status /
token 指纹 / 中文结论 / 处理建议），供三处复用：

* ``standalone_bridge.BrainConnector`` —— 运行期 last_error 与 /api/v1/status；
* ``config_dialog`` —— 配置窗口「测试连接」的弹窗文案；
* ``field-support/采集诊断.ps1`` / ``tools/check_brain_token.py`` —— 现场取证。

设计约束：只用标准库、不导入项目其它模块，确保诊断本身永远可用
（它要解释的正是"连不上"这种场景）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

INVALID_AGENT_TOKEN = "invalid_agent_token"
TOKEN_BOUND_ELSEWHERE = "token_bound_to_another_device"
DEVICE_BINDING_MISMATCH = "device_binding_mismatch"
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
SERVER_ERROR = "server_error"
NETWORK_ERROR = "network"
WS_UPGRADE_REQUIRED = "ws_upgrade_required"
UNKNOWN = "unknown"

# 服务端把这几种说法都用来表示"这串工位令牌我不认"。
_TOKEN_WORDS = (
    "invalid agent token",
    "invalid_agent_token",
    "invalid token",
    "invalid_token",
    "token invalid",
    "token expired",
    "token_expired",
    "bad token",
    "forbidden token",
    "unknown token",
    "令牌",
    "签名",
)
_DEVICE_WORDS = (
    "device binding",
    "binding mismatch",
    "device_mismatch",
    "device-id",
    "device_id",
    "device id",
    "设备绑定",
    "设备标识",
)
# 令牌本身有效、但已经绑在别的机器上。大脑用中文回这句（219 字节），
# 和"令牌不在这台大脑的库里"（43 字节 invalid agent token）是两回事：
# 前者要解绑/换机，后者说明令牌签到了另一套环境。现场 2026-11-04 就是后者。
_TOKEN_BOUND_WORDS = (
    "另一台电脑",
    "另一台机器",
    "another device",
    "token_bound_to_another_device",
    "bound to another",
)
_AUTH_STATUSES = {401, 403}
_ERROR_VALUE_KEYS = ("error", "detail", "message", "msg", "reason")
_CODE_KEYS = ("code", "error_code", "errno")


def token_fingerprint(value: Any) -> str:
    """Return a stable, non-reversible fingerprint such as ``sha256:1a2b3c4d5e``.

    Only the first 10 hex chars are kept: enough to tell two tokens apart in a
    support ticket, useless for replaying the credential.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return f"sha256:{digest[:10]}"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def parse_brain_error_detail(detail: Any) -> dict[str, Any]:
    """Pull ``error``/``code`` out of a brain error body.

    The brain answers with ``{"error": "...", "ok": false}``; some deployments
    wrap it as ``{"detail": ...}`` and the websockets layer uses plain text.
    Anything unparsable is kept verbatim in ``raw`` so the ticket still carries
    the server's own words.
    """
    raw = _as_text(detail)[:2000]
    payload: dict[str, Any] = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    error_text = ""
    for key in _ERROR_VALUE_KEYS:
        candidate = _as_text(payload.get(key))
        if candidate:
            error_text = candidate
            break
    if not error_text:
        error_text = raw
    code = ""
    for key in _CODE_KEYS:
        candidate = _as_text(payload.get(key))
        if candidate:
            code = candidate
            break
    return {"error_text": error_text[:500], "server_code": code[:80], "payload": payload, "raw": raw}


def normalize_brain_error_code(status_code: int, detail: Any) -> str:
    """Map a status code + body onto one of the module-level error codes."""
    parsed = parse_brain_error_detail(detail)
    haystack = f"{parsed['error_text']} {parsed['server_code']}".lower()
    # "已绑另一台电脑" 必须在 "令牌" 之前判：那句中文里也带"令牌"两个字。
    if any(word in haystack for word in _TOKEN_BOUND_WORDS):
        return TOKEN_BOUND_ELSEWHERE
    if any(word in haystack for word in _TOKEN_WORDS):
        return INVALID_AGENT_TOKEN
    if any(word in haystack for word in _DEVICE_WORDS):
        return DEVICE_BINDING_MISMATCH
    try:
        status = int(status_code or 0)
    except (TypeError, ValueError):
        status = 0
    if status == 401:
        return UNAUTHORIZED
    if status in _AUTH_STATUSES:
        return FORBIDDEN
    if status == 426:
        return WS_UPGRADE_REQUIRED
    if 500 <= status < 600:
        return SERVER_ERROR
    if status == 0:
        return NETWORK_ERROR
    return UNKNOWN


def describe_brain_error(
    status_code: int,
    detail: Any = "",
    *,
    token: Any = "",
    server_url: str = "",
    transport: str = "http",
) -> dict[str, Any]:
    """Return a structured, human-actionable description of a brain failure.

    Keys:
        status          HTTP status (0 = the request never completed)
        code            one of the module-level codes
        auth            True when the server rejected this workstation's credential
        retryable       False when retrying the same credential cannot help
        message         one-line Chinese conclusion (goes into ``last_error``)
        hint            what the operator should actually do next
        fingerprint     ``sha256:xxxxxxxxxx`` of the rejected token
        raw             the server's own words, verbatim
        error_text      the ``error`` field (or plain text body)
    """
    parsed = parse_brain_error_detail(detail)
    code = normalize_brain_error_code(status_code, detail)
    try:
        status = int(status_code or 0)
    except (TypeError, ValueError):
        status = 0
    fingerprint = token_fingerprint(token)
    suffix = f"，令牌指纹 {fingerprint}" if fingerprint else "，当前配置里没有令牌"
    endpoint = f"{server_url.rstrip('/')}" if server_url else "大脑"
    error_text = parsed["error_text"]

    message = ""
    hint = ""
    auth = False
    retryable = True

    if code == TOKEN_BOUND_ELSEWHERE:
        auth = True
        retryable = False
        message = f"这串令牌已经绑定在另一台电脑上{suffix}"
        hint = (
            "令牌本身是有效的，但和这台机器的设备 ID 对不上："
            "在大脑后台把它解绑，或者给这台机器单独签发一把。"
        )
    elif code == INVALID_AGENT_TOKEN:
        auth = True
        retryable = False
        message = f"大脑已连通，但工位令牌无效（invalid agent token{suffix}）"
        hint = (
            "服务端回 invalid agent token（43 字节）＝这串令牌不在这台大脑的库里，"
            "多半是在另一套后台/环境签发的。请打开 http://<大脑地址>/ 的令牌管理新增或"
            "重新生成，并确认浏览器地址栏和助手配置的是同一台大脑。"
            "重装客户端、改网络、关代理都不会恢复。"
        )
        if not fingerprint:
            hint = "配置里 brain_agent_token 为空：请在配置窗口填大脑工位令牌。"
    elif code == DEVICE_BINDING_MISMATCH:
        auth = True
        retryable = False
        message = f"大脑 HTTP {status}：设备绑定不匹配{suffix}"
        hint = (
            "令牌绑定的是另一台设备（换过机器或重装过系统）。"
            "请在大脑后台解绑该工位，或重新生成令牌后再启动。"
        )
    elif code == UNAUTHORIZED:
        auth = True
        retryable = False
        message = f"大脑 HTTP 401：工位令牌缺失或已失效{suffix}"
        hint = "到大脑后台复制该工位的当前令牌并重新保存。"
    elif code == FORBIDDEN:
        reason = error_text[:160]
        message = (
            f"大脑 HTTP 403：{reason}" if reason else "大脑 HTTP 403：服务端拒绝访问"
        )
        hint = "服务端拒绝了请求但不是令牌问题，请把本页状态发给大脑同学核对路由/权限。"
    elif code == WS_UPGRADE_REQUIRED:
        message = "大脑长连接（WebSocket）握手被拒"
        hint = "已自动回退 HTTP 上报；请大脑同学确认 /api/bridge/v1/ws 是否可用。"
    elif code == SERVER_ERROR:
        message = f"大脑 HTTP {status}：服务端异常，稍后自动重试"
        hint = error_text[:160] if error_text else "服务端 5xx，通常几分钟内自愈。"
    elif code == NETWORK_ERROR:
        message = f"无法连接大脑（{endpoint}）：{error_text or '请求未完成'}"
        hint = (
            "本机到大脑是直连（已绕过系统代理）。请确认："
            "1) 能打开 http://<大脑地址>/health；2) 没有防火墙拦截；"
            "3) 服务器地址与当前构建一致。"
        )
    else:
        message = f"大脑 HTTP {status}：{error_text}" if status else f"大脑请求失败：{error_text}"
        hint = error_text[:200]

    return {
        "status": status,
        "code": code,
        "auth": auth,
        "retryable": retryable,
        "message": message[:500],
        "hint": hint[:500],
        "fingerprint": fingerprint,
        "raw": parsed["raw"],
        "error_text": error_text,
        "transport": transport,
    }


def format_brain_error(described: dict[str, Any]) -> str:
    """Flatten a description into one dialog-ready sentence."""
    message = str(described.get("message") or "").strip()
    hint = str(described.get("hint") or "").strip()
    if message and hint:
        return f"{message}。{hint}"
    return message or hint


def is_auth_rejection(described: dict[str, Any]) -> bool:
    return bool(described.get("auth"))


def describe_brain_http_error(
    status_code: int,
    detail: Any = "",
    *,
    token: Any = "",
    server_url: str = "",
) -> dict[str, Any]:
    """Convenience alias kept for call sites that only have a status + body."""
    return describe_brain_error(
        status_code, detail, token=token, server_url=server_url
    )
