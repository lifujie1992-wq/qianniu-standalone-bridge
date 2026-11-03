"""127 工作台点开会话超时 / 暂无消息（2026-11-02）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QN_BRAIN_SERVER_URL", "http://127.0.0.1:1")

import types

try:
    # 真实 websockets 存在时绝不能用 stub 覆盖：模块级替换会在 discover
    # 先收集后运行的机制下污染后续所有测试（BrainWsChannelTests 曾因此
    # 全量跑报 serve 返回 None，单跑又正常 —— 2026-11-03）。
    import websockets.sync.server  # noqa: F401
except ImportError:
    _ws = sys.modules.setdefault("websockets", types.ModuleType("websockets"))
    _ws_sync = sys.modules.setdefault("websockets.sync", types.ModuleType("websockets.sync"))
    _ws_server = sys.modules.setdefault("websockets.sync.server", types.ModuleType("websockets.sync.server"))
    _ws_server.serve = lambda *args, **kwargs: None  # type: ignore[method-assign]
    sys.modules["websockets"].sync = _ws_sync  # type: ignore[attr-defined]
    _ws_sync.server = _ws_server  # type: ignore[attr-defined]

import standalone_bridge as bridge  # noqa: E402


def _event(index: int, buyer: str, content: str = "") -> dict:
    return {
        "platform": "taobao",
        "role": "user",
        "content": content or f"msg {index} for {buyer}",
        "account": "联想官方旗舰店:瑞秋",
        "buyer_id": f"{buyer}.1-126446588.1#11001@cntaobao",
        "buyer_nick": buyer,
        "msg_id": f"qn-msg-v1|taobao|{4376684990000 + index}.PNM",
        "ts": 1792218400 + index,
    }


class WorkbenchClickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bridge.StateDB(Path(self.tmp.name) / "click.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_old_limit_would_drop_target_but_new_query_keeps_it(self) -> None:
        target = "tb218579209"
        self.db.upsert_event(_event(0, target, "亲，您好，接下来由我为您服务"))
        original = bridge.WORKBENCH_MESSAGE_LIMIT
        bridge.WORKBENCH_MESSAGE_LIMIT = 5
        try:
            for index in range(1, 12):
                self.db.upsert_event(_event(index, f"other{index:02d}"))
            messages = self.db.workbench_messages(
                "联想官方旗舰店:瑞秋",
                f"{target}.1-126446588.1#11001@cntaobao",
            )
        finally:
            bridge.WORKBENCH_MESSAGE_LIMIT = original
        assert messages
        assert any("接下来由我为您服务" in item["content"] for item in messages)

    def test_messages_do_not_leak_other_buyers(self) -> None:
        self.db.upsert_event(_event(0, "alice", "alice says hi"))
        self.db.upsert_event(_event(1, "bob", "bob says hi"))
        messages = self.db.workbench_messages(
            "联想官方旗舰店:瑞秋",
            "alice.1-126446588.1#11001@cntaobao",
        )
        assert [item["content"] for item in messages] == ["alice says hi"]
