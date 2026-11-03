"""工作台订单兜底：mtop 被另一家店的页面答空时，用事件里已抓到的订单。

2026-11-04 现场：买家「邱misslucky」明明有 5 单，工作台却显示"暂无订单"。
原因是多页面客户端里 mtop 是广播、先答者胜，另一家店的聊天页先回了空；
而事件里的 ``recent_orders`` 是当时在正确页面上抓到的。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import standalone_bridge as bridge


def event(buyer_encrypt_id: str, orders=None, msg_id: str = "1") -> dict:
    return {
        "platform": "taobao",
        "role": "user",
        "content": "在吗",
        "account": "联想官方旗舰店:小山",
        "buyer_id": f"{buyer_encrypt_id}.1-126446588.1#11001@cntaobao",
        "buyer_encrypt_id": buyer_encrypt_id,
        "buyer_nick": "邱misslucky",
        "msg_id": f"qn-msg-v1|taobao|{msg_id}.PNM",
        "ts": 1792218400,
        "recent_orders": orders or [],
    }


class CachedOrderContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bridge.StateDB(Path(self.tmp.name) / "orders.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_returns_orders_captured_in_an_earlier_event(self):
        self.db.upsert_event(event("2717597411", [{"order_id": "3317078712249044785"}]))
        cached = self.db.cached_order_context("2717597411")
        self.assertEqual([row["order_id"] for row in cached], ["3317078712249044785"])

    def test_ignores_other_buyers_and_empty_events(self):
        self.db.upsert_event(event("1111111111", [{"order_id": "other"}]))
        self.db.upsert_event(event("2717597411", [], msg_id="2"))
        self.assertEqual(self.db.cached_order_context("2717597411"), [])
        self.assertEqual(self.db.cached_order_context(""), [])

    def test_latest_non_empty_snapshot_wins(self):
        self.db.upsert_event(event("2717597411", [{"order_id": "old"}], msg_id="1"))
        self.db.upsert_event(event("2717597411", [{"order_id": "new"}], msg_id="2"))
        cached = self.db.cached_order_context("2717597411")
        self.assertEqual([row["order_id"] for row in cached], ["new"])

    def test_ignores_rows_without_matching_encrypt_id(self):
        payload = event("2717597411", [{"order_id": "leak"}])
        # 买家 ID 出现在别的字段里、但 buyer_encrypt_id 不是它时不能算命中。
        payload["buyer_encrypt_id"] = "9999999999"
        payload["buyer_id"] = "2717597411.1-126446588.1#11001@cntaobao"
        self.db.upsert_event(payload)
        self.assertEqual(self.db.cached_order_context("2717597411"), [])


if __name__ == "__main__":
    unittest.main()
