"""Order lookup must survive an incapable page winning the response race."""
import gc
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from standalone_bridge import BrowserServer, ContextEnricher, StateDB


ACCOUNT = "联想官方旗舰店:测试"
NO_SDK = {"ok": False, "error": "Qianniu imsdk is unavailable"}


def order_response(orders):
    return {"ok": True, "value": {
        "ok": True,
        "items": {"ok": True, "value": {"data": {"underInquiryItemList": []}}},
        "orders": {"ok": True, "value": {"data": {"orderList": orders}}},
    }}


def browser_with_responses(*responses):
    browser = BrowserServer(SimpleNamespace())
    for index in range(len(responses)):
        browser.connections[index] = threading.Lock()

    def send(connection, payload):
        browser.pending[payload["request_id"]].put_nowait(responses[connection])

    browser.send = Mock(side_effect=send)
    return browser


class ContextChannelRoutingTests(unittest.TestCase):
    def fetch(self, browser, account=ACCOUNT):
        app = SimpleNamespace(config={}, browser=browser)
        with patch("standalone_bridge.time.sleep"):
            return ContextEnricher(app).fetch("123456789", account=account)

    def test_fast_page_without_sdk_does_not_hide_order_page(self):
        browser = browser_with_responses(
            NO_SDK, order_response([{"bizOrderId": "1234567890123456789"}]),
        )
        result = self.fetch(browser)
        self.assertEqual(result["context_enrich"]["status"], "found")
        self.assertEqual(result["context_enrich"]["retry_count"], 0)
        self.assertEqual(result["order_info"]["order_count"], 1)
        self.assertEqual(browser.send.call_count, 2)
        self.assertEqual(browser.pending, {})

    def test_fast_empty_control_result_does_not_hide_order_page(self):
        browser = browser_with_responses(
            {"ok": True, "value": None},
            order_response([{"bizOrderId": "1234567890123456789"}]),
        )
        self.assertEqual(self.fetch(browser)["context_enrich"]["status"], "found")

    def test_delayed_capable_page_still_wins_after_sdk_rejection(self):
        browser = browser_with_responses(NO_SDK, order_response([]))
        original_send = browser.send.side_effect
        timers = []

        def send(connection, payload):
            if connection == 0:
                original_send(connection, payload)
            else:
                timer = threading.Timer(0.01, original_send, args=(connection, payload))
                timers.append(timer)
                timer.start()

        browser.send.side_effect = send
        try:
            self.assertEqual(self.fetch(browser)["context_enrich"]["status"], "confirmed_empty")
        finally:
            for timer in timers:
                timer.join()

    def test_timeout_of_another_page_is_not_reported_as_missing_sdk(self):
        browser = browser_with_responses(NO_SDK, order_response([]))
        original_send = browser.send.side_effect
        browser.send.side_effect = lambda connection, payload: (
            original_send(connection, payload) if connection == 0 else None
        )
        app = SimpleNamespace(config={"context_enrich_timeout_seconds": 0.01}, browser=browser)
        with patch("standalone_bridge.time.sleep"):
            result = ContextEnricher(app).fetch("123456789", account=ACCOUNT)
        self.assertEqual(result["context_enrich"]["status"], "error_unknown")
        self.assertEqual(result["context_enrich"]["retry_count"], 2)
        self.assertIn("timed out", result["context_enrich"]["error"])
        self.assertEqual(browser.pending, {})

    def test_no_sdk_is_reported_once_instead_of_three_identical_attempts(self):
        browser = browser_with_responses(NO_SDK, NO_SDK)
        result = self.fetch(browser)
        self.assertEqual(result["context_enrich"]["status"], "error_unknown")
        self.assertIn("imsdk is unavailable", result["context_enrich"]["error"])
        self.assertEqual(result["context_enrich"]["retry_count"], 0)
        self.assertEqual(browser.send.call_count, 2)
        self.assertFalse(result["order_info"]["context_received"])
        self.assertFalse(result["order_info"].get("no_orders", False))

    def test_transport_error_is_also_published_for_other_shops(self):
        result = self.fetch(browser_with_responses(NO_SDK), account="other-shop")
        self.assertEqual(result["context_enrich"]["status"], "error_unknown")
        self.assertIn("imsdk is unavailable", result["context_enrich"]["error"])

    def test_disconnected_channel_does_not_retry(self):
        result = self.fetch(browser_with_responses())
        self.assertEqual(result["context_enrich"]["status"], "error_unknown")
        self.assertEqual(result["context_enrich"]["retry_count"], 0)
        self.assertIn("not connected", result["context_enrich"]["error"])

    def test_business_failure_still_retries_and_is_never_no_orders(self):
        response = order_response([])
        response["value"]["orders"] = {"ok": False, "error": "session expired"}
        browser = browser_with_responses(NO_SDK, response)
        result = self.fetch(browser)
        self.assertEqual(result["context_enrich"]["status"], "error_unknown")
        self.assertEqual(result["context_enrich"]["retry_count"], 2)
        self.assertIn("session expired", result["context_enrich"]["error"])
        self.assertFalse(result["order_info"].get("no_orders", False))

    def test_optional_image_result_filter_still_returns_none_on_no_match(self):
        browser = browser_with_responses(NO_SDK)
        self.assertIsNone(browser.execute("image", result_filter=lambda value: False))

    def test_confirmed_empty_requires_order_response_and_remains_unconfirmed_for_tmall(self):
        browser = browser_with_responses(NO_SDK, order_response([]))
        result = self.fetch(browser)
        self.assertEqual(result["context_enrich"]["status"], "confirmed_empty")
        self.assertEqual(result["context_enrich"]["retry_count"], 2)
        self.assertTrue(result["order_info"]["empty_response_only"])
        self.assertFalse(result["order_info"]["no_orders"])


