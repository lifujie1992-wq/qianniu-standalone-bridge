"""Recover image media and handoff context from the authenticated native client."""
from __future__ import annotations

from datetime import datetime
import json
import re
import struct
import threading
import time
from typing import Any

from nickname_support import valid_nickname
from taobao_message_contract import TRANSFER, epoch, message_id as platform_message_id
from native_image_lookup import IMAGE_PLACEHOLDERS, is_placeholder_image_event, enrich_native_image_event

CCODE = re.compile(r'^(\d+)(?:\.\d+)?-\d+(?:\.\d+)?#\d+@cntaobao$')


def unpack_content(data: bytes) -> Any:
    """Bounded MessagePack subset used by native AIM text/image content."""
    if len(data) > 262144:
        raise ValueError('content_too_large')
    offset = 0
    items = 0

    def take(size: int) -> bytes:
        nonlocal offset
        if size < 0 or offset + size > len(data):
            raise ValueError('truncated_content')
        value = data[offset:offset+size]
        offset += size
        return value

    def number(size: int, signed: bool = False) -> int:
        return int.from_bytes(take(size), 'big', signed=signed)

    def read(depth: int = 0) -> Any:
        nonlocal items
        items += 1
        if depth > 12 or items > 4096:
            raise ValueError('content_limit')
        code = number(1)
        if code <= 127:
            return code
        if code >= 224:
            return code-256
        if 160 <= code <= 191:
            return take(code & 31).decode('utf-8')
        if code in (192, 194, 195):
            return {192: None, 194: False, 195: True}[code]
        if code in (204, 205, 206, 207, 208, 209, 210, 211):
            return number((1, 2, 4, 8)[(code-204) % 4], code >= 208)
        if code in (202, 203):
            return struct.unpack('>f' if code == 202 else '>d', take(4 if code == 202 else 8))[0]
        if code in (217, 218, 219, 196, 197, 198):
            size = number((1, 2, 4)[code-217 if code >= 217 else code-196])
            value = take(size)
            return value.decode('utf-8') if code >= 217 else value
        if 128 <= code <= 143 or code in (222, 223):
            size = code & 15 if code <= 143 else number(2 if code == 222 else 4)
            if size > 1024:
                raise ValueError('map_limit')
            result = {}
            for _ in range(size):
                key = read(depth+1)
                result[key] = read(depth+1)
            return result
        if 144 <= code <= 159 or code in (220, 221):
            size = code & 15 if code <= 159 else number(2 if code == 220 else 4)
            if size > 1024:
                raise ValueError('array_limit')
            return [read(depth+1) for _ in range(size)]
        raise ValueError('unsupported_content_tag')

    result = read()
    if offset != len(data):
        raise ValueError('trailing_content')
    return result


def native_event(row: list[Any], account: str, buyer_id: str) -> dict[str, Any] | None:
    """Keep only exact conversation/identity matches and original platform IDs."""
    match = CCODE.fullmatch(buyer_id)
    if not match or len(row) != 10 or row[0] != buyer_id or not row[1] or row[8] != '0' or row[9] != '0':
        return None
    stamp = epoch(row[3])
    if not stamp:
        return None
    try:
        ext = json.loads(row[7] or '{}')
        if not isinstance(ext, dict):
            return None
        packed = unpack_content(bytes.fromhex(row[6] or ''))
        if not isinstance(packed, dict):
            return None
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return None
    sender = str(row[2] or '').removesuffix('@cntaobao').removesuffix('.1')
    sender_nick = str(ext.get('sender_nick') or '').removeprefix('cntaobao')
    recipient_nick = str(ext.get('receiver_nick') or '').removeprefix('cntaobao')
    shop = account.split(':', 1)[0]
    if sender == match[1]:
        role, nickname = 'user', valid_nickname(sender_nick, buyer_id)
    elif sender_nick == shop or sender_nick.startswith(shop+':'):
        role, nickname = 'seller', valid_nickname(recipient_nick, buyer_id)
    else:
        return None
    content = ''
    media = ''
    if str(row[4]) == '1' and packed.get(1) == 1:
        text = packed.get(2)
        if isinstance(text, dict) and isinstance(text.get(1), str):
            content = text[1]
    elif str(row[4]) == '101' and packed.get(1) == 101:
        image = packed.get(3)
        try:
            value = json.loads(image.get(2) or '{}') if isinstance(image, dict) else {}
        except (ValueError, TypeError):
            value = {}
        if isinstance(value, dict) and isinstance(value.get('url'), str) and value['url'].startswith(('https://', 'http://')):
            content = media = value['url']
    if not content:
        return None
    result = {'type':'message', 'platform':'taobao', 'account':account, 'buyer_id':buyer_id,
              'msg_id':str(row[1]), 'original_msg_id':str(row[1]), 'nickname':nickname,
              'content':content, 'role':role, 'ts':stamp, 'original_timestamp':stamp,
              'capture_mode':'history_snapshot', 'source':'qianniu_native_message_store',
              'raw_type':'image' if media else 'text'}
    if media:
        result.update(image_url=media, media_url=media, message_type='image', image_lookup='native_message_store')
    return result


