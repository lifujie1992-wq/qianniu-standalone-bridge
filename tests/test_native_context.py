import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from native_context import NativeContext, native_event, unpack_content
from standalone_bridge import StateDB, normalize_event, BrainConnector, Config

ACCOUNT = '联想官方旗舰店:花花'
BUYER = '100.1-200.1#11001@cntaobao'
URL = 'https://img.alicdn.com/chat.jpg'
STAMP = 1792245438.123


def pack(value):
    if value is None:
        return b'\xc0'
    if isinstance(value,int) and 0<=value<128:
        return bytes([value])
    if isinstance(value,str):
        data=value.encode()
        return b'\xda'+len(data).to_bytes(2,'big')+data
    if isinstance(value,dict):
        return bytes([128+len(value)])+b''.join(pack(k)+pack(v) for k,v in value.items())
    raise TypeError(value)


def row(mid='123.PNM', text='重置套餐可以吗', image=False, stamp=STAMP, sender='100@cntaobao'):
    content={1:101,3:{1:7,2:json.dumps({'url':URL,'width':100,'height':200})}} if image else {1:1,2:{1:text}}
    ext={'sender_nick':'cntaobaotb_7715324','receiver_nick':'cntaobao'+ACCOUNT}
    return [BUYER,mid,sender,str(round(stamp*1000)),str(101 if image else 1),text,pack(content).hex(),json.dumps(ext),'0','0']


class NativeContentTests(unittest.TestCase):
    def test_native_text_keeps_original_platform_identity_and_time(self):
        event=native_event(row(),ACCOUNT,BUYER)
        self.assertEqual(event['content'],'重置套餐可以吗')
        self.assertEqual(event['msg_id'],'123.PNM')
        self.assertEqual(event['ts'],STAMP)
        self.assertEqual(event['nickname'],'tb_7715324')
        self.assertEqual(event['sender_uid'],'100')
        self.assertEqual(event['sender_nick'],'tb_7715324')
        self.assertEqual(event['capture_mode'],'history_snapshot')

    def test_image_reads_original_media_from_its_own_packed_body(self):
        event=native_event(row(image=True),ACCOUNT,BUYER)
        self.assertEqual(event['image_url'],URL)
        self.assertEqual(event['content'],URL)
        self.assertEqual(event['raw_type'],'image')

    def test_recalled_deleted_wrong_conversation_and_unknown_staff_are_rejected(self):
        for index,value in [(8,'1'),(9,'1'),(0,'999.1-126446588.1#11001@cntaobao'),(2,'999@cntaobao')]:
            value_row=row();value_row[index]=value
            self.assertIsNone(native_event(value_row,ACCOUNT,BUYER))

    def test_staff_from_another_seat_in_same_shop_is_context(self):
        value=row(sender='999@cntaobao')
        value[7]=json.dumps({'sender_nick':'cntaobao联想官方旗舰店:京京','receiver_nick':'cntaobaotb_7715324'})
        event=native_event(value,ACCOUNT,BUYER)
        self.assertEqual(event['role'],'mall_cs')
        self.assertEqual(event['sender_uid'],'999')
        self.assertEqual(event['staff_name'],'联想官方旗舰店:京京')
        self.assertEqual(event['recipient_nick'],'tb_7715324')

    def test_truncation_and_unbounded_structures_fail_closed(self):
        for value in [b'',b'\xda\xff\xff',b'\x81\x01',b'\xdf\xff\xff\xff\xff',b'\x01\x02']:
            with self.assertRaises((ValueError,TypeError)):
                unpack_content(value)


