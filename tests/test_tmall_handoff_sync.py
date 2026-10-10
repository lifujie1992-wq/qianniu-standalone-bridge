import tempfile,time,unittest,threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from standalone_bridge import BrainConnector,StandaloneBridge,StateDB
from tmall_delivery_guard import projection_status

ACCOUNT='联想官方旗舰店:燕燕'
BUYER='4007146934.1-126446588.1#11001@cntaobao'

class TmallHandoffSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=StateDB(Path(self.temp.name)/'state.sqlite3')
        self.app=SimpleNamespace(config={'delivery_enabled':True},db=self.db,
            send_text=Mock(return_value={'status':'confirmed'}))
        self.brain=BrainConnector(self.app);self.brain.account_allowed=lambda _:True
        self.now=int(time.time())
        self.db.upsert_event({'account':ACCOUNT,'buyer_id':BUYER,'role':'user','content':'问题',
            'msg_id':'parent','original_msg_id':'parent','ts':self.now,'original_timestamp':self.now})
    def tearDown(self):self.temp.cleanup()
    def state(self, **meta):
        return self.brain.execute_command({'id':'state','type':'session_state','account':ACCOUNT,'buyer_id':BUYER,'meta':meta})
    def command(self, **meta):
        return {'id':'send','type':'send_text','account':ACCOUNT,'buyer_id':BUYER,'content':'亲，已转接专员',
            'meta':{'takeover_parent_msg_id':'parent','takeover_parent_ts':self.now,'expires_at_ms':(self.now+30)*1000,**meta}}
    def test_remote_resume_clears_old_automatic_pause(self):
        self.db.set_session_control(ACCOUNT,BUYER,'human','退款关键词','automatic')
        result=self.state(handoff=False,ai_takeover_enabled=True)
        self.assertTrue(result['ok']);self.assertTrue(result['changed'])
        self.assertEqual(self.db.session_control(ACCOUNT,BUYER)['ai_mode'],'ai')
        self.assertTrue(self.brain.execute_command(self.command())['ok'])
    def test_remote_handoff_becomes_brain_authoritative(self):
        self.db.set_session_control(ACCOUNT,BUYER,'human','旧本地原因','automatic')
        self.state(handoff=True,handoff_reason='服务端转人工')
        control=self.db.session_control(ACCOUNT,BUYER)
        self.assertEqual(control['handoff_source'],'brain');self.assertEqual(control['handoff_reason'],'服务端转人工')
    def test_server_resume_replaces_stale_local_manual_pause(self):
        self.db.set_session_control(ACCOUNT,BUYER,'human','人工接管','manual')
        self.state(handoff=False,ai_takeover_enabled=True)
        self.assertEqual(self.db.session_control(ACCOUNT,BUYER)['ai_mode'],'ai')
        result=self.brain.execute_command(self.command(handoff_notice=True))
        self.assertTrue(result['ok']);self.app.send_text.assert_called_once()
    def test_central_send_command_executes_despite_local_handoff_snapshot(self):
        self.state(handoff=True)
        self.assertTrue(self.brain.execute_command(self.command())['ok'])
        self.app.send_text.assert_called_once()
        self.assertTrue(self.brain.execute_command(self.command(handoff_notice=True))['ok'])
        self.assertEqual(self.db.session_control(ACCOUNT,BUYER)['ai_mode'],'human')
    def test_notice_does_not_require_local_freshness(self):
        self.state(handoff=True);cmd=self.command(handoff_notice=True);cmd['meta']['expires_at_ms']=1
        self.assertTrue(self.brain.execute_command(cmd)['ok'])
        self.app.send_text.assert_called_once()
    def test_keywords_do_not_independently_pause_tmall(self):
        app=StandaloneBridge.__new__(StandaloneBridge);app.config={'delivery_enabled':True};app.db=self.db
        app.delivery=SimpleNamespace(wakeup=threading.Event());app.context=SimpleNamespace(enqueue=Mock())
        app.ingest_event({'account':ACCOUNT,'buyer_id':BUYER,'role':'user','content':'我要退款','msg_id':'refund'})
        self.assertEqual(self.db.session_control(ACCOUNT,BUYER)['ai_mode'],'ai')
    def test_polling_can_clear_tmall_automatic_controls_but_not_other_shops(self):
        self.db.set_session_control(ACCOUNT,BUYER,'human','旧关键词','automatic')
        self.db.set_session_control('其他店',BUYER,'human','旧关键词','automatic')
        self.assertIn((ACCOUNT,BUYER),self.db.brain_handoff_controls())
        self.assertNotIn(('其他店',BUYER),self.db.brain_handoff_controls())
        self.brain.request=Mock(return_value={'sessions':[]})
        self.brain.sync_brain_handoffs()
        self.assertEqual(self.db.session_control(ACCOUNT,BUYER)['ai_mode'],'ai')
    def test_explicit_server_ai_pause_survives_handoff_poll_until_resume(self):
        self.state(handoff=False,ai_takeover_enabled=False)
        self.brain.request=Mock(return_value={'sessions':[]});self.brain.sync_brain_handoffs()
        self.assertEqual(self.db.session_control(ACCOUNT,BUYER)['ai_mode'],'human')
        self.state(handoff=False,ai_takeover_enabled=True)
        self.assertEqual(self.db.session_control(ACCOUNT,BUYER)['ai_mode'],'ai')
    def test_invalid_or_out_of_scope_state_never_changes_control(self):
        self.assertFalse(self.state(handoff='false')['ok'])
        self.brain.account_allowed=lambda _:False
        self.assertFalse(self.state(handoff=False)['ok'])
    def test_projection_distinguishes_delivery_and_failure(self):
        self.assertEqual(projection_status({'delivery_status':'confirmed'}),'confirmed')
        self.assertEqual(projection_status({'auto_send_status':'accepted'}),'submitted')
        self.assertEqual(projection_status({'auto_send_status':'shadow_only'}),'not_sent')
        self.db.upsert_local_projection({'msg_id':'draft','account':ACCOUNT,'buyer_id':BUYER,'role':'assistant_simulated',
            'content':'生成的回复','auto_send_status':'blocked','error':'session in human handoff'})
        rows=self.db.workbench_messages(ACCOUNT,BUYER)
        row=next(r for r in rows if r['msg_id']=='draft')
        self.assertEqual(row['status'],'not_sent');self.assertEqual(row['error'],'session in human handoff')

if __name__=='__main__':unittest.main()
