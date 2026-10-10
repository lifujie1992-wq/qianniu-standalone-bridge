import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from standalone_bridge import BrainConnector, StandaloneBridge, StateDB, brain_event_suppression_reason, ConfirmedSendRejection

SHOP='联想官方旗舰店:燕燕'
BUYER='123456789.1-126446588.1#11001@cntaobao'

class TransparentCommandsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=StandaloneBridge.__new__(StandaloneBridge)
        self.app.db=StateDB(Path(self.temp.name)/'state.db')
        self.app.config={'send_enabled':False,'brain_ai_reply_enabled':False}
        self.app.stop_event=threading.Event()
        self.app.appbiz=SimpleNamespace(send_text=Mock(return_value={'callback_received':True,'result_code':0}))
        self.app.db.set_session_control(SHOP,BUYER,'human','本机旧暂停','manual')
        self.brain=BrainConnector(self.app);self.brain.account_allowed=lambda a:True
        self.command={'id':'central-1','type':'send_text','account':SHOP,'buyer_id':BUYER,'content':'名称在设备后盖。','meta':{'expires_at_ms':1,'takeover_parent_msg_id':'missing'}}
    def tearDown(self):self.temp.cleanup()
    def test_central_tmall_command_reaches_native_despite_legacy_local_vetoes(self):
        result=self.brain.execute_command(self.command)
        self.assertEqual(result['status'],'confirmed',result)
        self.assertTrue(result['real_send'])
        self.app.appbiz.send_text.assert_called_once()
    def test_transport_idempotency_still_prevents_double_native_send(self):
        for _ in range(2):self.brain.execute_command(self.command)
        self.app.appbiz.send_text.assert_called_once()
    def test_other_shop_preserves_local_pause(self):
        self.command['account']='其他店:客服'
        result=self.brain.execute_command(self.command)
        self.assertFalse(result['real_send'])
        self.app.appbiz.send_text.assert_not_called()
    def test_account_authentication_still_required(self):
        self.brain.account_allowed=lambda a:False
        result=self.brain.execute_command(self.command)
        self.assertFalse(result['real_send'])
        self.app.appbiz.send_text.assert_not_called()
    def test_tmall_platform_asset_is_raw_input_for_brain(self):
        self.assertEqual(brain_event_suppression_reason({'account':SHOP,'content':'https://img.alicdn.com/tps/TB1abc-80-80.png'}),'')

    def test_proven_native_rejections_retry_but_eventual_success_sends_once(self):
        self.app.stop_event.wait=Mock()
        self.app.appbiz.send_text.side_effect=[ConfirmedSendRejection("not ready"), ConfirmedSendRejection("not ready"), {"callback_received":True,"result_code":0}]
        result=self.brain.execute_command(self.command)
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.app.appbiz.send_text.call_count,3)
        self.brain.execute_command(self.command)
        self.assertEqual(self.app.appbiz.send_text.call_count,3)
    def test_ambiguous_rpc_failure_never_retries_or_reports_success(self):
        self.app.appbiz.send_text.side_effect=RuntimeError("RPC connection lost during send")
        with self.assertRaises(RuntimeError):self.brain.execute_command(self.command)
        result=self.brain.execute_command(self.command)
        self.assertEqual(result["status"],"unknown")
        self.assertFalse(result["real_send"])
        self.app.appbiz.send_text.assert_called_once()
    def test_manual_pause_requires_acknowledgment_from_brain(self):
        self.brain.agent_id=lambda:"test-agent"
        self.brain.request=Mock(return_value={"event_acks":[]})
        with self.assertRaises(RuntimeError):self.brain.set_tmall_session_mode(SHOP,BUYER,"ai")
        self.assertEqual(self.app.db.session_control(SHOP,BUYER)["ai_mode"],"human")
        def response(method,path,payload):
            event=payload["events"][0]
            return {"event_acks":[{"event_id":event["event_id"],"status":"session_control_applied","committed":True}]}
        self.brain.request.side_effect=response
        self.brain.set_tmall_session_mode(SHOP,BUYER,"ai")
        self.assertEqual(self.app.db.session_control(SHOP,BUYER)["ai_mode"],"ai")