class NativePrepareTests(unittest.TestCase):
    def make_context(self,rows):
        reader=Mock(return_value={'ok':True,'rows':rows})
        app=SimpleNamespace(config={},appbiz=SimpleNamespace(read_messages=reader),
                            db=SimpleNamespace(remember_native_context=Mock()),
                            browser=SimpleNamespace(lookup_image_url=Mock(return_value='')))
        return NativeContext(app),app

    def test_transfer_context_precedes_parent_and_excludes_future_messages(self):
        context,app=self.make_context([row(stamp=STAMP-20),row('future.PNM',stamp=STAMP+1)])
        history_rows=app.appbiz.read_messages.return_value['rows']
        app.appbiz.read_messages.side_effect=lambda *args: {'ok':True,'rows':[row('transfer.PNM',text='由 小迎 转交给 花花')]} if args[4] else {'ok':True,'rows':history_rows}
        event={'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'transfer.PNM','content':'由 小迎 转交给 花花','ts':STAMP}
        updated,history=context.prepare(event)
        self.assertEqual([x['msg_id'] for x in history],['123.PNM'])
        self.assertEqual(updated['native_history']['status'],'loaded')
        self.assertEqual(event['content'],updated['content'])
        self.assertTrue(all(x['capture_mode']=='history_snapshot' for x in history))
        self.assertLess(app.appbiz.read_messages.call_args.args[3],STAMP*1000)

    def test_transfer_to_another_seat_and_history_replay_do_not_fetch(self):
        context,app=self.make_context([])
        for event in [{'content':'由 小迎 转交给 京京'}, {'content':'由 小迎 转交给 花花','capture_mode':'history_snapshot'}]:
            context.prepare(dict(event,account=ACCOUNT,buyer_id=BUYER,ts=STAMP))
        app.appbiz.read_messages.assert_not_called()

    def test_delayed_transfer_uses_native_platform_day_and_keeps_original_timestamp(self):
        context,app=self.make_context([])
        app.appbiz.read_messages.side_effect=lambda *args: {'ok':True,'rows':[row('transfer.PNM',text='由 小迎 转交给 花花')]} if args[4] else {'ok':True,'rows':[row(stamp=STAMP-20)]}
        receipt=STAMP+2*86400
        updated,history=context.prepare({'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'transfer.PNM','content':'由 小迎 转交给 花花','ts':receipt})
        self.assertEqual(updated['ts'],receipt)
        self.assertEqual(updated['native_history']['platform_timestamp'],STAMP)
        self.assertEqual(len(history),1)
        self.assertLess(app.appbiz.read_messages.call_args.args[2],receipt*1000)

    def test_missing_transfer_anchor_is_visible_as_unavailable(self):
        context,app=self.make_context([])
        updated,history=context.prepare({'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'transfer.PNM','content':'由 小迎 转交给 花花','ts':STAMP})
        self.assertFalse(history)
        self.assertEqual(updated['native_history']['status'],'unavailable')

    def test_image_uses_exact_native_message_and_preserves_caption(self):
        context,app=self.make_context([row(image=True)])
        original={'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'123.PNM','content':'这张截图上的时间不对','raw_type':'image','ts':STAMP}
        updated,history=context.prepare(original)
        self.assertEqual(updated['image_url'],URL)
        self.assertEqual(updated['content'],original['content'])
        self.assertEqual(app.appbiz.read_messages.call_args.args[4],'123.PNM')
        self.assertFalse(history)

    def test_missing_native_store_falls_back_to_existing_browser_media_lookup(self):
        context,app=self.make_context([])
        app.appbiz.read_messages.return_value={'ok':False,'error':'message_database_not_observed'}
        app.browser.lookup_image_url.return_value=URL
        updated,_=context.prepare({'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'123.PNM','content':'[图片]','ts':STAMP})
        self.assertEqual(updated['image_url'],URL)

    def test_different_message_image_is_never_substituted(self):
        context,app=self.make_context([row('other.PNM',image=True)])
        updated,_=context.prepare({'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'123.PNM','content':'[图片]','ts':STAMP})
        self.assertNotIn('image_url',updated)

    def test_local_backfill_preserves_realtime_mode_and_delivery_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            db=StateDB(Path(folder)/'bridge.sqlite3')
            original={'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'123.PNM','content':'[图片]','ts':STAMP,'capture_mode':'appbiz_native_callback'}
            event_id,_=db.upsert_event(original)
            db.enqueue_brain_event(event_id)
            before=db.brain_event_counts()
            db.remember_native_context([native_event(row(image=True),ACCOUNT,BUYER),native_event(row('older.PNM',stamp=STAMP-10),ACCOUNT,BUYER)])
            saved=db.event_payload(event_id)
            self.assertEqual(saved['capture_mode'],'appbiz_native_callback')
            self.assertEqual(saved['image_url'],URL)
            self.assertEqual({key:db.brain_event_counts()[key] for key in ('pending','sending','delivered')},
                             {key:before[key] for key in ('pending','sending','delivered')})
            self.assertEqual(len(db.pending()),1)
            self.assertEqual(db.event_status(normalize_event({'platform':'taobao','msg_id':'older.PNM'})['event_id']),'delivered')

    def test_local_enrichment_does_not_invalidate_an_active_delivery_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            db=StateDB(Path(folder)/'bridge.sqlite3')
            event_id,_=db.upsert_event({'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'123.PNM','content':'[图片]','ts':STAMP})
            claimed=db.claim_pending()[0]
            db.remember_native_context([native_event(row(image=True),ACCOUNT,BUYER)])
            self.assertTrue(db.mark_delivered(event_id,claimed['revision']))

    def test_native_history_role_correction_preserves_delivery_and_platform_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            db=StateDB(Path(folder)/'bridge.sqlite3')
            native=row(sender='999@cntaobao')
            native[7]=json.dumps({'sender_nick':'cntaobao联想官方旗舰店:京京','receiver_nick':'cntaobaotb_7715324'})
            corrected=native_event(native,ACCOUNT,BUYER)
            old=dict(corrected,role='seller')
            db.remember_native_context([old])
            event_id=normalize_event(old)['event_id']
            before=db.event_payload(event_id)
            db.remember_native_context([corrected])
            saved=db.event_payload(event_id)
            self.assertEqual(saved['role'],'mall_cs')
            self.assertEqual(db.event_status(event_id),'delivered')
            self.assertEqual(len(db.pending()),0)
            for key in ('event_id','account','buyer_id','msg_id','ts','original_timestamp','content','capture_mode'):
                self.assertEqual(saved[key],before[key])

    def test_native_history_cannot_reclassify_a_live_event(self):
        with tempfile.TemporaryDirectory() as folder:
            db=StateDB(Path(folder)/'bridge.sqlite3')
            history=native_event(row(),ACCOUNT,BUYER)
            for index,source in enumerate(('appbiz_native_callback','another_history_reader')):
                incoming=dict(history,msg_id=f'{index}.PNM')
                original=dict(incoming,role='unknown',source=source,
                              capture_mode='live' if index==0 else 'history_snapshot')
                event_id,_=db.upsert_event(original)
                before=db.event_payload(event_id)
                db.remember_native_context([incoming])
                self.assertEqual(db.event_payload(event_id),before)


class NativeUploadTests(unittest.TestCase):
    def test_history_is_acknowledged_before_parent_and_primary_ack_mapping_is_preserved(self):
        self.check_upload(True)

    def test_uncommitted_history_prevents_uploading_the_transfer_parent(self):
        self.check_upload(False)

    def check_upload(self, committed):
        with tempfile.TemporaryDirectory() as folder:
            db=StateDB(Path(folder)/'db.sqlite3')
            config=Config(Path(folder)/'config.json',{'brain_agent_id':'test-agent','brain_ws_enabled':False})
            history=[native_event(row(str(i)+'.PNM',stamp=STAMP-100+i),ACCOUNT,BUYER) for i in range(100)]
            parent=normalize_event({'account':ACCOUNT,'buyer_id':BUYER,'msg_id':'parent.PNM','role':'user','content':'由 小迎 转交给 花花','ts':STAMP})
            app=SimpleNamespace(config=config,db=db,stop_event=threading.Event(),
                                native_context=SimpleNamespace(prepare=lambda event:(event,history)))
            brain=BrainConnector(app)
            brain.watch_draft=Mock()
            def response(method,path,body):
                return {'event_acks':[{'event_id':e['event_id'],'committed':committed,'retryable':not committed,'status':'context_traced' if e.get('capture_mode')=='history_snapshot' else 'accepted'} for e in body['events']]}
            brain.request=Mock(side_effect=response)
            claimed=[{'event_id':parent['event_id'],'revision':'r','payload':parent}]
            if committed:
                result=brain.upload_events(claimed)
                self.assertIn(parent['event_id'],result)
                batches=[call.args[2]['events'] for call in brain.request.call_args_list]
                self.assertEqual([len(x) for x in batches],[100,1])
                self.assertTrue(all(e['capture_mode']=='history_snapshot' for e in batches[0]))
                self.assertEqual(batches[1][0]['event_id'],parent['event_id'])
                self.assertEqual(brain.watch_draft.call_count,1)
            else:
                with self.assertRaisesRegex(RuntimeError,'持久化确认'):
                    brain.upload_events(claimed)
                self.assertEqual(brain.request.call_count,1)


if __name__=='__main__':
    unittest.main()
