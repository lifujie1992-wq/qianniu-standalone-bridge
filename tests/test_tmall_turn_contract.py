import unittest
from taobao_message_contract import classification, epoch, message_id, select_parent, verified_batch_parent

def event(mid, ts=100, captured=100, content='买家问题', role='user', mode='browser_live', **kw):
    return dict(msg_id=mid,ts=ts,captured_at_ms=captured*1000,content=content,role=role,capture_mode=mode,**kw)

class TurnContractTests(unittest.TestCase):
    def test_all_timestamp_units_and_id_prefixes(self):
        for value in (1791282104,1791282104000,1791282104000000,1791282104000000000):
            self.assertEqual(epoch(value),1791282104)
        self.assertEqual(message_id('qn-msg-v1|taobao|qn-msg-v1|taobao|a'),'a')
        for value in (float('nan'),float('inf'),0,-1,'invalid'):
            self.assertEqual(epoch(value),0)

    def test_native_browser_clock_difference_does_not_replace_parent(self):
        rows=[event('t',107,107,'由 小韩韩 转交给 燕燕'),event('p',100,109)]
        self.assertEqual(select_parent(rows,'p')['msg_id'],'p')

    def test_whole_admitted_text_voice_image_batch_is_one_turn(self):
        rows=[event('p',100,100),event('voice',101,101,'https://siyou.alicdn.com/a.amr'),event('image',102,102,'https://img.alicdn.com/b.jpg')]
        self.assertEqual(select_parent(rows,'p',['p','voice','image'])['msg_id'],'p')
        self.assertTrue(select_parent(rows,'p',['p','voice'])['_parent_guard_superseded'])

    def test_new_live_question_supersedes_even_with_clock_going_backwards(self):
        rows=[event('p',100,100),event('new',99,101)]
        self.assertTrue(select_parent(rows,'p')['_parent_guard_superseded'])

    def test_late_old_history_does_not_supersede_live_question(self):
        for mode in ('GetLocalHisMsg','GetRemoteHisMsg','history_snapshot','poll:history'):
            rows=[event('p',100,100),event('old',99,101,mode=mode)]
            self.assertEqual(select_parent(rows,'p')['msg_id'],'p')

    def test_late_browser_cache_does_not_replace_new_native_question(self):
        rows=[event('p',100,100,mode='appbiz_native_callback'),event('old',99,101,mode='event-local-db:im.singlemsg.onReceiveNewMsg')]
        self.assertEqual(select_parent(rows,'p')['msg_id'],'p')

    def test_context_staff_unknown_and_incomplete_do_not_replace_question(self):
        rows=[event('p')]
        rows += [event('origin',101,101,'当前用户来自 搜索结果页'),event('staff',102,102,role='mall_cs'),event('unknown',103,103,role='unknown'),event('incomplete',104,104,incomplete=True)]
        self.assertEqual(select_parent(rows,'p')['msg_id'],'p')

    def test_missing_parent_is_not_invented_from_batch(self):
        rows=[event('other')]
        self.assertNotEqual(select_parent(rows,'missing',['other'])['msg_id'],'missing')

    def test_media_product_card_and_real_words_are_buyer_messages(self):
        for text in ('[图片]','[视频]','https://siyou.alicdn.com/a.amr','https://item.taobao.com/item.htm?id=1','请尽快回复我','当前用户来自商品详情页是什么意思？'):
            self.assertEqual(classification(event('p',content=text)),'buyer')

    def test_batch_parent_can_replace_any_filtered_task_trigger(self):
        rows=[event('p',content='快递'),event('card',content='预计明天送达')]
        self.assertEqual(verified_batch_parent(rows,'card',[{'msg_id':'p','content':'快递'}]),('p',['p']))

    def test_bad_batch_ids_contents_roles_do_not_authorize_parent(self):
        rows=[event('p'),event('staff',role='mall_cs')]
        for q in ({'msg_id':'missing','content':'买家问题'},{'msg_id':'p','content':'另一问题'},{'msg_id':'staff','content':'买家问题'}):
            self.assertEqual(verified_batch_parent(rows,'original',[q]),('original',[]))

    def test_new_buyer_after_transfer_supersedes_first_response(self):
        rows=[event('t',100,100,'由 雪晴 转交给 燕燕'),event('p',99,101)]
        self.assertTrue(select_parent(rows,'t')['_parent_guard_superseded'])
