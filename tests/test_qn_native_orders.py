"""qn_native_orders 单测：解析千牛 app.log 里的原生 reception.detail.get。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import qn_native_orders as n

# 现场真实行（DESKTOP-8RPPIBT，2026-10-28 23:39:23）
LIVE_LINE = (
    '[10-28 23:39:23 568286031 25904 1044 INFO] UnifiedMtop [MTOP 联想官方旗舰店:小山#3#2212223914397 ]'
    '[response. nTaskID: #  action : # resultCode: # data: #][ data={"api":'
    '"mtop.taobao.qianniu.airisland.reception.detail.get","data":{"data":['
    '{"bizDomain":"taobao","buyerId":"2252311170","buyerNick":"cntaobaohshking1",'
    '"snsNick":"hshking1","tradeSimpleList":[{"bizOrderId":5127834996048025044,'
    '"logisticsStatus":8,"modifiedTime":1793201415000,"payStatus":4}]}]}}]'
)

QNMODEL_LINE = (
    '[10-28 23:39:23 568286031 25904 1044 WARNING] qnmodel [QNSDK 3#2212223914397 ]'
    '[OnInvokeAsync $][ api=mtop.taobao.qianniu.airisland.reception.detail.get ]'
    '[MTopChannel.cpp(94) OnInvokeAsync]'
)


class ParseTests(unittest.TestCase):
    def test_parses_live_line(self):
        rows = n.parse_line(LIVE_LINE)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["buyer_id"], "2252311170")
        self.assertEqual(row["buyer_nick"], "hshking1")
        self.assertEqual(len(row["orders"]), 1)
        order = row["orders"][0]
        self.assertEqual(order["order_id"], "5127834996048025044")
        self.assertEqual(order["pay_status"], 4)
        self.assertEqual(order["logistics_status"], 8)
        self.assertEqual(order["source"], "qianniu_native_reception")
        self.assertAlmostEqual(order["modified_at"], 1793201415.0, places=1)

    def test_ignores_lines_without_payload(self):
        self.assertEqual(n.parse_line(QNMODEL_LINE), [])
        self.assertEqual(n.parse_line("hello world"), [])
        self.assertEqual(n.parse_line(""), [])

    def test_tolerates_truncated_json(self):
        broken = LIVE_LINE[: LIVE_LINE.index('"snsking1"') if '"snsking1"' in LIVE_LINE else 200]
        self.assertEqual(n.parse_line(broken), [])

    def test_multiple_buyers_in_one_response(self):
        line = (
            'data={"api":"mtop.taobao.qianniu.airisland.reception.detail.get","data":{"data":['
            '{"buyerId":"1111","snsNick":"a","tradeSimpleList":[{"bizOrderId":11,"payStatus":4}]},'
            '{"buyerId":"2222","snsNick":"b"},'
            '{"buyerId":"3333","snsNick":"c","tradeSimpleList":[{"bizOrderId":33}]}]}}]'
        )
        rows = n.parse_line(line)
        self.assertEqual([r["buyer_id"] for r in rows], ["1111", "2222", "3333"])
        self.assertEqual(len(rows[0]["orders"]), 1)
        self.assertEqual(rows[1]["orders"], [])


class CacheTests(unittest.TestCase):
    def _app(self, extra_config=None):
        class Config(dict):
            def get(self, key, default=None):
                return dict.get(self, key, default)

        class App:
            config = Config(extra_config or {})

        return App()

    def test_tail_reads_only_new_lines_and_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "app.log"
            log_path.write_text("old line before startup\n", encoding="utf-8")

            app = self._app({"native_order_log_paths": [str(log_path)]})
            store = n.NativeOrderLog(app, poll_seconds=0.05)
            store.path = store.locate()
            self.assertEqual(store.path, log_path)
            store.offset = log_path.stat().st_size

            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(LIVE_LINE + "\n")
            store._consume()

            orders = store.orders_for("2252311170")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["order_id"], "5127834996048025044")
            self.assertEqual(store.status()["buyers"], 1)
            self.assertEqual(store.status()["orders"], 1)

            # 同一订单再来一次（状态更新）不应产生重复
            updated = LIVE_LINE.replace('"payStatus":4', '"payStatus":6')
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(updated + "\n")
            store._consume()
            orders = store.orders_for("2252311170")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["pay_status"], 6)

    def test_startup_does_not_replay_history(self):
        app = self._app({})
        store = n.NativeOrderLog(app, poll_seconds=0.02)
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "app.log"
            log_path.write_text(LIVE_LINE + "\n", encoding="utf-8")
            store.stop_event.set()  # 只跑一轮 attach 逻辑，不进入循环
            store.path = log_path
            store.offset = log_path.stat().st_size
            store._consume()
            self.assertEqual(store.orders_for("2252311170"), [])

    def test_unknown_buyer_returns_empty(self):
        app = self._app({})
        store = n.NativeOrderLog(app, poll_seconds=0.05)
        self.assertEqual(store.orders_for("999"), [])
        self.assertEqual(store.orders_for(""), [])

    def test_status_reports_disabled_without_log(self):
        app = self._app({})
        store = n.NativeOrderLog(app, poll_seconds=0.05)
        status = store.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["orders"], 0)


class EndpointPayloadTests(unittest.TestCase):
    def _host(self):
        import standalone_bridge as sb

        class App:
            class config(dict):
                pass

            config = config()

        class Server:
            pass

        app = App()
        app.native_orders = n.NativeOrderLog(app, poll_seconds=0.05)
        app.native_orders._remember("2252311170", "hshking1", [
            n.normalize_order({"bizOrderId": 5127834996048025044, "payStatus": 4, "modifiedTime": 1793201415000}),
        ])
        server = Server()
        server.app = app

        host = object.__new__(sb.WorkbenchHandler)
        host.server = server
        return host

    def test_payload_uses_native_log_when_page_fails(self):
        host = self._host()
        payload = host._native_orders_payload(
            {"buyer_encrypt_id": ["2252311170"]}, error="Qianniu imsdk is unavailable"
        )
        self.assertIsNotNone(payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "native_log")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["orders"][0]["order_id"], "5127834996048025044")
        self.assertEqual(payload["page_error"], "Qianniu imsdk is unavailable")

    def test_payload_none_when_buyer_unknown(self):
        host = self._host()
        self.assertIsNone(host._native_orders_payload({"buyer_encrypt_id": ["123"]}))


class BackfillTests(unittest.TestCase):
    """装了就要能查到：启动时回读日志尾部，且丢弃半行。"""

    def test_backfill_reads_existing_lines_and_skips_partial_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "app.log"
            # 前半行（模拟从中段开始读到的残行）+ 完整行
            log_path.write_text("MTOP junk half line " + LIVE_LINE + "\n" + LIVE_LINE + "\n",
                                encoding="utf-8")

            class App:
                class config(dict):
                    pass

                config = config()

            store = n.NativeOrderLog(App(), poll_seconds=0.05, backfill_bytes=8 * 1024 * 1024)
            store.path = log_path
            store.offset = max(0, log_path.stat().st_size - store.backfill_bytes)
            store.backfilled = int(store.offset > 0)
            store._consume()

            orders = store.orders_for("2252311170")
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["order_id"], "5127834996048025044")
            self.assertGreater(store.status()["lines_read"], 0)


class NoFullRescanTests(unittest.TestCase):
    """append 造成 offset 略大于 size 时，不能误判截断并重扫全文。"""

    def test_offset_ahead_of_size_does_not_trigger_full_rescan(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "app.log"
            log_path.write_text(LIVE_LINE + "\n", encoding="utf-8")
            size = log_path.stat().st_size

            class App:
                class config(dict):
                    pass

                config = config()

            store = n.NativeOrderLog(App(), poll_seconds=0.05)
            store.path = log_path
            store.last_size = size
            store.offset = size + 500  # 模拟 stat/read 时间差
            store._consume()
            self.assertEqual(store.lines_read, 0)
            self.assertEqual(store.orders_for("2252311170"), [])

            # 真正截断（size 变小）才重扫
            log_path.write_text(LIVE_LINE + "\n", encoding="utf-8")
            store.last_size = size + 10_000
            store.offset = size + 10_000
            store._consume()
            self.assertEqual(len(store.orders_for("2252311170")), 1)
