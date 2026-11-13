import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from standalone_bridge import AppBizSendAdapter, Config
from pathlib import Path

class NativeReadinessTests(unittest.TestCase):
    def adapter(self):
        adapter=AppBizSendAdapter(SimpleNamespace(config=Config(Path('config.json'),{'appbiz_send_abi_validated':True,'appbiz_callback_wait_seconds':0})))
        status={'candidate_count':1,'selected_service':'0x1','selection_reason':'singlemsg_getnewmsg'}
        exports=SimpleNamespace(status=Mock(return_value=status),preparesend=Mock(return_value={'ok':True}),sendtext=Mock(return_value=0),pollsend=Mock(return_value={'state':1,'result_code':0}),cancelsend=Mock())
        adapter.script=SimpleNamespace(exports_sync=exports)
        return adapter,exports

    def test_cached_global_readiness_is_refreshed_before_rejecting(self):
        adapter,exports=self.adapter()
        self.assertFalse(adapter.route_ready)
        receipt=adapter.send_text('buyer#1@cntaobao','答复','test',tmall_account='联想官方旗舰店:燕燕')
        self.assertTrue(receipt['native_submitted']);exports.sendtext.assert_called_once()

    def test_passive_target_route_can_prepare_without_new_message_fetch(self):
        adapter,exports=self.adapter()
        cold={'candidate_count':1,'selected_service':'','selection_reason':''}
        exports.status.side_effect=[cold,{'candidate_count':1,'selected_service':'0x1','selection_reason':'singlemsg_getnewmsg'},{'candidate_count':1,'selected_service':'0x1','selection_reason':'singlemsg_getnewmsg'}]
        adapter.send_text('buyer#1@cntaobao','答复','test',tmall_account='联想官方旗舰店:燕燕')
        exports.preparesend.assert_called_once_with('buyer#1@cntaobao', True)
        exports.sendtext.assert_called_once()

    def test_no_observed_route_still_rejects_without_native_send(self):
        adapter,exports=self.adapter()
        exports.status.return_value={'candidate_count':1,'selected_service':'','selection_reason':''}
        exports.preparesend.return_value={'ok':False}
        with self.assertRaisesRegex(RuntimeError,'service is not selected'):
            adapter.send_text('buyer#1@cntaobao','答复','test',tmall_account='联想官方旗舰店:燕燕')
        exports.sendtext.assert_not_called()

    def test_status_read_failure_after_native_submit_is_not_unsent(self):
        adapter,exports=self.adapter();adapter.selected_service='0x1';adapter.selection_reason='message_arrive'
        exports.status.side_effect=RuntimeError('status read lost')
        receipt=adapter.send_text('buyer#1@cntaobao','答复','test',tmall_account='联想官方旗舰店:燕燕')
        self.assertTrue(receipt['native_submitted']);exports.sendtext.assert_called_once()

    def test_receipt_read_failure_after_submit_stays_unconfirmed_without_retry(self):
        adapter,exports=self.adapter();adapter.selected_service='0x1';adapter.selection_reason='message_arrive'
        adapter.app.config.raw['appbiz_callback_wait_seconds']=0.1
        exports.pollsend.side_effect=RuntimeError('receipt channel lost')
        receipt=adapter.send_text('buyer#1@cntaobao','答复','test',tmall_account='联想官方旗舰店:燕燕')
        self.assertTrue(receipt['native_submitted']);self.assertFalse(receipt['callback_received'])
        exports.sendtext.assert_called_once()

    def test_other_shop_does_not_recover_a_cold_route(self):
        adapter,exports=self.adapter()
        with self.assertRaisesRegex(RuntimeError,'service is not selected'):
            adapter.send_text('buyer#1@cntaobao','答复','test',tmall_account='其他店铺:客服')
        exports.status.assert_not_called();exports.preparesend.assert_not_called()
        exports.sendtext.assert_not_called()

    def test_native_tmall_transfer_requests_targeted_passive_history_refresh(self):
        browser = SimpleNamespace(
            connected=1,
            execute_all=Mock(return_value=[{"captured": 2, "sent": 2}]),
        )
        app = SimpleNamespace(
            config=Config(Path("config.json"), {}),
            browser=browser,
            ingest_event=Mock(return_value=("event-1", True)),
        )
        adapter = AppBizSendAdapter(app)

        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        event = {
            "platform": "taobao",
            "role": "user",
            "account": "联想官方旗舰店:铭铭",
            "buyer_id": "buyer#1@cntaobao",
            "content": "由 服务助手 转交给 铭铭 原因：机器人接待触发转人工",
            "msg_id": "transfer.PNM",
            "original_msg_id": "transfer.PNM",
        }
        with patch("standalone_bridge.threading.Thread", ImmediateThread):
            adapter.on_message({
                "type": "send",
                "payload": {"event": "appbiz_receive", "message": event},
            }, None)

        browser.execute_all.assert_called_once()
        expression = browser.execute_all.call_args.args[0]
        self.assertIn("__qn_standalone_refresh_conversation", expression)
        self.assertIn("__qn_standalone_self_heal", expression)
        self.assertIn("buyer#1@cntaobao", expression)
        self.assertNotIn("GetNewMsg", expression)
        self.assertNotIn("GetRemoteHisMsg", expression)
