import importlib.util,sys,tempfile,unittest
from pathlib import Path
source=Path(sys.argv.pop(1)); sys.path.insert(0,str(source.parent))
spec=importlib.util.spec_from_file_location('gateway_under_test',source); gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)
BUYER='2212089943099.1-126446588.1#11001@cntaobao'; ACCOUNT='联想官方旗舰店:小山'
class NicknameFlow(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'seat.json';self.state=gateway.LocalSeatState(self.path)
 def tearDown(self):self.temp.cleanup()
 def message(self,**kw):
  event=dict(account=ACCOUNT,buyer_id=BUYER,platform='taobao',shop_id='tb_lenovo',content='充电的时候正常亮',event_id='qn-msg-v1|taobao|4343025034808.PNM',buyer_nick='2212089943099',ts=1791530668);event.update(kw);return event
 def update(self,**kw):
  event=dict(type='nickname_update',account=ACCOUNT,buyer_id=BUYER,platform='taobao',nickname='dysileib',event_id='fixture-nick');event.update(kw);return event
 def test_metadata_updates_exact_session_without_chat_or_unread(self):
  self.state.publish(self.message());before=dict(self.state.sessions[self.state._key(ACCOUNT,BUYER)])
  ack=self.state.publish(self.update());row=self.state.sessions[self.state._key(ACCOUNT,BUYER)]
  self.assertEqual(row['nickname'],'dysileib');self.assertTrue(ack['committed'])
  for key in ('buyer_id','account','msg_count','last_ts','last_content','unread'):self.assertEqual(row[key],before[key])
  self.assertFalse(self.state.publish(self.update())['accepted'])
  restored=gateway.LocalSeatState(self.path);self.assertEqual(restored.sessions[restored._key(ACCOUNT,BUYER)]['nickname'],'dysileib')
  self.state.publish(self.message(event_id='second',buyer_nick='2212089943099'))
  self.assertEqual(row['nickname'],'dysileib')
 def test_update_before_message_and_cross_account_isolation(self):
  self.state.publish(self.update());self.state=gateway.LocalSeatState(self.path);self.state.publish(self.message())
  self.assertEqual(self.state.sessions[self.state._key(ACCOUNT,BUYER)]['nickname'],'dysileib')
  self.state.publish(self.message(account='其他店:客服',event_id='other'))
  self.assertNotEqual(self.state.sessions[self.state._key('其他店:客服',BUYER)]['nickname'],'dysileib')
 def test_remote_real_name_survives_newer_local_placeholder(self):
  self.state.publish(self.message());key='/api/sessions?scope=active'
  self.state.remote_cache[key]={'sessions':[dict(self.state.sessions[self.state._key(ACCOUNT,BUYER)],nickname='dysileib',last_ts=1791530667)]}
  self.assertEqual(self.state.merge_sessions(key)['sessions'][0]['nickname'],'dysileib')
 def test_numeric_or_wrong_platform_metadata_rejected(self):
  self.state.publish(self.message())
  for bad in (self.update(nickname='2212089943099'),self.update(nickname=BUYER),self.update(platform='pdd')):
   with self.assertRaises(ValueError):self.state.publish(bad)
if __name__=='__main__':unittest.main()
