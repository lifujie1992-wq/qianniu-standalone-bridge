"""Last-mile deterministic guards for the authorized Tmall shop only."""
import math
import re
import time
from taobao_message_contract import message_id, epoch, classification, REVISION


def in_scope(account):
    return str(account or "").removeprefix("tb_nick_").replace("：", ":").split(":")[0] == "联想官方旗舰店"


def delivery_identity_view(event):
    """Use observed sender evidence in the final guard, preserving the raw event."""
    if not in_scope(event.get('account')):
        return event

    def uid(value):
        value = str(value or '')
        return value.split('.')[0] if re.fullmatch(r'\d+(?:\.\d+)?', value) else ''

    sender = uid(event.get('sender_uid'))
    login = uid(event.get('login_uid'))
    buyer = re.match(r'^(\d+)\.', str(event.get('buyer_id') or ''))
    nick = str(event.get('sender_nick') or '')
    staff = ((sender and login and sender == login)
             or (':' in nick or '：' in nick)
             and re.split(r'[:：]', nick, maxsplit=1)[0] == '联想官方旗舰店')
    if staff:
        return {**event, 'role': 'mall_cs', 'identity_uncertain': False}
    if sender and buyer and sender == buyer.group(1):
        return {**event, 'role': 'user', 'identity_uncertain': False}
    return event


def parent_id(value):
    return message_id(value)


def is_platform_context_notice(event):
    return classification(event) == "context"


def timestamp(value):
    return epoch(value)


def blocked_reason(meta, latest, now=None):
    now = time.time() if now is None else now
    # expires_at_ms is explicitly milliseconds, including small test values.
    try:
        expires = float(meta.get("expires_at_ms") or 0) / 1000
    except (TypeError, ValueError):
        expires = 0
    parent = parent_id(meta.get("takeover_parent_msg_id"))
    parent_ts = timestamp(meta.get("takeover_parent_ts"))
    if not parent or not parent_ts or not math.isfinite(expires) or expires <= 0:
        return "tmall_command_parent_missing"
    if now >= expires:
        return "tmall_command_expired"
    if not latest:
        return "tmall_local_parent_missing"
    latest_id = parent_id(latest.get("original_msg_id") or latest.get("msg_id"))
    latest_ts = timestamp(latest.get("original_timestamp") or latest.get("ts"))
    if latest_id != parent and (latest.get("_parent_guard_superseded") or latest_ts >= parent_ts):
        return "tmall_command_parent_superseded"
    if latest_id != parent:
        return "tmall_local_parent_mismatch"
    return ""


def parent_guard_evidence(meta, latest):
    latest = latest or {}
    return {"contract_revision": REVISION, "latest_class": classification(latest),
            "command_parent_msg_ids": meta.get("takeover_parent_msg_ids", []),
            "command_parent_msg_id": meta.get("takeover_parent_msg_id", ""),
            "command_parent_ts": timestamp(meta.get("takeover_parent_ts")),
            "latest_local_msg_id": latest.get("original_msg_id") or latest.get("msg_id") or "",
            "latest_local_ts": timestamp(latest.get("original_timestamp") or latest.get("ts")),
            "latest_captured_at_ms": latest.get("captured_at_ms", 0),
            "latest_capture_mode": latest.get("capture_mode", "")}


def projection_status(event):
    delivery = str(event.get("delivery_status") or "")
    auto = str(event.get("auto_send_status") or "")
    if delivery in {"confirmed", "delivered"}:
        return "confirmed"
    if delivery in {"failed", "rejected", "blocked"} or auto in {"failed", "blocked", "suppressed", "shadow_only", "stale_context", "already_handed_off", "duplicate_buyer_turn"}:
        return "not_sent"
    if delivery == "unknown":
        return "unknown"
    if delivery in {"accepted", "submitted", "in_flight"} or auto in {"accepted", "queued", "submitted"}:
        return "submitted"
    return "ai_draft"
