"""Last-mile deterministic guards for the authorized Tmall shop only."""
import math
import time


def in_scope(account):
    return str(account or "").removeprefix("tb_nick_").replace("：", ":").split(":")[0] == "联想官方旗舰店"


def parent_id(value):
    return str(value or "").removeprefix("qn-msg-v1|taobao|")


def timestamp(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    if value > 1e17:
        return value / 1e9
    if value > 1e14:
        return value / 1e6
    if value > 1e11:
        return value / 1e3
    return value


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
    if latest_id != parent and latest_ts >= parent_ts:
        return "tmall_command_parent_superseded"
    if latest_id != parent:
        return "tmall_local_parent_mismatch"
    return ""
