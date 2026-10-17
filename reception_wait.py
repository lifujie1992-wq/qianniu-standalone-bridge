"""Reply deadlines for the local reception list; drafts are not replies."""

from __future__ import annotations

from typing import Any

from tmall_delivery_guard import projection_status

REPLY_TIMEOUT_SECONDS = 180


def message_direction(event: dict[str, Any]) -> str:
    role = str(event.get("role") or "user").lower()
    if role in {"user", "buyer", "customer"}:
        return "user"
    if role == "assistant_simulated":
        return "seller" if projection_status(event) == "confirmed" else ""
    if role in {"assistant", "mall_cs", "seller", "self"}:
        # This hook observes a send attempt. Native local-send notifications
        # and the MessageSDK callback provide the reply receipt separately.
        if event.get("source") == "qianniu_appbiz_official_send":
            return ""
        if str(event.get("delivery_status") or "") in {
            "failed", "rejected", "blocked", "unknown", "submitted", "in_flight",
        }:
            return ""
        return "seller"
    return ""


def waiting_state(messages: list[tuple[float, float, str]], reply_ts: float = 0.0) -> dict[str, Any]:
    """Use local first ingestion time for the unanswered buyer turn.

    Message timestamps decide who spoke last. Ingestion time decides when the
    buyer appeared in the local window. Further buyer messages do not extend
    the deadline; a new turn after a reply gets its own 180 seconds.
    """
    latest_reply = max([reply_ts] + [ts for ts, _arrived, role in messages if role == "seller"])
    unanswered = [(ts, arrived) for ts, arrived, role in messages if role == "user" and ts > latest_reply]
    started_at = min((arrived for _ts, arrived in unanswered), default=0.0)
    return {
        "last_message_role": "user" if unanswered else ("seller" if latest_reply else ""),
        "reply_wait_started_at": started_at,
        "reply_due_at": started_at + REPLY_TIMEOUT_SECONDS if started_at else 0.0,
        "reply_timeout_seconds": REPLY_TIMEOUT_SECONDS,
    }
