"""Executable coverage for reception timing and native metadata (no live sends)."""
from __future__ import annotations

import gc
import json
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nickname_resolver import ConversationSelectionWatch, NicknameResolver, local_reply_records, nickname_records
from reception_wait import waiting_state
from standalone_bridge import BrowserServer, Config, StandaloneBridge, StateDB

BUYER = '100.1-200.1#11001@cntaobao'


def notification(payload, event='im.uiutil.onConversationChange', marker='strEvent'):
    stamp = datetime.now().strftime('%m-%d %H:%M:%S')
    return (f'[{stamp} 1 2 3 I] app [CHAT store#3#200 store#3#200] '
            f'{marker}={event}, jsonStr=' + json.dumps(payload, ensure_ascii=False) + '\n')


class ReceptionOrderingTests(unittest.TestCase):
    def test_buyer_after_reply_with_same_platform_second_starts_wait(self):
        row = waiting_state([(1000, 1000.1, 'seller'), (1000, 1000.2, 'user')])
        self.assertEqual(row['last_message_role'], 'user')
        self.assertEqual(row['reply_due_at'], 1180.2)

    def test_reply_after_buyer_with_same_platform_second_stops_wait(self):
        self.assertEqual(waiting_state([(1000, 1000.1, 'user'),
                                       (1000, 1000.2, 'seller')])['reply_due_at'], 0)

    def test_receipt_without_message_keeps_timestamp_confirmation(self):
        self.assertEqual(waiting_state([(1000, 1000.1, 'user')], 1000)['reply_due_at'], 0)

    def test_consecutive_buyer_messages_do_not_extend_deadline(self):
        self.assertEqual(waiting_state([(1000, 1001, 'user'),
                                       (1010, 1011, 'user')])['reply_due_at'], 1181)

    def test_replayed_older_buyer_message_does_not_restart_wait(self):
        self.assertEqual(waiting_state([(900, 1010, 'user'),
                                       (1000, 1001, 'seller')])['reply_due_at'], 0)

    def test_session_preview_and_deadline_use_newest_same_second_message(self):
        with tempfile.TemporaryDirectory() as folder:
            db = StateDB(Path(folder) / 'state.sqlite3')
            base = float(int(time.time()) - 1)
            for role, arrived, content in [('seller', base+.1, 'older reply'),
                                            ('user', base+.2, 'new buyer message')]:
                with patch('standalone_bridge.time.time', return_value=arrived):
                    db.upsert_event({'account': 'store', 'buyer_id': BUYER, 'role': role,
                                     'content': content, 'msg_id': role, 'ts': base})
            for instance in [db, StateDB(db.path)]:
                row = instance.workbench_sessions()[0]
                self.assertEqual(row['last_message'], 'new buyer message')
                self.assertEqual(row['last_message_role'], 'user')
                self.assertAlmostEqual(row['reply_due_at'], base+.2+180)
            gc.collect()


