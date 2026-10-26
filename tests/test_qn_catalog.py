"""qn_catalog 单测：mtop 表达式、响应归一化、分页与错误路径。"""

from __future__ import annotations

import json
import unittest

import qn_catalog as qc


class FakeBrowser:
    """按方法名返回预置响应，并记录页面表达式。"""

    METHODS = (
        qc.ITEM_ONSALE_METHOD,
        qc.ITEM_DETAIL_METHOD,
        qc.ITEM_DESC_METHOD,
        qc.ITEM_RECORD_METHOD,
        qc.TRADE_QUERY_METHOD,
        qc.TRADE_HISTORY_METHOD,
        qc.ORDER_MEMO_METHOD,
        qc.SHOP_INFO_METHOD,
    )

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.expressions: list[str] = []

    def execute(self, expression: str, timeout: float = 3.0):
        self.expressions.append(expression)
        method = ""
        for candidate in self.METHODS:
            if candidate in expression:
                method = candidate
                break
        payload = self.responses.get(method, {"ok": False, "error": "no canned response"})
        if callable(payload):
            return payload(expression)
        return payload


class FakeApp:
    def __init__(self, responses: dict[str, object], config: dict | None = None):
        self.browser = FakeBrowser(responses)
        self.config = config or {}


def mtop_success(data: object) -> dict:
    return {"ok": True, "value": {"ret": ["SUCCESS::调用成功"], "data": data}}


class NormalizeTests(unittest.TestCase):
    def test_normalize_goods_reads_fuke_shape(self):
        value = mtop_success({
            "data": {
                "total": 2,
                "itemList": [
                    {"itemId": "7001", "title": "棉袜", "picUrl": "https://img/1.jpg",
                     "price": "9.90", "quantity": 120, "soldQuantity": 8},
                    {"itemId": "7002", "title": "拖鞋", "zkFinalPrice": "19.9"},
                    {"itemId": "7001", "title": "棉袜-重复"},
                ],
            }
        })
        parsed = qc.normalize_goods(value)
        self.assertEqual(len(parsed["goods"]), 2)
        self.assertEqual(parsed["total"], 2)
        first = parsed["goods"][0]
        self.assertEqual(first["goods_id"], "7001")
        self.assertEqual(first["goods_name"], "棉袜")
        self.assertEqual(first["goods_url"], "https://item.taobao.com/item.htm?id=7001")
        self.assertEqual(first["goods_price"], "9.90")
        self.assertEqual(first["stock"], 120)

    def test_normalize_orders_reads_fuke_shape(self):
        value = mtop_success({
            "data": {
                "orders": [{
                    "bizOrderId": "3001",
                    "orderPrice": "59.00",
                    "payStatus": "已付款",
                    "logisticsStatus": "待发货",
                    "sellerMemo": "尽快发",
                    "itemList": [
                        {"auctionId": "7001", "auctionTitle": "棉袜", "price": "9.90", "subOrderId": "3001-1"},
                    ],
                }]
            }
        })
        orders = qc.normalize_orders(value)
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["order_id"], "3001")
        self.assertEqual(order["order_price"], "59.00")
        self.assertEqual(order["seller_memo"], "尽快发")
        self.assertEqual(order["item_count"], 1)
        self.assertEqual(order["items"][0]["goods_id"], "7001")

    def test_mtop_ok_and_ret(self):
        self.assertTrue(qc.mtop_ok({"ret": ["SUCCESS::ok"]}))
        self.assertFalse(qc.mtop_ok({"ret": ["FAIL_SYS_TOKEN_EMPTY::token"]}))
        self.assertTrue(qc.mtop_ok({"data": {}}))
        self.assertEqual(qc.mtop_ret({"ret": "SUCCESS::x"}), ["SUCCESS::x"])

    def test_collect_lists_walks_nested_wrappers(self):
        payload = {"data": {"result": json.dumps({"orderList": [{"tid": "1"}]})}}
        buckets = qc.collect_lists(payload, {"orderList"})
        self.assertEqual(buckets, [[{"tid": "1"}]])


