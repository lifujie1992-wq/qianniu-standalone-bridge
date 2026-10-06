"""Shared, dependency-free Taobao turn contract; server/client copies match."""
import math
import re

REVISION = "tmall-turn-contract-v1"
TRANSFER = re.compile(r"由\s*\S.{0,60}?\s*转交给\s*\S.{0,100}")
CONTEXT = (
    re.compile(r"当前用户来自\s*[:：]?\s*[^\n？?]{1,80}"),
    re.compile(r"请尽快回复[，,]?\s*避免超时[。.!！]?"),
    re.compile(r"买家多次进线咨询[，,]?\s*请做好用户接待[，,]?\s*及时解决问题[，,]?\s*可有效提升满意度[、,，]?\s*降低平台求助率[。.!！]?"),
)
TPS_ASSET = re.compile(r"https?://[^/]*alicdn\.com/.+!!600000000\d+-\d+-tps-\d+-\d+\.(?:png|gif|jpe?g|webp)", re.I)
HISTORY = re.compile(r"GetRemoteHisMsg|GetLocalHisMsg|history_snapshot|^poll:|^event-remote:", re.I)


def message_id(value):
    value = str(value or "").strip()
    prefix = "qn-msg-v1|taobao|"
    while value.startswith(prefix):
        value = value[len(prefix):]
    return value


def epoch(value):
    try:
        value = float(value)
    except (ValueError, TypeError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    for threshold, divisor in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)):
        if value > threshold:
            return value / divisor
    return value



NOTICE_REVISION = 'tmall-notice-contract-v1'
TMALL_NOTICE_PATTERNS = (
    ('shipping_status', re.compile(r'预计\d+小时内发货[，,|｜\s]+(?:预计[^？?\n]{1,35}送达|承诺\d+小时内发货)[。~～!！]*')),
    ('refund_expired', re.compile(r'亲[，,]客服帮你申请的退款已过期[，,]请与客服重新沟通[。~～!！]*')),
    ('refund_retention_status', re.compile(r'您已成功发送退款挽留方案[。！!]请密切关注消费者的反馈[，,]必要时进行服务跟进[，,]有助于提升挽留成功率哦[。~～!！]*')),
    ('service_risk', re.compile(r'买家近期的咨询不满意风险高[，,]请做好用户接待[，,]及时解决问题[，,]可有效提升满意度[、，,]降低平台求助率[。~～!！]*')),
)


def notification_kind(event):
    account = str(event.get('account') or '').split(':', 1)[0].split('：', 1)[0]
    if event.get('shop_id') != 'tb_nick_联想官方旗舰店' and account != '联想官方旗舰店':
        return ''
    text = str(event.get('content') or '').strip()
    if TRANSFER.fullmatch(text):
        return 'transfer'
    if any(pattern.fullmatch(text) for pattern in CONTEXT):
        return 'service_reminder'
    return next((kind for kind, pattern in TMALL_NOTICE_PATTERNS if pattern.fullmatch(text)), '')


def classification(event):
    role = str(event.get("role") or "").strip().lower()
    if role in {"mall_cs", "assistant", "assistant_simulated", "seller", "self"}:
        return "staff"
    if (event.get("incomplete") or event.get("identity_uncertain")
            or event.get("brain_suppressed") or event.get("capture_mode") == "brain_projection"):
        return "uncertain"
    if event.get("type") == "nickname_update":
        return "context"
    content = str(event.get("content") or "").strip()
    if notification_kind(event) not in {'', 'transfer'}:
        return 'context'
    if (any(p.fullmatch(content) for p in CONTEXT)
            or content in {"为您推荐宝贝", "向您推荐宝贝", "邀请您评价"}
            or TPS_ASSET.fullmatch(content)):
        return "context"
    if role not in {"user", "buyer", "customer"}:
        return "uncertain"
    if TRANSFER.fullmatch(content):
        return "transfer"
    # Genuine text, image, voice, video and product-card messages all count.
    return "buyer"


def eligible(event):
    return (classification(event) in {"buyer", "transfer"}
            and bool(message_id(event.get("original_msg_id") or event.get("msg_id")))
            and bool(epoch(event.get("original_timestamp") or event.get("ts"))))


def captured(event):
    return epoch(event.get("captured_at_ms")) or epoch(event.get("captured_at"))


def select_parent(events, parent_id="", batch_ids=()):
    """Events are in stable capture/insertion order, oldest first.

    The local parent anchors ordering; the server may have restamped its time.
    Known messages in the same admitted batch do not supersede one another.
    History uses original time, so a late history replay is not a new turn.
    """
    candidates = [(index, event) for index, event in enumerate(events) if eligible(event)]
    wanted = message_id(parent_id)
    anchor = next(((index, event) for index, event in candidates
                   if message_id(event.get("original_msg_id") or event.get("msg_id")) == wanted), None)
    if anchor is None:
        return max(candidates, key=lambda item: (epoch(item[1].get("original_timestamp") or item[1].get("ts")),
                   classification(item[1]) == "buyer", item[0]))[1] if candidates else None
    anchor_index, parent = anchor
    cohort = {message_id(i) for i in batch_ids} if isinstance(batch_ids, (tuple, list)) else set()
    cohort.add(wanted)
    parent_ts = epoch(parent.get("original_timestamp") or parent.get("ts"))
    parent_capture = captured(parent)
    newer = []
    for index, event in candidates:
        mid = message_id(event.get("original_msg_id") or event.get("msg_id"))
        if mid in cohort or classification(event) != "buyer":
            continue
        stamp = epoch(event.get("original_timestamp") or event.get("ts"))
        receipt = captured(event)
        mode = str(event.get("capture_mode") or "")
        parent_mode = str(parent.get("capture_mode") or "")
        if classification(parent) == "transfer" and stamp == parent_ts:
            after = True
        elif (HISTORY.search(mode)
              or (mode.startswith("event-local-db:") and "native_callback" in parent_mode and stamp < parent_ts)):
            after = stamp > parent_ts
        elif parent_capture and receipt:
            after = receipt > parent_capture or (receipt == parent_capture and index > anchor_index)
        else:
            after = stamp > parent_ts or (stamp == parent_ts and index > anchor_index)
        if after:
            newer.append((receipt or stamp, index, event))
    if newer:
        result = dict(max(newer, key=lambda item: (item[0], item[1]))[2])
        result["_parent_guard_superseded"] = True
        return result
    return parent


def verified_batch_parent(events, parent_id, questions):
    """Only stored, eligible buyer messages whose full content matches qualify."""
    by_id = {message_id(e.get("original_msg_id") or e.get("msg_id")): e for e in events}
    verified = []
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            continue
        mid = message_id(question.get("msg_id"))
        event = by_id.get(mid)
        if (event and eligible(event) and classification(event) == "buyer"
                and str(event.get("content") or "").strip() == str(question.get("content") or "").strip()):
            box = event.get("whitebox") or {}
            queue = box.get("shadow_queue") or {}
            if queue.get("eligible") is not False and queue.get("auto_send_eligible") is not False:
                verified.append(mid)
    if verified:
        selected = by_id[verified[-1]]
        return selected.get("msg_id") or selected.get("original_msg_id"), verified
    return parent_id, []