class NativeContext:
    def __init__(self, app: Any):
        self.app = app
        self.lock = threading.RLock()
        self.images = 0
        self.history_rows = 0
        self.last_error = ''
        self.last_lookup_ms = 0

    def read(self, event: dict[str, Any], message_id: str = '') -> list[dict[str, Any]]:
        account, buyer_id = str(event.get('account') or ''), str(event.get('buyer_id') or '')
        stamp = epoch(event.get('original_timestamp') or event.get('ts')) or time.time()
        start = datetime.fromtimestamp(stamp).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        before = int((max(stamp,time.time())+300)*1000) if message_id else int((stamp+1)*1000)
        after = 0 if message_id else int(start*1000)
        response = self.app.appbiz.read_messages(account, buyer_id, before, after, message_id, 1 if message_id else 100)
        if not isinstance(response, dict) or response.get('ok') is not True:
            raise RuntimeError(str((response or {}).get('error') or 'native_history_unavailable'))
        return [event for row in response.get('rows', []) if isinstance(row, list)
                if (event := native_event(row, account, buyer_id)) is not None]

    def prepare(self, event: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run on the upload worker, after durable capture and before brain ingest."""
        history: list[dict[str, Any]] = []
        enabled = bool(self.app.config.get('bridge_native_image_lookup', True))
        content = str(event.get('content') or '').strip()
        is_transfer = bool(TRANSFER.fullmatch(content)) and content.rsplit('转交给', 1)[-1].strip() == str(event.get('account') or '').split(':')[-1]
        needs_image = enabled and is_placeholder_image_event(event) and not event.get('image_url')
        # History-only uploads must never recursively fetch another history window.
        if not needs_image and (not is_transfer or event.get('capture_mode') == 'history_snapshot'):
            return event, history
        started = time.perf_counter()
        with self.lock:
            try:
                message_id = platform_message_id(event.get('original_msg_id') or event.get('msg_id'))
                if needs_image:
                    matches = self.read(event, message_id)
                    exact = next((item for item in matches if item['msg_id'] == message_id and item.get('image_url')), None)
                    if exact:
                        event = dict(event)
                        event.update({key:exact[key] for key in ('image_url','media_url','message_type','image_lookup','raw_type')})
                        if content in IMAGE_PLACEHOLDERS or not content:
                            event['original_content'] = str(event.get('content') or '')
                            event['content'] = exact['image_url']
                        self.images += 1
                    else:
                        self.last_error = 'image_message_not_available'
                if is_transfer and self.app.config.get('bridge_native_transfer_history', True):
                    parent = next((item for item in self.read(event,message_id)
                                   if item['msg_id']==message_id and item['content'].strip()==content),None)
                    if parent is None:
                        raise RuntimeError('transfer_message_not_available')
                    # The old native callback stamps receipt time. Anchor the
                    # history window to the actual platform record without
                    # rewriting the original live event's timestamp fields.
                    stamp = parent['ts']
                    history = [item for item in self.read(dict(event,original_timestamp=stamp))
                               if item['msg_id'] != message_id and item['ts'] <= stamp]
                    event = dict(event, native_history={'status':'loaded' if history else 'empty', 'count':len(history), 'source':'native_message_store', 'platform_timestamp':stamp})
                    self.history_rows += len(history)
                if history or event.get('image_url'):
                    self.last_error = ''
                self.app.db.remember_native_context(history+[event])
            except Exception as error:
                self.last_error = str(error)[:200]
                if is_transfer:
                    event = dict(event, native_history={'status':'unavailable', 'count':0, 'error':self.last_error})
            if needs_image and not event.get('image_url'):
                event = enrich_native_image_event(event, self.app.browser, enabled=True)
                if event.get('image_url'):
                    self.images += 1
                    self.last_error = ''
                    self.app.db.remember_native_context([event])
            self.last_lookup_ms = round((time.perf_counter()-started)*1000)
        return event, history

    def status(self) -> dict[str, Any]:
        return {'images_resolved':self.images, 'history_messages':self.history_rows,
                'last_lookup_ms':self.last_lookup_ms, 'last_error':self.last_error}
