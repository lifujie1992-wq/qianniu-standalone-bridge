"""Native AppBiz image events only carry "[图片]"; recover the URL from the page cache.

The AppBiz callback exposes a text ``content`` field but not the image URL. The
in-page bridge can read Qianniu's ``window._db.msgDataMap`` and extract chat
image URLs (``browser_bridge.js`` exposes ``window.__qn_standalone_image_url``).
This module turns the native placeholder into a real image event so the brain's
vision pipeline receives an image instead of asking the buyer to retype it.
"""
from __future__ import annotations

import json
from typing import Any

IMAGE_PLACEHOLDERS = frozenset({"[图片]", "买家发送图片", "图片"})
IMAGE_RAW_TYPES = frozenset({"image", "img", "pic"})
HTTP_PREFIXES = ("http://", "https://")


def is_placeholder_image_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    content = str(event.get("content") or "").strip()
    raw_type = str(event.get("raw_type") or "").strip().lower()
    if content.startswith(HTTP_PREFIXES):
        return False
    return content in IMAGE_PLACEHOLDERS or raw_type in IMAGE_RAW_TYPES


def enrich_native_image_event(
    event: dict[str, Any],
    browser: Any,
    *,
    enabled: bool = True,
    timeout: float = 1.5,
) -> dict[str, Any]:
    """Return a copy with the image URL filled in when the page cache has it."""
    if not enabled or not is_placeholder_image_event(event):
        return event
    ccode = str(event.get("buyer_id") or "").strip()
    msg_id = str(event.get("original_msg_id") or event.get("msg_id") or "").strip()
    if not ccode or not msg_id:
        return event
    execute = getattr(browser, "execute", None)
    lookup = getattr(browser, "lookup_image_url", None)
    if not callable(execute) and not callable(lookup):
        return event
    expression = (
        "window.__qn_standalone_image_url && "
        f"window.__qn_standalone_image_url({json.dumps(ccode)}, {json.dumps(msg_id)})"
    )
    try:
        url = lookup(ccode, msg_id, timeout=timeout) if callable(lookup) else execute(expression, timeout=timeout)
    except Exception:
        return event
    if not isinstance(url, str) or not url.startswith(HTTP_PREFIXES):
        return event
    enriched = dict(event)
    enriched["original_content"] = str(event.get("content") or "")
    if not str(event.get("content") or "").strip() or str(event.get("content") or "").strip() in IMAGE_PLACEHOLDERS:
        enriched["content"] = url
    enriched["image_url"] = url
    enriched["media_url"] = url
    enriched["raw_type"] = "image"
    enriched["message_type"] = "image"
    enriched["image_lookup"] = "browser_msg_data_map"
    return enriched