class ContextEnqueueEvidenceTests(unittest.TestCase):
    def test_worker_reports_found_empty_and_error_as_separate_counts(self):
        for status in ("found", "confirmed_empty", "error_unknown"):
            with self.subTest(status=status):
                stop = threading.Event()
                db = SimpleNamespace(
                    event_payload=Mock(return_value={"buyer_encrypt_id": "123456789"}),
                    enrich_event=Mock(side_effect=lambda *_: stop.set()),
                )
                enricher = ContextEnricher(SimpleNamespace(config={}, db=db, stop_event=stop))
                enricher.fetch = Mock(return_value={"context_enrich": {"status": status}})
                enricher.enqueue("event", {"buyer_encrypt_id": "123456789"})
                enricher.run()
                self.assertEqual(enricher.status()["result_counts"], {status: 1})
                self.assertEqual(enricher.successes, int(status != "error_unknown"))

    def test_missing_buyer_id_leaves_failure_evidence_on_durable_event(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(Path(directory) / "state.sqlite3")
            event_id, _ = db.upsert_event({
                "platform": "taobao", "msg_id": "no-uid", "role": "user",
                "account": ACCOUNT, "buyer_id": "unknown", "content": "hello",
            })
            enricher = ContextEnricher(SimpleNamespace(config={}, db=db))
            enricher.enqueue(event_id, db.event_payload(event_id))
            payload = db.event_payload(event_id)
            self.assertEqual(payload["context_enrich"]["status"], "error_unknown")
            self.assertEqual(payload["context_enrich"]["error"], "missing_buyer_encrypt_id")
            self.assertFalse(payload["context_enrich"]["ok"])
            self.assertNotIn("no_orders", payload.get("order_info", {}))
            self.assertTrue(enricher.pending.empty())
            self.assertEqual(enricher.status()["skip_reasons"]["missing_buyer_encrypt_id"], 1)
            gc.collect()

    def test_enqueue_diagnostics_distinguish_disabled_role_and_duplicate(self):
        app = SimpleNamespace(config={"context_enrich_enabled": False})
        enricher = ContextEnricher(app)
        buyer = {"account": "other-shop", "role": "user", "buyer_encrypt_id": "123456789"}
        enricher.enqueue("disabled", buyer)
        app.config["context_enrich_enabled"] = True
        enricher.enqueue("seller", dict(buyer, role="seller"))
        enricher.enqueue("buyer", buyer)
        enricher.enqueue("buyer", buyer)
        status = enricher.status()
        self.assertEqual(status["enqueue_calls"], 4)
        self.assertEqual(status["enqueued"], 1)
        self.assertEqual(status["skip_reasons"], {
            "disabled": 1, "role_filtered": 1, "already_queued": 1,
        })


if __name__ == "__main__":
    unittest.main()
