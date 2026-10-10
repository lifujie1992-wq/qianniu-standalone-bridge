import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from standalone_bridge import ContextEnricher, BrainConnector

ACCOUNT = "联想官方旗舰店:燕燕"


def response(orders, ret=None):
    value = {"data": {"orderList": orders}}
    if ret is not None:
        value["ret"] = ret
    return {
        "items": {"ok": True, "value": {"data": {"underInquiryItemList": []}}},
        "orders": {"ok": True, "value": value},
    }


class TmallContextEvidenceTests(unittest.TestCase):
    def app(self, payload):
        return SimpleNamespace(
            config={}, browser=SimpleNamespace(execute=Mock(return_value=payload))
        )

    def test_raw_order_fields_and_buyer_binding_survive_upload(self):
        raw = {
            "bizOrderId": "1234567890123456789",
            "unknownSkuField": {"card": "evidence"},
        }
        result = ContextEnricher(self.app(response([raw]))).fetch(
            "123456789", account=ACCOUNT
        )
        evidence = BrainConnector.compatible_context_event(result)["order_info"][
            "raw_context"
        ]
        self.assertEqual(evidence["buyer_encrypt_id"], "123456789")
        self.assertEqual(evidence["account"], ACCOUNT)
        self.assertEqual(evidence["orders"]["data"]["orderList"], [raw])

    def test_mtop_business_failure_is_not_no_orders(self):
        with patch("standalone_bridge.time.sleep"):
            result = ContextEnricher(
                self.app(response([], ["FAIL_SYS_SESSION_EXPIRED::expired"]))
            ).fetch("123456789", account=ACCOUNT)
        self.assertEqual(result["local_context_lookup"]["status"], "error_unknown")
        self.assertFalse(result["order_info"].get("no_orders", False))

    def test_unknown_schema_is_not_no_orders(self):
        payload = response([])
        payload["orders"]["value"] = {"data": {"unknownOrders": []}}
        with patch("standalone_bridge.time.sleep"):
            result = ContextEnricher(self.app(payload)).fetch(
                "123456789", account=ACCOUNT
            )
        self.assertEqual(result["local_context_lookup"]["status"], "error_unknown")

    def test_transfer_event_enriches_without_local_role_filter(self):
        enricher = ContextEnricher(SimpleNamespace(config={}))
        enricher.enqueue(
            "transfer",
            {"account": ACCOUNT, "role": "system", "buyer_encrypt_id": "123456789"},
        )
        self.assertEqual(enricher.pending.get_nowait(), "transfer")

    def test_other_shop_keeps_legacy_status_and_role_filter(self):
        with patch("standalone_bridge.time.sleep"):
            result = ContextEnricher(
                self.app(response([], ["FAIL_SYS_SESSION_EXPIRED"]))
            ).fetch("123456789", account="other")
        self.assertEqual(result["local_context_lookup"]["status"], "confirmed_empty")
        self.assertNotIn("raw_context", result["order_info"])
        enricher = ContextEnricher(SimpleNamespace(config={}))
        enricher.enqueue(
            "transfer",
            {"account": "other", "role": "system", "buyer_encrypt_id": "123456789"},
        )
        self.assertTrue(enricher.pending.empty())

    def test_requested_order_does_not_reuse_different_cached_order(self):
        enricher = ContextEnricher(self.app(response([])))
        enricher._remember_order(
            (ACCOUNT, "123456789"), [{"order_id": "9999999999999999999"}]
        )
        with patch("standalone_bridge.time.sleep"):
            result = enricher.fetch("123456789", "1234567890123456789", ACCOUNT)
        self.assertNotIn("selected_order", result["order_info"])
        self.assertFalse(result["order_info"]["no_orders"])
        self.assertEqual(
            result["order_info"]["raw_context"]["requested_order_id"],
            "1234567890123456789",
        )

    def test_query_uses_event_buyer_and_retains_all_product_fields(self):
        payload = response([{"bizOrderId": "1234567890123456789"}])
        payload["items"]["value"]["data"]["underInquiryItemList"] = [
            {"itemId": "123456", "title": "商品", "unknownSku": "套餐A"}
        ]
        app = self.app(payload)
        result = ContextEnricher(app).fetch("123456789", account=ACCOUNT)
        expression = app.browser.execute.call_args.args[0]
        self.assertIn('const encryptId = "123456789"', expression)
        self.assertIn("securityBuyerUid: encryptId", expression)
        self.assertEqual(
            result["order_info"]["raw_context"]["items"], payload["items"]["value"]
        )
