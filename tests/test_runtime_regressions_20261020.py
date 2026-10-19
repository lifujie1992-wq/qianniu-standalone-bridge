"""Executable coverage for reception timing and native metadata (no live sends)."""
from __future__ import annotations

import gc
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nickname_resolver import ConversationSelectionWatch, local_reply_records, nickname_records
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
    def test_clicking_current_chat_still_dispatches_but_does_not_wait_for_another_event(self):
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
            app.nicknames = SimpleNamespace(log=SimpleNamespace(paths=[log]))
            app.stop_event = threading.Event()
            app._conversation_open_lock = threading.Lock()
            app._conversation_watch = None
            app._conversation_watch_pid = 0
            with patch('standalone_bridge.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run, \
                    patch.object(app.stop_event, 'wait', side_effect=AssertionError('unnecessary wait')):
                result = app.open_conversation_protocol('store', 'buyer', BUYER)
            self.assertTrue(result['verified'])
            self.assertEqual(result['opened_ccode'], BUYER)
            self.assertEqual(Path(run.call_args.args[0][0]).resolve(), (runtime/'9.97.86N/WWCmd.exe').resolve())
            self.assertNotIn('content=', run.call_args.args[0][1])


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
