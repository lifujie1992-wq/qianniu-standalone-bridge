import unittest
from standalone_bridge import preferred_buyer_nick


class BuyerIdentityRegressionTests(unittest.TestCase):
    def test_customer_name_never_comes_from_staff_message(self):
        payload={"messages":[
            {"role":"user","nickname":"买家小花"},
            {"role":"mall_cs","nickname":"瑞秋"},
            {"role":"assistant_simulated","nickname":"AI助手"},
        ]}
        self.assertEqual(preferred_buyer_nick(payload,"123.1-456.1#11001@cntaobao","123"),"买家小花")

    def test_missing_customer_name_does_not_use_staff_name(self):
        payload={"messages":[{"role":"mall_cs","nickname":"瑞秋"}]}
        self.assertEqual(preferred_buyer_nick(payload,"123.1-456.1#11001@cntaobao","123"),"")
