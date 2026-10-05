"""Buyer nickname validation shared by capture, persistence and brain upload."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote


def valid_nickname(value: Any, buyer_id: str = "") -> str:
    if not isinstance(value, str):
        return ""
    nick = value.strip()
    if not nick or len(nick) > 128 or any(ord(c) < 32 for c in nick):
        return ""
    if nick == str(buyer_id or "").strip() or re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?(?:#\d+)?", nick):
        return ""
    if "#" in nick and "@" in nick:
        return ""
    return nick


def event_nickname(event: dict[str, Any]) -> str:
    buyer_id = str(event.get("buyer_id") or "")
    # Explicit nickname and the bridge's buyer_nick already describe the buyer,
    # including outgoing messages. Sender fields only describe inbound buyers.
    candidates = [event.get("nickname"), event.get("buyer_nick")]
    if str(event.get("role") or "user").lower() in {"user", "buyer", "customer"}:
        candidates.extend(event.get(k) for k in ("senderNick", "nick", "senderName"))
        sender = event.get("fromid") or event.get("fromId") or event.get("sender")
        if isinstance(sender, dict):
            candidates.extend(sender.get(k) for k in ("nick", "nickname", "senderNick", "senderName", "display"))
    return next((nick for value in candidates if (nick := valid_nickname(value, buyer_id))), "")


def nickname_event_id(event: dict[str, Any]) -> str:
    # Same escaping as JavaScript encodeURIComponent. Nick changes get separate
    # identities without sharing a message's idempotency key.
    parts = ["taobao", event.get("account"), event.get("buyer_id"), event.get("nickname")]
    return "qn-nick-v1|" + "|".join(quote(str(p or ""), safe="~()*!.'-") for p in parts)
