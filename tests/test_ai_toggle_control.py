"""浮窗「开启 / 暂停 AI 回复」（客户端侧）。

对齐拼多多：浮窗上点一下就能暂停/恢复**当前会话**的 AI 回复，不用切到千牛窗口，
也不影响其它会话。

两条链路必须同时成立：

1. 大脑权威：`POST /api/session/state`（本机工位令牌）返回确认后的
   `ai_takeover_enabled`；本机只把确认结果落到 `session_controls`（source
   `brain` / `brain_pause`），不自己改发送规则。
2. 浮窗 UI：dock 模式下每个会话行都有 AI 开关，点击不会顺带打开千牛会话。

旧实现（1.6.14）发的是 `session_control` 事件，而大脑侧没有这个 event 类型，
所以工作台上的「AI / 人工」按钮一直是"点了没反应"。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QN_BRAIN_SERVER_URL", "http://127.0.0.1:1")

import standalone_bridge as bridge  # noqa: E402

ACCOUNT = "联想官方旗舰店:瑞秋"
BUYER = "2209262149113.1-126446588.1#11001@cntaobao"


class _Config(dict):
    def get(self, key, default=None):  # noqa: ANN001
        return dict.get(self, key, default)


def _app(db) -> SimpleNamespace:
    return SimpleNamespace(db=db, config=_Config({"brain_server_url": "http://127.0.0.1:1"}))


class _Recorder:
    """替换 BrainConnector.request，记录 (method, path, body)。"""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, path, body=None, timeout=None):  # noqa: ANN001
        self.calls.append((method, path, dict(body or {})))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class AiTakeoverControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bridge.StateDB(Path(self.tmp.name) / "ai-toggle.sqlite3")
        self.app = _app(self.db)
        self.connector = bridge.BrainConnector(self.app)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _install(self, response):
        recorder = _Recorder(response)
        self.connector.request = recorder  # type: ignore[assignment]
        return recorder

    def test_pause_goes_through_the_brain_and_is_not_local_only(self):
        recorder = self._install({
            "ok": True,
            "ai_takeover_enabled": False,
            "ai_takeover_state": "paused",
            "shop_ai_takeover_enabled": True,
            "sticky_handoff": False,
        })
        control = self.connector.set_ai_takeover(ACCOUNT, BUYER, False)
        method, path, body = recorder.calls[0]
        assert (method, path) == ("POST", "/api/session/state")
        assert body["account"] == ACCOUNT
        assert body["buyer_id"] == BUYER
        assert body["ai_takeover_enabled"] is False
        # 本地只反映大脑确认后的状态
        assert control["ai_mode"] == "human"
        assert control["handoff_source"] == "brain_pause"
        assert control["ai_takeover_enabled"] is False
        assert self.db.session_control(ACCOUNT, BUYER)["ai_mode"] == "human"

    def test_resume_clears_the_pause(self):
        self._install({"ok": True, "ai_takeover_enabled": False})
        self.connector.set_ai_takeover(ACCOUNT, BUYER, False)
        self._install({"ok": True, "ai_takeover_enabled": True, "ai_takeover_state": "active"})
        control = self.connector.set_ai_takeover(ACCOUNT, BUYER, True)
        assert control["ai_mode"] == "ai"
        assert control["handoff_source"] == "brain"
        assert self.db.session_control(ACCOUNT, BUYER)["ai_mode"] == "ai"

    def test_brain_rejection_raises_and_leaves_local_state_untouched(self):
        self._install({"ok": False, "error": "该店铺不在本机桥接授权范围内"})
        with self.assertRaises(RuntimeError):
            self.connector.set_ai_takeover(ACCOUNT, BUYER, False)
        assert self.db.session_control(ACCOUNT, BUYER)["ai_mode"] == "ai"

    def test_brain_effective_state_wins_over_the_request(self):
        # 大脑因 sticky handoff 之类的原因没放行恢复：本地必须跟大脑，而不是乐观置为 ai。
        self._install({"ok": True, "ai_takeover_enabled": False, "sticky_handoff": True})
        control = self.connector.set_ai_takeover(ACCOUNT, BUYER, True)
        assert control["ai_mode"] == "human"
        assert control["sticky_handoff"] is True

    def test_workbench_mode_compat_delegates_to_ai_takeover(self):
        recorder = self._install({"ok": True, "ai_takeover_enabled": False})
        control = self.connector.set_tmall_session_mode(ACCOUNT, BUYER, "human", "人工接管")
        assert recorder.calls[0][1] == "/api/session/state"
        assert recorder.calls[0][2]["ai_takeover_enabled"] is False
        assert control["ai_mode"] == "human"


class ControlEndpointSourceTests(unittest.TestCase):
    """工作台/浮窗开关必须以大脑为准，不能静默只写本地。"""

    def test_control_endpoint_uses_brain_whenever_configured(self):
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        start = source.index('if path == "/api/v1/session/control":')
        block = source[start:start + 1200]
        assert "self.app.brain.configured()" in block
        # 旧实现按 tmall_guard_scope 分流，非联想店静默写本地
        assert "if tmall_guard_scope(account)\n" not in block


class DockToggleMarkupTests(unittest.TestCase):
    """浮窗（dock 模式）必须有一键 AI 开关，且不能顺带打开千牛会话。"""

    def setUp(self) -> None:
        self.html = (ROOT / "workbench.html").read_text(encoding="utf-8")

    def test_dock_rows_render_an_ai_toggle(self):
        assert "function aiToggleChip(session)" in self.html
        assert "if (dockMode) side.append(aiToggleChip(session), time, glyph);" in self.html

    def test_toggle_does_not_open_the_conversation(self):
        # 行本身是 <button>：芯片必须是 span，且点击/回车都要 stopPropagation。
        assert "const activate = event => {" in self.html
        assert self.html.count("event.stopPropagation();") >= 1
        assert "chip.setAttribute('role', 'button');" in self.html
        assert "chip.addEventListener('keydown'" in self.html

    def test_toggle_calls_the_session_control_api(self):
        assert "async function toggleSessionAI(session)" in self.html
        assert "api('/api/v1/session/control'" in self.html
        assert "mode: next" in self.html

    def test_toggle_shows_failure_instead_of_silently_reverting(self):
        assert "dockToast(error.message || '大脑未确认 AI 开关', true)" in self.html

    def test_dock_only_chip_keeps_full_workbench_unchanged(self):
        # 非 dock 模式仍用原来的 AI / 人工 按钮，不重复渲染芯片。
        assert "else side.append(time, glyph);" in self.html


if __name__ == "__main__":
    unittest.main()
