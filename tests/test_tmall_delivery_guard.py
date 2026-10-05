import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from standalone_bridge import BrainConnector, StandaloneBridge, StateDB
from tmall_delivery_guard import blocked_reason, in_scope

ACCOUNT = '联想官方旗舰店:燕燕'
BUYER = '4007146934.1-126446588.1#11001@cntaobao'

class TmallDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = StateDB(Path(self.temp.name) / 'state.sqlite3')
        self.now = int(time.time())
        self.event('parent', self.now)
        self.send = Mock(return_value={'status': 'submitted'})
        self.brain = BrainConnector(SimpleNamespace(config={}, db=self.db, send_text=self.send))
        self.brain.account_allowed = lambda _: True
        self.command = {'id':'c1', 'type':'send_text', 'account':ACCOUNT, 'buyer_id':BUYER,
                        'content':'亲，已接到您的咨询', 'meta':{
                            'takeover_parent_msg_id':'qn-msg-v1|taobao|parent',
                            'takeover_parent_ts':self.now, 'expires_at_ms':(self.now+30)*1000}}

    def tearDown(self):
        self.temp.cleanup()

    def event(self, msg_id, ts, **kw):
        return self.db.upsert_event({'account':ACCOUNT, 'buyer_id':BUYER, 'role':'user',
            'content':'问题', 'msg_id':msg_id, 'original_msg_id':msg_id, 'ts':ts,
            'original_timestamp':ts, **kw})

    def test_current_parent_can_send(self):
        self.assertTrue(self.brain.execute_command(self.command)['real_send'])
        self.send.assert_called_once()

    def test_expired_never_calls_sender(self):
        self.command['meta']['expires_at_ms']=1
        r=self.brain.execute_command(self.command)
        self.assertEqual(r['error'],'tmall_command_expired')
        self.assertFalse(r['real_send']); self.send.assert_not_called()

    def test_new_question_supersedes_parent_including_same_second(self):
        self.event('new-question', self.now)
        r=self.brain.execute_command(self.command)
        self.assertEqual(r['error'],'tmall_command_parent_superseded')
        self.send.assert_not_called()

    def test_missing_meta_blocks_only_authorized_shop(self):
        self.command['meta']={}
        self.assertEqual(self.brain.execute_command(self.command)['error'],'tmall_command_parent_missing')
        self.send.assert_not_called()
        self.command['account']='其他淘宝店:客服'
        self.assertTrue(self.brain.execute_command(self.command)['real_send'])
        self.assertFalse(in_scope('cs_123'))

    def test_explicit_manual_send_does_not_require_automatic_parent(self):
        self.command['meta']={'manual_direct':True}
        self.assertTrue(self.brain.execute_command(self.command)['real_send'])
        self.send.assert_called_once()

    def test_metadata_staff_and_incomplete_history_do_not_supersede_parent(self):
        self.event('staff', self.now+1, role='mall_cs')
        self.event('incomplete', self.now+2, incomplete=True)
        self.db.upsert_event({'account':ACCOUNT,'buyer_id':BUYER,'type':'nickname_update','nickname':'买家'})
        self.assertEqual(self.db.latest_tmall_buyer_event(ACCOUNT,BUYER)['msg_id'],'parent')
        self.assertTrue(self.brain.execute_command(self.command)['real_send'])

    def test_late_new_question_is_checked_again_before_native_send(self):
        bridge=StandaloneBridge.__new__(StandaloneBridge)
        bridge.config={'send_enabled':True}; bridge.db=self.db
        bridge.appbiz=SimpleNamespace(send_text=Mock())
        bridge.expire_unconfirmed_sends=Mock()
        self.event('new-question', self.now+1)
        r=bridge.send_text({'request_id':'race','buyer_cid':BUYER,'content':'旧回复',
                           'tmall_account':ACCOUNT,'tmall_command_meta':self.command['meta']},brain_authorized=True)
        self.assertEqual(r['status'],'blocked'); bridge.appbiz.send_text.assert_not_called()

    def test_other_seat_or_buyer_cannot_change_local_parent(self):
        self.event('other-buyer',self.now+1,buyer_id='other')
        self.event('other-seat',self.now+2,account='联想官方旗舰店:雪晴')
        self.assertTrue(self.brain.execute_command(self.command)['real_send'])

    def test_invalid_timestamps_and_missing_local_parent_fail_closed(self):
        for value in ('nan','bad',0):
            meta=dict(self.command['meta'],takeover_parent_ts=value)
            self.assertEqual(blocked_reason(meta,{}),'tmall_command_parent_missing')
        self.assertEqual(blocked_reason(self.command['meta'],None),'tmall_local_parent_missing')

if __name__=='__main__': unittest.main()
