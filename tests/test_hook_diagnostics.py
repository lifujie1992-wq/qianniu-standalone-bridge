"""任务 A（1.6.28）单测：URL 脱敏、页面归类、失败原因、汇总、日志跃变。"""

from __future__ import annotations

import unittest

import hook_diagnostics as hd


class SanitizeTests(unittest.TestCase):
    def test_strips_query_and_fragment(self):
        url = "https://qn.taobao.com/web_chat-packer/recent.html?ccode=1234567890&token=abc#/x"
        self.assertEqual(
            hd.sanitize_page_url(url),
            "https://qn.taobao.com/web_chat-packer/recent.html",
        )

    def test_keeps_origin_and_path(self):
        self.assertEqual(
            hd.sanitize_page_url("https://h5.m.taobao.com/miniappNav/index.html?a=1"),
            "https://h5.m.taobao.com/miniappNav/index.html",
        )

    def test_truncates_long_path(self):
        url = "https://example.com/" + ("a" * 300)
        out = hd.sanitize_page_url(url)
        self.assertTrue(out.startswith("https://example.com/"))
        self.assertEqual(len(out.split("example.com", 1)[1]), 160)

    def test_rejects_non_http(self):
        self.assertEqual(hd.sanitize_page_url("about:blank"), "")
        self.assertEqual(hd.sanitize_page_url(""), "")
        self.assertEqual(hd.sanitize_page_url(None), "")

    def test_mask_text(self):
        text = hd.mask_text("boom https://a.example/x?token=1 order 1234567890123")
        self.assertIn("<url>", text)
        self.assertNotIn("1234567890123", text)
        self.assertIn("***", text)

    def test_mask_text_truncates(self):
        self.assertEqual(len(hd.mask_text("x" * 500)), 200)

    def test_mask_text_strips_control_chars(self):
        self.assertEqual(hd.mask_text("a\x00b\x1fc"), "a b c")


