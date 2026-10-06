import unittest
from taobao_message_contract import classification, notification_kind, select_parent
from tmall_delivery_guard import blocked_reason
ACCOUNT='联想官方旗舰店:燕燕'
NOTICES=[
 '预计18小时内发货，预计10月9日送达',
 '亲，客服帮你申请的退款已过期，请与客服重新沟通~',
 '您已成功发送退款挽留方案。请密切关注消费者的反馈，必要时进行服务跟进，有助于提升挽留成功率哦~',
 '买家近期的咨询不满意风险高，请做好用户接待，及时解决问题，可有效提升满意度、降低平台求助率。']
def event(mid,text,ts):
    return {'msg_id':mid,'role':'user','account':ACCOUNT,'content':text,'ts':ts,'captured_at_ms':ts*1000}
class ScopedNoticeTests(unittest.TestCase):
    def test_each_notice_keeps_command_parent_and_does_not_block_send(self):
        for text in NOTICES:
            with self.subTest(text=text):
                p=event('p','套餐延期四天',100);n=event('n',text,101)
                self.assertEqual(classification(n),'context')
                latest=select_parent([p,n],'p')
                self.assertEqual(latest['msg_id'],'p')
                self.assertEqual(blocked_reason({'takeover_parent_msg_id':'p','takeover_parent_ts':100,'expires_at_ms':200000},latest,now=110),'')
    def test_other_shops_are_unchanged(self):
        for text in NOTICES:
            n=event('n',text,101);n['account']='其他店:客服'
            self.assertEqual(classification(n),'buyer')
    def test_real_buyer_questions_are_preserved(self):
        for text in ['退款已过期怎么办？','请尽快回复我','预计18小时内发货，预计10月9日送达，是真的吗？']:
            self.assertEqual(classification(event('p',text,100)),'buyer')
    def test_transfer_still_has_first_response_parent(self):
        t=event('t','由 雪晴 转交给 燕燕',100)
        self.assertEqual(notification_kind(t),'transfer')
        self.assertEqual(select_parent([t],'t')['msg_id'],'t')
    def test_actual_new_buyer_question_still_blocks_stale_reply(self):
        rows=[event('p','原问题',100),event('n',NOTICES[0],101),event('new','退货怎么处理',102)]
        self.assertEqual(select_parent(rows,'p')['msg_id'],'new')