class CatalogTests(unittest.TestCase):
    def test_goods_page_uses_invoke_mtopchannel_service(self):
        app = FakeApp({qc.ITEM_ONSALE_METHOD: mtop_success({
            "data": {"total": 3, "itemList": [{"itemId": "1", "title": "A"}]}
        })})
        catalog = qc.QnCatalog(app)
        page = catalog.goods_page(page_no=1, page_size=2)
        self.assertEqual(page["count"] if "count" in page else len(page["goods"]), 1)
        self.assertTrue(page["has_more"])
        expression = app.browser.expressions[-1]
        self.assertIn("application.invokeMTopChannelService", expression)
        self.assertIn("mtop.taobao.qianniu.cs.item.onsale.query", expression)
        self.assertIn('"pageNo": 1', expression.replace('"pageNo":1', '"pageNo": 1'))

    def test_goods_all_paginates_and_dedupes(self):
        pages = {
            1: {"data": {"total": 3, "itemList": [{"itemId": "1", "title": "A"}, {"itemId": "2", "title": "B"}]}},
            2: {"data": {"total": 3, "itemList": [{"itemId": "2", "title": "B"}, {"itemId": "3", "title": "C"}]}},
        }

        def respond(expression: str):
            page_no = 1 if '"pageNo":1' in expression.replace(" ", "") else 2
            return mtop_success(pages.get(page_no, {"data": {"total": 3, "itemList": []}}))

        app = FakeApp({qc.ITEM_ONSALE_METHOD: respond})
        catalog = qc.QnCatalog(app)
        result = catalog.goods_all(max_pages=5, page_size=2)
        self.assertEqual(result["count"], 3)
        self.assertEqual([row["goods_id"] for row in result["goods"]], ["1", "2", "3"])
        self.assertTrue(result["complete"])

    def test_buyer_orders_normalizes_and_passes_uid(self):
        app = FakeApp({qc.TRADE_QUERY_METHOD: mtop_success({
            "data": {"orders": [{"bizOrderId": "8888", "orderPrice": "1.00"}]}
        })})
        catalog = qc.QnCatalog(app)
        result = catalog.buyer_orders("2217298756354")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["orders"][0]["order_id"], "8888")
        expression = app.browser.expressions[-1]
        self.assertIn("securityBuyerUid", expression)
        self.assertIn("2217298756354", expression)

    def test_non_success_ret_raises(self):
        app = FakeApp({qc.TRADE_QUERY_METHOD: {"ok": True, "value": {"ret": ["FAIL_SYS_TOKEN_EMPTY::x"]}}})
        catalog = qc.QnCatalog(app)
        with self.assertRaises(qc.QnCatalogError):
            catalog.buyer_orders("2217298756354")

    def test_page_error_raises(self):
        app = FakeApp({qc.ITEM_ONSALE_METHOD: {"ok": False, "error": "Qianniu browser bridge is not connected"}})
        catalog = qc.QnCatalog(app)
        with self.assertRaises(qc.QnCatalogError):
            catalog.goods_page()

    def test_order_memo_falls_back_to_second_param_shape(self):
        def respond(expression: str):
            if '"operation": "query"' not in expression and '"operation":"query"' not in expression:
                return {"ok": True, "value": {"ret": ["FAIL_SYS_BIZPARAM_MISSED::缺少业务参数operation"]}}
            return mtop_success({"data": {"editRecordList": [{"memoContent": "加急", "flagTag": "红"}],
                                          "flagTagList": [{"flagId": "1", "latestTag": "AG自动退"}]}})

        app = FakeApp({qc.ORDER_MEMO_METHOD: respond})
        catalog = qc.QnCatalog(app)
        result = catalog.order_memo("3001")
        self.assertEqual(result["params_used"], {"operation": "query", "orderId": "3001"})
        self.assertEqual(result["memos"][0]["memo"], "加急")
        self.assertEqual(result["flag_tags"][0]["latest_tag"], "AG自动退")

    def test_goods_desc_uses_version_4_1(self):
        app = FakeApp({qc.ITEM_DESC_METHOD: mtop_success({"data": {"images": ["https://img/1.jpg"]}})})
        catalog = qc.QnCatalog(app)
        desc = catalog.goods_desc("7001")
        self.assertEqual(desc["images"], ["https://img/1.jpg"])
        expression = app.browser.expressions[-1]
        self.assertIn('"version": "4.1"', expression)
        self.assertIn("item_num_id", expression)

    def test_goods_detail_passes_encrypt_id(self):
        app = FakeApp({qc.ITEM_DETAIL_METHOD: mtop_success({
            "data": {"item": {"itemId": "7001", "title": "棉袜", "pic": "https://img/1.jpg"},
                     "propsName": "颜色:白色"}
        })})
        catalog = qc.QnCatalog(app)
        detail = catalog.goods_detail("7001", "2210703046212")
        self.assertEqual(detail["detail"]["goods_name"], "棉袜")
        expression = app.browser.expressions[-1]
        self.assertIn("encryptId", expression)
        self.assertIn("isNewCustomer", expression)

    def test_shop_info_tmall_flag(self):
        app = FakeApp({qc.SHOP_INFO_METHOD: mtop_success({
            "result": {"tmallSeller": True, "shopName": "示例旗舰店"}
        })})
        catalog = qc.QnCatalog(app)
        info = catalog.shop_info()
        self.assertTrue(info["is_tmall"])
        self.assertEqual(info["shop_name"], "示例旗舰店")


