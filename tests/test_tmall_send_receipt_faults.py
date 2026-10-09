"""Fault injection at the real native adapter / command receipt seams."""
import json
import tempfile
import threading
import time
import unittest
import os

os.environ.setdefault("QN_BRAIN_SERVER_URL", "http://127.0.0.1:1")
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from standalone_bridge import AppBizSendAdapter, BrainConnector, Config, StandaloneBridge, StateDB

SHOP = 'tb_nick_联想官方旗舰店'

class ReceiptFaultTests(unittest.TestCase):
    def adapter(self, code, delay=3.0):
        clock = [0.0]
        exports = SimpleNamespace(sendtext=Mock(return_value=0),
            pollsend=Mock(side_effect=lambda token: {'state': 1, 'result_code': code} if clock[0] >= delay else {'state': 0}),
            cancelsend=Mock(), status=Mock(return_value={'candidate_count': 1, 'selected_service': '0x1', 'selection_reason': 'message_arrive'}))
        adapter = AppBizSendAdapter(SimpleNamespace(config=Config(Path('/tmp/audit-config.json'), {
            'appbiz_send_abi_validated': True, 'appbiz_callback_wait_seconds': 2,
            'send_confirmation_timeout_seconds': 15})))
        adapter.pid = 7
        adapter.script = SimpleNamespace(exports_sync=exports)
        adapter.selected_service = '0x1'
        adapter.selection_reason = 'message_arrive'
        return adapter, clock, exports

    def run_delayed(self, code, account=SHOP):
        adapter, clock, exports = self.adapter(code)
        with patch('standalone_bridge.time.monotonic', side_effect=lambda: clock[0]), patch('standalone_bridge.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = adapter.send_text('buyer#1@cntaobao', '测试回复', 'test', tmall_account=account)
        return result, exports

    def test_late_native_rejection_is_not_reported_as_submitted(self):
        with self.assertRaisesRegex(RuntimeError, 'CheckConvExist_error'):
            self.run_delayed(5)

    def test_late_native_success_is_collected(self):
        result, exports = self.run_delayed(0)
        self.assertTrue(result['callback_received'])
        self.assertEqual(result['result_code'], 0)
        exports.cancelsend.assert_not_called()

    def test_other_shop_keeps_original_wait(self):
        result, exports = self.run_delayed(0, 'other-shop')
        self.assertFalse(result['callback_received'])
        exports.cancelsend.assert_called_once()

    def chain(self, directory, code, delay=3.0):
        adapter, clock, exports = self.adapter(code, delay)
        app = StandaloneBridge.__new__(StandaloneBridge)
        app.db = StateDB(Path(directory)/'state.db')
        app.config = Config(Path(directory)/'config.json', {
            'send_enabled': True, 'appbiz_send_abi_validated': True,
            'appbiz_callback_wait_seconds': 2, 'send_confirmation_timeout_seconds': 15})
        app.stop_event = threading.Event()
        app.appbiz = adapter
        adapter.app = app
        now = time.time()
        app.db.upsert_event({'account': SHOP, 'buyer_id': 'buyer#1@cntaobao', 'role': 'user',
            'content': '测试问题', 'msg_id': 'parent', 'original_msg_id': 'parent', 'ts': now})
        brain = BrainConnector(app)
        brain.account_allowed = lambda account: True
        brain.report_command_result = Mock()
        command = {'id': 'chain-fault', 'type': 'send_text', 'account': SHOP,
            'buyer_id': 'buyer#1@cntaobao', 'content': '正常客服回复', 'meta': {
                'takeover_parent_msg_id': 'qn-msg-v1|taobao|parent',
                'takeover_parent_ts': now, 'expires_at_ms': (now+120)*1000}}
        return brain, command, clock, exports

    def handle_chain(self, brain, command, clock):
        with patch('standalone_bridge.time.monotonic', side_effect=lambda: clock[0]), patch('standalone_bridge.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0]+seconds)):
            brain.handle_command(command)
        return brain.report_command_result.call_args.args[1]

    def test_delayed_failure_reaches_command_result_and_send_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            brain, command, clock, exports = self.chain(directory, 5)
            result = self.handle_chain(brain, command, clock)
            self.assertFalse(result['ok'])
            self.assertIn('CheckConvExist_error', result['error'])
            self.assertEqual(brain.app.db.send_counts(), {'rejected': 1})
            exports.sendtext.assert_called_once()

    def test_delayed_success_reaches_confirmed_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            brain, command, clock, exports = self.chain(directory, 0)
            result = self.handle_chain(brain, command, clock)
            self.assertTrue(result['confirmed'])
            self.assertEqual(brain.app.db.send_counts(), {'confirmed': 1})
            exports.sendtext.assert_called_once()

    def test_no_callback_stays_unconfirmed_and_replay_never_resends(self):
        with tempfile.TemporaryDirectory() as directory:
            brain, command, clock, exports = self.chain(directory, 0, 100)
            result = self.handle_chain(brain, command, clock)
            self.assertEqual(result['status'], 'submitted')
            self.assertFalse(result['confirmed'])
            self.assertIn('人工核对', result.get('error_user', ''))
            stored = brain.app.db.pending_brain_command_results(10)[0]['result']
            self.assertEqual(stored['error_user'], result['error_user'])
            brain.app.db.expire_unconfirmed_sends(15, now=time.time()+100)
            self.assertEqual(brain.app.db.send_counts(), {'unknown': 1})
            self.handle_chain(brain, command, clock)
            exports.sendtext.assert_called_once()

    def test_report_failure_and_restart_do_not_send_again(self):
        with tempfile.TemporaryDirectory() as directory:
            brain, command, clock, exports = self.chain(directory, 0)
            brain.report_command_result.side_effect = RuntimeError('ACK disconnected')
            with self.assertRaisesRegex(RuntimeError, 'ACK disconnected'):
                self.handle_chain(brain, command, clock)
            # Reopen SQLite and create a fresh connector as a process restart would.
            brain.app.db = StateDB(Path(directory)/'state.db')
            restarted = BrainConnector(brain.app)
            restarted.account_allowed = lambda account: True
            restarted.report_command_result = Mock()
            self.handle_chain(restarted, command, clock)
            exports.sendtext.assert_called_once()
            self.assertTrue(restarted.report_command_result.call_args.args[1]['confirmed'])

    def test_in_flight_replay_cannot_be_promoted_to_success(self):
        with tempfile.TemporaryDirectory() as directory:
            app = SimpleNamespace(config=Config(Path(directory)/'config.json', {}),
                db=StateDB(Path(directory)/'state.db'), stop_event=threading.Event(),
                send_text=Mock(return_value={'ok': False, 'status': 'in_flight', 'submitted': False, 'confirmed': False}))
            brain = BrainConnector(app)
            with patch.object(brain, 'account_allowed', return_value=True):
                result = brain.execute_command({'id': 'fault-test', 'type': 'send_text', 'account': SHOP,
                    'buyer_id': 'buyer#1@cntaobao', 'content': '正常客服回复', 'meta': {'manual_direct': True}})
            self.assertFalse(result['ok'])
            self.assertFalse(result['real_send'])

if __name__ == '__main__':
    unittest.main()
