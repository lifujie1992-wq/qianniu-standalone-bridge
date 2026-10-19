# -*- coding: utf-8 -*-
"""原生 [图片] 事件补真实 URL（2026-10-20）。

千牛 AppBiz 回调只给 content="[图片]"，大脑视觉链路只认图片 URL。
浏览器页内 bridge 暴露 __qn_standalone_image_url 从本地 msgDataMap 取 URL，
Python 侧在 ingest 前补进事件。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from native_image_lookup import enrich_native_image_event, is_placeholder_image_event

ROOT = Path(__file__).resolve().parents[1]
URL = "https://img.alicdn.com/imgextra/i3/126446588/chat-image.jpg"


class _Browser:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = []

    def execute(self, expression, timeout=0):
        self.calls.append((expression, timeout))
        if self.error:
            raise self.error
        return self.value


def _event() -> dict:
    return {
        "content": "[图片]",
        "raw_type": "text",
        "buyer_id": "2209017372373.1-126446588.1#11001@cntaobao",
        "original_msg_id": "4359395424756.PNM",
    }


class NativeImageLookupTest(unittest.TestCase):
    def test_placeholder_is_enriched_with_the_cached_url(self):
        browser = _Browser(URL)
        enriched = enrich_native_image_event(_event(), browser)
        self.assertEqual(enriched["content"], URL)
        self.assertEqual(enriched["image_url"], URL)
        self.assertEqual(enriched["media_url"], URL)
        self.assertEqual(enriched["raw_type"], "image")
        self.assertEqual(enriched["original_content"], "[图片]")
        self.assertIn("__qn_standalone_image_url", browser.calls[0][0])
        self.assertIn("4359395424756.PNM", browser.calls[0][0])

    def test_disabled_or_missing_url_keeps_the_event(self):
        original = _event()
        self.assertIs(enrich_native_image_event(original, _Browser(URL), enabled=False), original)
        self.assertIs(enrich_native_image_event(original, _Browser("")), original)
        self.assertIs(enrich_native_image_event(original, _Browser(None)), original)
        self.assertIs(enrich_native_image_event(original, _Browser(error=RuntimeError("no page"))), original)

    def test_non_image_event_is_not_touched(self):
        event = {"content": "你好", "raw_type": "text", "buyer_id": "b", "msg_id": "m"}
        self.assertIs(enrich_native_image_event(event, _Browser(URL)), event)
        self.assertFalse(is_placeholder_image_event(event))

    def test_wiring_is_present(self):
        source = (ROOT / "browser_bridge.js").read_text(encoding="utf-8")
        self.assertIn("window.__qn_standalone_image_url = imageUrlForMessage;", source)
        bridge = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn("enrich_native_image_event", bridge)
        self.assertIn('"bridge_native_image_lookup": true', (ROOT / "config.example.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