if __name__ == "__main__":
    unittest.main()


class CatalogStoreTests(unittest.TestCase):
    """商品目录落库 + 同步器（用真实 StateDB + 假页面）。"""

    def test_upsert_and_search_roundtrip(self):
        import tempfile, pathlib
        from standalone_bridge import StateDB
        with tempfile.TemporaryDirectory() as directory:
            db = StateDB(pathlib.Path(directory) / "state.sqlite3")
            written = db.catalog_upsert_goods([
                {"goods_id": "7001", "goods_name": "联想无线鼠标", "goods_price": "62.00",
                 "stock": 1165, "sold_count": 905385, "goods_url": "https://item/7001"},
                {"goods_id": "7002", "goods_name": "联想移动硬盘", "goods_price": "659.00", "stock": 1640},
                {"goods_id": "", "goods_name": "无ID应被丢弃"},
            ])
            self.assertEqual(written, 2)
            hits = db.catalog_search("硬盘")
            self.assertEqual([row["goods_id"] for row in hits], ["7002"])
            self.assertEqual(len(db.catalog_search("")), 2)
            db.catalog_set_meta("last_sync", {"written": 2})
            meta = db.catalog_meta()
            self.assertEqual(meta["stored_goods"], 2)
            self.assertEqual(meta["last_sync"]["written"], 2)

    def test_syncer_pages_until_total(self):
        import tempfile, pathlib
        from standalone_bridge import StateDB
        pages = {
            1: {"goods": [{"goods_id": "1", "goods_name": "A"}, {"goods_id": "2", "goods_name": "B"}], "total": 3},
            2: {"goods": [{"goods_id": "3", "goods_name": "C"}], "total": 3},
        }

        class FakeCatalog:
            def goods_page(self, page_no, page_size):
                return pages.get(page_no, {"goods": [], "total": 3})

        class FakeConfig:
            def get(self, key, default=None):
                return {
                    "catalog_sync_enabled": True,
                    "catalog_sync_interval_seconds": 1800,
                    "catalog_sync_page_size": 2,
                    "catalog_sync_max_pages": 5,
                }.get(key, default)

        class FakeBrowser:
            connected = 1

        with tempfile.TemporaryDirectory() as directory:
            app = type("App", (), {})()
            app.catalog = FakeCatalog()
            app.config = FakeConfig()
            app.browser = FakeBrowser()
            app.db = StateDB(pathlib.Path(directory) / "state.sqlite3")
            app.stop_event = None
            syncer = qc.CatalogSyncer(app)
            summary = syncer.sync_once()
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["written"], 3)
            self.assertEqual(summary["total"], 3)
            self.assertEqual(app.db.catalog_meta()["stored_goods"], 3)
            self.assertEqual(syncer.status()["last_total"], 3)

    def test_syncer_skips_when_browser_offline(self):
        class FakeBrowser:
            connected = 0

        app = type("App", (), {})()
        app.catalog = object()
        app.config = type("C", (), {"get": lambda self, key, default=None: default})()
        app.browser = FakeBrowser()
        app.db = object()
        app.stop_event = None
        syncer = qc.CatalogSyncer(app)
        summary = syncer.sync_once()
        self.assertFalse(summary["ok"])
        self.assertIn("not connected", summary["error"])
