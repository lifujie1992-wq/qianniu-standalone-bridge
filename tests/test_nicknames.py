from __future__ import annotations

import gc
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nickname_support import valid_nickname, event_nickname, nickname_event_id
from standalone_bridge import (StateDB, StandaloneBridge, BrowserServer, BrainConnector, Config,
    normalize_event, canonical_event_id)

BUYER = '4007146934.1-126446588.1#11001@cntaobao'


class NicknameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.temp.name) / 'state.sqlite3')

    def tearDown(self):
        gc.collect()  # Existing StateDB connections rely on GC for Windows unlink.
        self.temp.cleanup()

    def event(self, **overrides):
        return {'account': 'seller', 'buyer_id': BUYER, 'role': 'user', 'content': 'hello',
                'msg_id': 'm1', 'original_msg_id': 'm1', 'ts': 1791220000.25,
                'original_timestamp': 1791220000.25, **overrides}

    def app(self):
        app = StandaloneBridge.__new__(StandaloneBridge)
        app.config = {'delivery_enabled': True}
        app.db = self.db
        app.delivery = SimpleNamespace(wakeup=threading.Event())
        app.context = Mock()
        app.brain = SimpleNamespace(configured=lambda: True, wakeup=threading.Event(), event_wakeup=threading.Event())
        return app

    def test_normalization_adds_only_nickname_to_message_contract(self):
        original = self.event(senderNick='昵称', buyer_nick='4007146934.1')
        normalized = normalize_event(original)
        self.assertEqual(normalized['nickname'], '昵称')
        for key, value in original.items():
            self.assertEqual(normalized[key], value)
        self.assertEqual(canonical_event_id(normalized), 'qn-msg-v1|taobao|m1')

    def test_empty_is_explicit_and_invalid_names_rejected(self):
        for nick in ('', '4007146934', '4007146934.1', '4007146934.1-126446588.1', BUYER, {}, 'bad\nname'):
            self.assertEqual(valid_nickname(nick, BUYER), '')
        self.assertEqual(normalize_event(self.event())['nickname'], '')
        self.assertEqual(valid_nickname('tb597275049', BUYER), 'tb597275049')

    def test_outbound_never_takes_seller_sender_fields(self):
        self.assertEqual(event_nickname(self.event(role='mall_cs', senderNick='seller')), '')
        self.assertEqual(event_nickname(self.event(role='mall_cs', senderNick='seller', buyer_nick='买家')), '买家')

    def test_db_cache_persists_and_cannot_cross_shops_or_be_erased(self):
        self.db.upsert_event(self.event(nickname='真昵称'))
        again = StateDB(self.db.path)
        self.assertEqual(again.cached_nickname('seller', BUYER), '真昵称')
        self.assertEqual(again.cached_nickname('other-shop', BUYER), '')
        again.upsert_event(self.event(msg_id='m2', original_msg_id='m2', nickname=''))
        self.assertEqual(again.cached_nickname('seller', BUYER), '真昵称')
        self.assertEqual(again.workbench_sessions()[0]['buyer_nick'], '真昵称')

    def test_metadata_backfill_updates_session_without_new_messages_or_context(self):
        self.db.upsert_event(self.event(buyer_nick='4007146934.1'))
        before = self.db.workbench_sessions()[0]
        app = self.app()
        update = {'type': 'nickname_update', 'account': 'seller', 'buyer_id': BUYER, 'nickname': '补传昵称'}
        event_id, changed = app.ingest_event(update)
        self.assertTrue(changed)
        self.assertEqual(event_id, nickname_event_id(update))
        app.context.enqueue.assert_not_called()
        after = self.db.workbench_sessions()[0]
        self.assertEqual(after['buyer_nick'], '补传昵称')
        for key in ('message_count', 'last_ts', 'last_message'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(app.ingest_event(update), (event_id, False))
        rows = self.db.claim_brain_events(0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['payload']['type'], 'nickname_update')
        self.assertNotIn('msg_id', rows[0]['payload'])
        self.assertNotIn('ts', rows[0]['payload'])

    def test_invalid_update_is_not_enqueued(self):
        app = self.app()
        self.assertFalse(app.ingest_event({'type': 'nickname_update', 'account': 'seller', 'buyer_id': BUYER, 'nickname': BUYER})[1])
        self.assertEqual(self.db.claim_brain_events(0), [])

    def test_metadata_id_distinguishes_shop_and_changed_nick(self):
        first = {'type': 'nickname_update', 'account': '联想', 'buyer_id': BUYER, 'nickname': '昵称|甲'}
        expected = 'qn-nick-v1|taobao|%E8%81%94%E6%83%B3|4007146934.1-126446588.1%2311001%40cntaobao|%E6%98%B5%E7%A7%B0%7C%E7%94%B2'
        self.assertEqual(canonical_event_id(first), expected)
        self.assertNotEqual(canonical_event_id(first), canonical_event_id(dict(first, nickname='乙')))
        self.assertNotEqual(canonical_event_id(first), canonical_event_id(dict(first, account='other')))

    def test_browser_accepts_metadata_without_content_and_acks_commit(self):
        app = self.app()
        browser = BrowserServer(app)
        update = {'type': 'nickname_update', 'account': 'seller', 'buyer_id': BUYER, 'nickname': '昵称'}
        class Connection:
            def __iter__(self):
                return iter([json.dumps({'type': 'chat_event', 'event_id': nickname_event_id(update), 'payload': update})])
        connection = Connection()
        with patch.object(browser, 'allowed', return_value=True), patch.object(browser, 'send') as send:
            browser.handler(connection)
        self.assertEqual(browser.error, '')
        self.assertEqual(send.call_args.args[1]['type'], 'chat_ack')
        self.assertTrue(send.call_args.args[1]['committed'])
        self.assertEqual(self.db.cached_nickname('seller', BUYER), '昵称')

    def test_upload_http_and_ws_preserve_type_and_fill_legacy_nickname(self):
        self.db.remember_nickname('seller', BUYER, '缓存昵称')
        config = Config(Path(self.temp.name) / 'config.json', {
            'brain_enabled': True, 'brain_server_url': 'http://127.0.0.1:1',
            'brain_agent_token': 'test-only', 'brain_agent_id': 'test-agent', 'device_id': 'test-device'})
        brain = BrainConnector(SimpleNamespace(config=config, db=self.db, stop_event=threading.Event()))
        message = self.event(event_id='message-id')
        update = {'type': 'nickname_update', 'account': 'seller', 'buyer_id': BUYER, 'nickname': '缓存昵称', 'event_id': 'update-id'}
        rows = [{'event_id': value['event_id'], 'revision': 'r1', 'payload': value} for value in (message, update)]
        ack = {'event_acks': [{'event_id': row['event_id'], 'committed': True, 'retryable': False} for row in rows]}
        for ws in (False, True):
            channel = SimpleNamespace(available=ws, send_events=Mock(return_value=ack))
            with patch.object(brain, 'event_channel', return_value=channel), patch.object(brain, 'request', return_value=ack) as request:
                self.assertEqual(brain.upload_events(rows), {'message-id', 'update-id'})
            events = channel.send_events.call_args.args[0] if ws else request.call_args.args[2]['events']
            self.assertEqual([event['type'] for event in events], ['message', 'nickname_update'])
            self.assertEqual([event['nickname'] for event in events], ['缓存昵称', '缓存昵称'])
            self.assertEqual(events[0]['buyer_id'], BUYER)
            self.assertEqual(events[0]['msg_id'], 'm1')
            self.assertEqual(events[0]['ts'], message['ts'])

    def test_backfill_worker_filters_old_and_named_sessions(self):
        app = self.app()
        app.browser = SimpleNamespace(connected=1, execute_all=Mock())
        app.db.upsert_event(self.event(ts=time.time(), original_timestamp=time.time()))
        app.db.upsert_event(self.event(msg_id='old', original_msg_id='old', buyer_id='old#1@cntaobao', ts=1, original_timestamp=1))
        app.db.upsert_event(self.event(msg_id='named', original_msg_id='named', buyer_id='named#1@cntaobao', nickname='已有昵称', ts=time.time(), original_timestamp=time.time()))
        app.backfill_nicknames()
        expression = app.browser.execute_all.call_args.args[0]
        self.assertIn(BUYER, expression)
        self.assertNotIn('old#1', expression)
        self.assertNotIn('named#1', expression)


if __name__ == '__main__':
    unittest.main()