class PageKindTests(unittest.TestCase):
    def test_kinds(self):
        cases = {
            "https://qn/web_chat-packer/recent.html": "chat_recent",
            "https://qn/web_chat-packer/index.html": "chat_other",
            "https://h5/miniappNav/x.html": "miniapp",
            "https://qn/selectOrder/select.html": "select_order",
            "https://qn/whatever": "other",
            "": "other",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(hd.page_kind(url), expected)


class NormalizeConnectionTests(unittest.TestCase):
    def test_fills_a1_fields_and_keeps_legacy(self):
        raw = {
            "imsdk_hooked": False,
            "websocket_connected": True,
            "page_url": "https://qn/web_chat-packer/recent.html?x=1",
            "page_title": "千牛工作台",
            "hook_attempts": 3,
            "last_hook_attempt_at_ms": 1700000000000,
            "last_hook_error": "imsdk_undefined",
        }
        out = hd.normalize_connection(raw)
        self.assertEqual(out["page_url"], "https://qn/web_chat-packer/recent.html")
        self.assertEqual(out["page_kind"], "chat_recent")
        self.assertEqual(out["imsdk_typeof"], "undefined")
        self.assertEqual(out["imsdk_invoke_typeof"], "n/a")
        self.assertTrue(out["is_top_frame"])
        self.assertEqual(out["hook_attempts"], 3)
        self.assertEqual(out["last_hook_error"], "imsdk_undefined")
        self.assertEqual(out["first_hooked_at_ms"], 0)
        # 旧字段原样保留
        self.assertTrue(out["websocket_connected"])
        self.assertFalse(out["imsdk_hooked"])

    def test_derives_kind_when_missing(self):
        out = hd.normalize_connection({"page_url": "https://qn/selectOrder/a.html"})
        self.assertEqual(out["page_kind"], "select_order")

    def test_masks_error_and_title_length(self):
        out = hd.normalize_connection({
            "last_hook_error": "exception: Error: " + "x" * 400,
            "page_title": "t" * 100,
        })
        self.assertEqual(len(out["last_hook_error"]), 200)
        self.assertEqual(len(out["page_title"]), 60)


class SummaryTests(unittest.TestCase):
    def test_by_kind_and_chat_present(self):
        rows = [
            {"page_kind": "chat_recent", "imsdk_hooked": True},
            {"page_kind": "other", "imsdk_hooked": False},
            {"page_kind": "other", "imsdk_hooked": False},
        ]
        summary = hd.hook_summary(rows)
        self.assertEqual(summary["pages"], 3)
        self.assertEqual(summary["hooked"], 1)
        self.assertEqual(summary["by_kind"]["chat_recent"], {"pages": 1, "hooked": 1})
        self.assertEqual(summary["by_kind"]["other"], {"pages": 2, "hooked": 0})
        self.assertTrue(summary["chat_page_present"])
        self.assertNotIn("no_chat_page_hint", summary)

    def test_no_chat_page_sets_hint(self):
        summary = hd.hook_summary([{"page_kind": "other", "imsdk_hooked": False}])
        self.assertFalse(summary["chat_page_present"])
        self.assertEqual(summary["no_chat_page_hint"], hd.NO_CHAT_PAGE_HINT)

    def test_empty(self):
        summary = hd.hook_summary([])
        self.assertEqual(summary, {
            "pages": 0, "hooked": 0, "by_kind": {},
            "chat_page_present": False,
            "no_chat_page_hint": hd.NO_CHAT_PAGE_HINT,
        })

    def test_chat_page_hooked(self):
        self.assertFalse(hd.chat_page_hooked([{"page_kind": "chat_recent", "imsdk_hooked": False}]))
        self.assertTrue(hd.chat_page_hooked([{"page_kind": "chat_other", "imsdk_hooked": True}]))
        self.assertFalse(hd.chat_page_hooked([{"page_kind": "other", "imsdk_hooked": True}]))


class UnavailableReasonTests(unittest.TestCase):
    def test_no_pages(self):
        self.assertEqual(hd.unavailable_reason([]), "no_page_hooked")

    def test_chat_missing(self):
        # 未挂钩且没有聊天页 → chat_page_missing
        rows = [{"page_kind": "other", "imsdk_hooked": False}]
        self.assertEqual(hd.unavailable_reason(rows), "chat_page_missing")

    def test_hooked_non_chat_page_counts_as_capable(self):
        # 新版客户端聊天页在 dx-h5 的 iframe 里：只要挂上 imsdk 就能取订单
        rows = [{"page_kind": "other", "imsdk_hooked": True}]
        self.assertEqual(hd.unavailable_reason(rows), "")

    def test_chat_present_not_hooked(self):
        rows = [{"page_kind": "chat_recent", "imsdk_hooked": False}]
        self.assertEqual(hd.unavailable_reason(rows), "no_page_hooked")

    def test_hooked_chat_but_invoke_failed(self):
        rows = [{"page_kind": "chat_recent", "imsdk_hooked": True}]
        self.assertEqual(
            hd.unavailable_reason(rows, "Qianniu imsdk is unavailable"),
            "hooked_but_invoke_failed",
        )

    def test_needs_human_wins(self):
        self.assertEqual(hd.unavailable_reason([], "", needs_human=True), "needs_human_action")

    def test_ok_when_nothing_wrong(self):
        rows = [{"page_kind": "chat_recent", "imsdk_hooked": True}]
        self.assertEqual(hd.unavailable_reason(rows, ""), "")


class ReasonsCounterTests(unittest.TestCase):
    def test_counts_failures_only(self):
        rows = [
            {"imsdk_hooked": False, "last_hook_error": "imsdk_undefined"},
            {"imsdk_hooked": False, "last_hook_error": "imsdk_undefined"},
            {"imsdk_hooked": False, "last_hook_error": "exception: TypeError: x"},
            {"imsdk_hooked": True, "last_hook_error": ""},
        ]
        self.assertEqual(
            hd.reasons_counter(rows),
            {"imsdk_undefined": 2, "exception": 1},
        )


class MonitorTests(unittest.TestCase):
    class Logger:
        def __init__(self):
            self.lines = []

        def info(self, message):
            self.lines.append(message)

    class Browser:
        def __init__(self, rows):
            self.rows = rows

        def diagnostics_by_connection(self):
            return self.rows

    def test_logs_summary_and_transition(self):
        logger = self.Logger()
        browser = self.Browser([{"page_kind": "other", "imsdk_hooked": False,
                                 "last_hook_error": "imsdk_undefined"}])
        app = type("App", (), {"browser": browser})()
        monitor = hd.HookMonitor(app, logger=logger, interval=3600)
        monitor.tick()
        self.assertIn("imsdk hook: pages=1 hooked=0", logger.lines[0])
        self.assertIn("reasons={'imsdk_undefined': 1}", logger.lines[0])
        # 跃变：0 -> 1
        browser.rows = [{"page_kind": "chat_recent", "imsdk_hooked": True}]
        monitor.tick()
        self.assertIn("imsdk hook changed:", logger.lines[1])
        self.assertEqual(monitor.transitions, 1)


if __name__ == "__main__":
    unittest.main()
