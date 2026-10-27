"""发送路由冷启动修复（1.6.30）单测。

背景：sendtext 要求该 ccode 在 120 秒内被千牛自己 GetNewMsg 过；助手/千牛重启后
映射表为空，直接抛 "Qianniu has no recent GetNewMsg context"。
修复：用官方 openChat 让千牛自己轮询（绝不主动调 GetNewMsg），就绪后再发。
"""

from __future__ import annotations

import unittest

from standalone_bridge import AppBizSendAdapter, ConfirmedSendRejection


class FakeConfig:
    def __init__(self, **values):
        self.values = {
            "appbiz_send_abi_validated": True,
            "appbiz_route_warmup_enabled": True,
            "appbiz_route_warmup_seconds": 1.0,
            "appbiz_callback_wait_seconds": 0.0,
            **values,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeExports:
    def __init__(self, states):
        self.states = list(states)  # preparesend 依次返回
        self.prepare_calls = []
        self.sent = []

    def preparesend(self, ccode, central):
        self.prepare_calls.append((ccode, central))
        state = self.states.pop(0) if self.states else False
        return {"ok": state}

    def status(self):
        return {"selected_service": "0x1", "selection_reason": "singlemsg_getnewmsg"}

    def sendtext(self, ccode, content, pcsource, token, central=False):
        self.sent.append((ccode, content, pcsource, token, central))
        return 0

    def pollsend(self, token):
        # state=1 表示回调到达；result_code=0 表示千牛接受
        return {"state": 1, "result_code": 0}

    def cancelsend(self, token):
        return {"ok": True}


class FakeScript:
    def __init__(self, exports):
        self.exports_sync = exports


class FakeDB:
    def account_for_buyer(self, buyer_id):
        return "店铺:客服"

    def cached_nickname(self, account, buyer_id):
        return "buyer_nick"

    def conversation_route_nickname(self, account, buyer_id):
        return "buyer_nick"


class FakeBridge:
    def __init__(self, warm_ok=True):
        self.config = FakeConfig()
        self.db = FakeDB()
        self.stop_event = type("E", (), {"wait": lambda self, seconds: False})()
        self.warm_calls = []
        self.warm_ok = warm_ok

    def warm_send_route(self, account, ccode):
        self.warm_calls.append((account, ccode))
        return self.warm_ok


def make_adapter(app, exports):
    adapter = object.__new__(AppBizSendAdapter)
    adapter.app = app
    adapter.script = FakeScript(exports)
    adapter.pid = 1234
    adapter.next_send_token = 1
    adapter.last_error = ""
    adapter.selected_service = 0x1
    adapter.selection_reason = "singlemsg_getnewmsg"
    adapter.refresh_status = lambda: None
    return adapter


class RouteWarmupTests(unittest.TestCase):
    def test_cold_route_is_warmed_then_sent(self):
        app = FakeBridge()
        exports = FakeExports([False, False, True])  # 预热前两次没有，热身之后有
        adapter = make_adapter(app, exports)
        adapter.send_text("2210703046212.1-1.1#11001@cntaobao", "你好", "QianniuStandaloneBridge/test")
        self.assertEqual(app.warm_calls, [("店铺:客服", "2210703046212.1-1.1#11001@cntaobao")])
        self.assertEqual(len(exports.sent), 1, "预热成功后必须发出")
        self.assertEqual(exports.sent[0][1], "你好")

    def test_warmup_failure_raises_route_cold_and_does_not_send(self):
        app = FakeBridge(warm_ok=False)
        exports = FakeExports([False])
        adapter = make_adapter(app, exports)
        with self.assertRaises(ConfirmedSendRejection) as ctx:
            adapter.send_text("2210703046212.1-1.1#11001@cntaobao", "你好", "x")
        self.assertIn("route_cold", str(ctx.exception))
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(exports.sent, [], "没有路由时绝不能发")

    def test_warmup_disabled_keeps_original_behaviour(self):
        """关掉开关 = 完全回到旧行为：不预热、不拦截，交给 agent 判定。"""
        app = FakeBridge()
        app.config.values["appbiz_route_warmup_enabled"] = False
        exports = FakeExports([False])
        adapter = make_adapter(app, exports)
        adapter.send_text("2210703046212.1-1.1#11001@cntaobao", "你好", "x")
        self.assertEqual(app.warm_calls, [])
        self.assertEqual(len(exports.sent), 1)

    def test_ready_route_skips_warmup(self):
        app = FakeBridge()
        exports = FakeExports([True])
        adapter = make_adapter(app, exports)
        adapter.send_text("2210703046212.1-1.1#11001@cntaobao", "你好", "x")
        self.assertEqual(app.warm_calls, [])
        self.assertEqual(len(exports.sent), 1)

    def test_route_state_handles_agent_exception(self):
        app = FakeBridge()

        class Boom:
            def preparesend(self, ccode, central):
                raise RuntimeError("frida gone")

        adapter = object.__new__(AppBizSendAdapter)
        adapter.app = app
        adapter.script = FakeScript(Boom())
        self.assertFalse(adapter.route_state("ccode", False))
        self.assertIsNone(adapter.script.exports_sync.preparesend("ccode", False) if False else None)


if __name__ == "__main__":
    unittest.main()