class NativeMetadataTests(unittest.TestCase):
    def test_deselect_invalidates_old_target_and_advances_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.log'
            path.write_text(notification({'ccode': BUYER, 'targetId': '100'}), encoding='utf-8')
            watch = ConversationSelectionWatch([path], process_id=2, recent_bytes=4096)
            watch.poll()
            revision = watch.revisions['store']
            with path.open('a', encoding='utf-8') as stream:
                stream.write(notification({'cid': {}}))
            watch.poll()
            self.assertNotIn('store', watch.selected)
            self.assertGreater(watch.revisions['store'], revision)

    def test_current_selection_can_be_reused_only_from_the_active_client(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.log'
            path.write_text(notification({'ccode': BUYER, 'targetId': '100'}), encoding='utf-8')
            current = ConversationSelectionWatch([path], process_id=2, recent_bytes=4096)
            current.poll()
            self.assertEqual(current.selected.get('store'), BUYER)
            restarted = ConversationSelectionWatch([path], process_id=999, recent_bytes=4096)
            restarted.poll()
            self.assertEqual(restarted.selected, {})
            other = '101.1-200.1#11001@cntaobao'
            with path.open('a', encoding='utf-8') as stream:
                stream.write(notification({'ccode': other, 'targetId': '101'}))
            current.poll()
            self.assertEqual(current.selected.get('store'), other)

    def test_large_log_gap_does_not_reuse_an_unobserved_old_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.log'
            path.write_text(notification({'ccode': BUYER, 'targetId': '100'}), encoding='utf-8')
            watch = ConversationSelectionWatch([path], process_id=2, recent_bytes=4096)
            watch.poll()
            with path.open('a', encoding='utf-8') as stream:
                stream.write('padding\n' * 400000)
            watch.poll()
            self.assertEqual(watch.selected, {})

    def test_reply_receipt_accepts_both_native_event_markers(self):
        now = time.time()
        payload = {'cid': {'ccode': BUYER}, 'newmsgs': [{
            'fromId': {'targetId': '200'}, 'toId': {'targetId': '100'},
            'mcode': {'messageId': 'reply-id'}, 'sendTime': now}]}
        for marker in ['strEvent', 'strFullEvent']:
            with self.subTest(marker=marker):
                line = notification(payload, 'im.singlemsg.onShopRobotLocalSendMsgs', marker)
                self.assertEqual(local_reply_records(line, now), [('store', BUYER, now)])

    def test_nickname_does_not_accept_staff_as_buyer(self):
        payload = {'cid': {'ccode': BUYER, 'targetId': '100', 'nick': 'store:staff'}}
        self.assertEqual(nickname_records(notification(payload), time.time()), [])

    def test_selection_requires_a_new_selection_notification(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.log'
            payload = {'ccode': BUYER, 'targetId': '100', 'nick': 'buyer'}
            path.write_text(notification(payload), encoding='utf-8')
            watch = ConversationSelectionWatch([path])
            watch.poll()
            self.assertEqual(watch.selected, {})
            with path.open('a', encoding='utf-8') as stream:
                stream.write(notification(payload, 'im.singlemsg.onReceiveNewMsg'))
            watch.poll()
            self.assertEqual(watch.selected, {})
            with path.open('a', encoding='utf-8') as stream:
                stream.write(notification(payload))
            watch.poll()
            self.assertEqual(watch.selected, {'store': BUYER})

    def test_selection_rejects_mismatched_buyer_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.log'
            path.write_text('', encoding='utf-8')
            watch = ConversationSelectionWatch([path])
            path.write_text(notification({'ccode': BUYER, 'targetId': '999'}), encoding='utf-8')
            watch.poll()
            self.assertEqual(watch.selected, {})


class NativeJumpTests(unittest.TestCase):
    def check_jump(self, *, new_selection=None, route_nick=''):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / 'runtime'
            for version in ['9.97.86N', '9.98.49N']:
                (runtime / version).mkdir(parents=True)
                (runtime / version / 'WWCmd.exe').touch()
            (runtime / 'AliWorkbench.ini').write_text('[Common]\nVersion=9.97.86N\n', encoding='utf-8')
            log = root / 'app.log'
            log.write_text(notification({'ccode': BUYER, 'targetId': '100'}), encoding='utf-8')
            app = StandaloneBridge.__new__(StandaloneBridge)
            app.config = Config(root/'config.json', {'qianniu_exe': str(runtime/'AliWorkbench.exe')})
            app.appbiz = SimpleNamespace(pid=2)
            app.db = SimpleNamespace(conversation_route_nickname=lambda account,buyer: route_nick)
            app.nicknames = SimpleNamespace(log=SimpleNamespace(paths=[log]))
            app.stop_event = threading.Event()
            app._conversation_open_lock = threading.Lock()
            app._conversation_watch = None
            app._conversation_watch_pid = 0
            def dispatch(*args, **kwargs):
                if new_selection is not None:
                    with log.open('a', encoding='utf-8') as stream:
                        stream.write(notification(new_selection))
                return SimpleNamespace(returncode=0)
            with patch('standalone_bridge.subprocess.run', side_effect=dispatch) as run, \
                    patch.object(app.stop_event, 'wait', side_effect=lambda _: app.stop_event.set()):
                result = app.open_conversation_protocol('store', 'buyer', BUYER)
            self.assertEqual(Path(run.call_args.args[0][0]).resolve(), (runtime/'9.97.86N/WWCmd.exe').resolve())
            self.assertNotIn('content=', run.call_args.args[0][1])
            self.assertIn('touid=cntaobao'+(route_nick or 'buyer'),run.call_args.args[0][1])
            return result

    def test_cached_current_chat_cannot_verify_a_new_jump(self):
        result = self.check_jump()
        self.assertFalse(result['verified'])
        self.assertEqual(result['opened_ccode'], '')

    def test_fresh_selection_verifies_the_exact_target(self):
        result = self.check_jump(new_selection={'ccode': BUYER, 'targetId': '100'})
        self.assertTrue(result['verified'])
        self.assertEqual(result['opened_ccode'], BUYER)

    def test_fresh_wrong_selection_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '未切换到目标会话'):
            self.check_jump(new_selection={'ccode': '101.1-200.1#11001@cntaobao', 'targetId': '101'})

    def test_fresh_deselect_does_not_verify_cached_target(self):
        self.assertFalse(self.check_jump(new_selection={'cid': {}})['verified'])

    def test_native_command_uses_login_address_while_display_name_changes(self):
        result = self.check_jump(new_selection={'ccode':BUYER,'targetId':'100'},route_nick='login-address')
        self.assertTrue(result['verified'])
        self.assertEqual(result['buyer_nick'],'buyer')


class NativeNicknameRefreshTests(unittest.TestCase):
    def test_routing_name_is_preserved_when_display_nickname_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            app, resolver, _ = self.make_app(Path(folder))
            resolver.resolve_pending()
            self.assertEqual(app.db.cached_nickname('store',BUYER),'new-name')
            self.assertEqual(app.db.conversation_route_nickname('store',BUYER),'tb123456')
            self.assertEqual(app.db.conversation_route_nickname('another-shop',BUYER),'')
            self.assertEqual(app.db.conversation_route_nickname('store','other-buyer'),'')

    def test_history_staff_and_metadata_cannot_supply_the_login_address(self):
        with tempfile.TemporaryDirectory() as folder:
            app, _, _ = self.make_app(Path(folder))
            base={'account':'store','buyer_id':BUYER,'content':'x','ts':1}
            with patch('standalone_bridge.time.time',return_value=1):
                app.db.upsert_event(dict(base,msg_id='staff',role='mall_cs',nickname='staff-name'))
                app.db.upsert_event(dict(base,msg_id='history',role='user',nickname='history-alias',capture_mode='history_snapshot'))
                app.db.upsert_event(dict(base,role='user',type='nickname_update',nickname='metadata-alias'))
            self.assertEqual(app.db.conversation_route_nickname('store',BUYER),'tb123456')

    def make_app(self, root, nick='tb123456'):
        app = StandaloneBridge.__new__(StandaloneBridge)
        app.db = StateDB(root / 'state/bridge.sqlite3')
        app.config = Config(root/'config.json', {})
        app.stop_event = threading.Event()
        app.delivery = SimpleNamespace(wakeup=threading.Event())
        app.brain = SimpleNamespace(configured=lambda: False)
        app.db.upsert_event({'account':'store','buyer_id':BUYER,'nickname':nick,
                             'msg_id':'original-message','content':'original','role':'user','ts':1234})
        resolver = NicknameResolver(app)
        log = root/'app.log'
        log.write_text(notification({'ccode':BUYER,'targetId':'100','nick':'new-name'}),encoding='utf-8')
        resolver.log.paths = [log]
        resolver.log.next_discovery = float('inf')
        resolver.log.available = True
        return app, resolver, log

    def test_nonempty_cache_refreshes_from_native_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            app, resolver, _ = self.make_app(Path(folder))
            with closing(app.db.connect()) as connection:
                before = connection.execute("SELECT payload FROM events WHERE json_extract(payload,'$.msg_id')='original-message'").fetchone()[0]
            resolver.resolve_pending()
            self.assertEqual(app.db.cached_nickname('store',BUYER),'new-name')
            self.assertEqual(app.db.workbench_sessions()[0]['buyer_nick'],'new-name')
            with closing(app.db.connect()) as connection:
                after = connection.execute("SELECT payload FROM events WHERE json_extract(payload,'$.msg_id')='original-message'").fetchone()[0]
                count = connection.execute("SELECT COUNT(*) FROM events WHERE json_extract(payload,'$.type')='nickname_update'").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(count,1)

    def test_repeated_polls_do_not_reemit_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            app, resolver, _ = self.make_app(Path(folder))
            with patch.object(app,'ingest_event',wraps=app.ingest_event) as ingest:
                resolver.resolve_pending(); resolver.resolve_pending()
            self.assertEqual(ingest.call_count,1)
            self.assertEqual(resolver.resolved,1)

    def test_unknown_native_peer_does_not_create_a_session(self):
        with tempfile.TemporaryDirectory() as folder:
            app, resolver, log = self.make_app(Path(folder))
            other='101.1-200.1#11001@cntaobao'
            log.write_text(notification({'ccode':other,'targetId':'101','nick':'unrelated'}),encoding='utf-8')
            resolver.resolve_pending()
            self.assertEqual(app.db.cached_nickname('store',other),'')
            self.assertEqual(len(app.db.workbench_sessions()),1)

    def test_missing_nickname_still_resolves_through_pending_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            app, resolver, _ = self.make_app(Path(folder),nick='')
            resolver.enqueue('store',BUYER); resolver.resolve_pending()
            self.assertEqual(app.db.cached_nickname('store',BUYER),'new-name')
            self.assertEqual(resolver.pending,{})

    def test_transient_ingest_failure_retries_metadata_next_poll(self):
        with tempfile.TemporaryDirectory() as folder:
            app, resolver, _ = self.make_app(Path(folder))
            with patch.object(app,'ingest_event',side_effect=RuntimeError('temporary')):
                with self.assertRaises(RuntimeError):resolver.resolve_pending()
            self.assertTrue(resolver.metadata_pending)
            resolver.resolve_pending()
            self.assertEqual(app.db.cached_nickname('store',BUYER),'new-name')
            self.assertEqual(resolver.metadata_pending,{})

    def test_latest_native_nickname_wins_within_one_poll(self):
        with tempfile.TemporaryDirectory() as folder:
            app, resolver, log = self.make_app(Path(folder))
            with log.open('a',encoding='utf-8') as stream:
                stream.write(notification({'ccode':BUYER,'targetId':'100','nick':'latest-name'}))
            resolver.resolve_pending()
            self.assertEqual(app.db.cached_nickname('store',BUYER),'latest-name')

    def test_older_log_record_cannot_replace_newer_native_nickname(self):
        with tempfile.TemporaryDirectory() as folder:
            app, resolver, log = self.make_app(Path(folder))
            older = notification({'ccode':BUYER,'targetId':'100','nick':'stale-name'})
            older = '['+datetime.fromtimestamp(time.time()-120).strftime('%m-%d %H:%M:%S')+older[15:]
            with log.open('a',encoding='utf-8') as stream:stream.write(older)
            resolver.resolve_pending()
            self.assertEqual(app.db.cached_nickname('store',BUYER),'new-name')


class ImagePageRaceTests(unittest.TestCase):
    def test_workbench_manual_reply_opens_qianniu_instead_of_a_denied_local_send(self):
        page = (Path(__file__).resolve().parents[1] / 'workbench.html').read_text(encoding='utf-8')
        self.assertIn('在千牛回复</button>', page)
        self.assertIn("addEventListener('click', replyInQianniu)", page)
        self.assertIn('await openInQianniu(state.current)', page)
        self.assertNotIn("api('/api/v1/reply'", page)

    def test_empty_dx_page_cannot_win_over_a_matching_image_page(self):
        browser = BrowserServer(SimpleNamespace())
        empty, useful = object(), object()
        browser.connections = {empty: threading.Lock(), useful: threading.Lock()}
        url = 'https://img.alicdn.com/chat.jpg'
        def respond(connection, payload):
            browser.pending[payload['request_id']].put_nowait(
                {'ok': True, 'value': '' if connection is empty else url})
        with patch.object(browser, 'send', side_effect=respond):
            self.assertEqual(browser.lookup_image_url(BUYER, 'image-message', timeout=.1), url)
        self.assertEqual(browser.pending, {})


if __name__ == '__main__':
    unittest.main()
