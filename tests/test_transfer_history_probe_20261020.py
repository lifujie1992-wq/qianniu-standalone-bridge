# -*- coding: utf-8 -*-
"""转接历史 / 图片结构诊断开关（2026-10-20）。

默认关闭；只用于在真实 Windows 客户端上采集 GetRemoteHisMsg 参数与
AppBiz 584 字节消息结构，不改变正常收发。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TransferHistoryProbeTest(unittest.TestCase):
    def test_browser_history_probe_is_opt_in_and_same_day(self):
        source = (ROOT / "browser_bridge.js").read_text(encoding="utf-8")
        self.assertIn("var HISTORY_PROBE = OPTIONS.history_probe === true;", source)
        self.assertIn("sameDayStartSeconds", source)
        self.assertIn("im.singlemsg.GetRemoteHisMsg", source)
        self.assertIn("im.singlemsg.GetLocalHisMsg", source)
        self.assertIn("history_probe_results", source)

    def test_appbiz_image_struct_dump_is_opt_in(self):
        source = (ROOT / "appbiz_agent.js").read_text(encoding="utf-8")
        self.assertIn("__APPBIZ_DUMP_IMAGE_STRUCT__", source)
        self.assertIn("imageStructDumpSent", source)
        self.assertIn("appbiz_message_dump", source)
        self.assertIn("readHex(message, MESSAGE_SIZE)", source)

    def test_loader_injects_dump_flag(self):
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn('"__APPBIZ_DUMP_IMAGE_STRUCT__"', source)
        self.assertIn("appbiz_dump_image_struct", source)
        self.assertIn("appbiz_message_dump", source)
        self.assertIn("history_probe_results", source)

    def test_config_defaults_stay_off(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertIs(config["bridge_history_probe"], False)
        self.assertIs(config["appbiz_dump_image_struct"], False)
        self.assertEqual(config["bridge_history_probe_params"], {})
        launcher = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('"history_probe"', launcher)
        injector = (ROOT / "tools" / "inject_runtime_webui.py").read_text(encoding="utf-8")
        self.assertIn('"history_probe"', injector)
        self.assertIn("bridge_history_probe_params", injector)


if __name__ == "__main__":
    unittest.main()
