"""坐席上送延迟 / 面板误报的回归测试（2026-10-17）。

现场：联想官方旗舰店:瑞秋 那台机（客户端 1.6.14）面板显示「连接异常 / 桥接服务
已停止 未监听」，同时大脑侧看到它的事件上送 p50 11.2s（同店其它坐席 0.6~1.9s），
客户端自己报 `delivery.last_error="timed out"` 占 73% 的采样。

三条结构性原因，本文件各钉一条：

1. 本地自检 POST 的 1.5s 超时 + 每事件一次 `connect()`/`event_status()`：7 个注入页
   并发写 SQLite 时必然超时，随后标记失败 + 退避重试，把本地开销放大成 10~30s。
2. 每次连接都跑 `PRAGMA journal_mode=WAL`（库级持久设置，只需要设一次），在 Windows
   杀软扫 SQLite 文件时会拿文件锁，单次开库就要几十~几百毫秒。
3. `workbench_sessions()` 的整表扫描（最多 20000 条 payload 逐条 JSON 解析）在全局锁里
   跑：它运行期间 /api/v1/status（托盘 5s 探测）、本地自检、事件入库全部排队，
   于是托盘把一台还在收发消息的机器报成「已停止」。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试绝不访问线上大脑。
os.environ.setdefault("QN_BRAIN_SERVER_URL", "http://127.0.0.1:1")

import standalone_bridge as bridge  # noqa: E402


class _Config(dict):
    def get(self, key: str, default=None):  # noqa: ANN001
        return dict.get(self, key, default)


def _event(index: int, buyer: str = "", **extra) -> dict:
    event = {
        "platform": "taobao",
        "role": "user",
        "content": f"stall probe {index}",
        "account": "联想官方旗舰店:瑞秋",
        "buyer_id": buyer or f"22092621491{index:02d}.1-126446588.1#11001@cntaobao",
        "msg_id": f"43558433045{index:02d}",
        "ts": 1792218400 + index,
    }
    event.update(extra)
    return event


class StateDBFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bridge.StateDB(Path(self.tmp.name) / "stall.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def seed(self, count: int = 3) -> list[str]:
        ids = []
        for index in range(count):
            event_id, _changed = self.db.upsert_event(_event(index))
            ids.append(event_id)
        return ids


class BatchedDurabilityAckTests(StateDBFixture):
    """本地自检从「每事件一次查询」改成「一次批量查询」。"""

    def test_batch_status_matches_per_event_status(self):
        ids = self.seed(4)
        batch = self.db.event_statuses(ids)
        assert set(batch) == set(ids)
        for event_id in ids:
            assert batch[event_id] == self.db.event_status(event_id)

    def test_batch_status_dedupes_and_tolerates_empty_input(self):
        ids = self.seed(2)
        assert self.db.event_statuses([]) == {}
        assert self.db.event_statuses(["", ""]) == {}
        duplicated = [ids[0], ids[0], ids[1], ""]
        assert set(self.db.event_statuses(duplicated)) == set(ids)

    def test_ack_payload_only_commits_claimed_rows(self):
        ids = self.seed(2)
        events = [_event(0), _event(1)]
        # 刚入库是 pending：与旧实现一致，不能 ACK 成 durable。
        with self.assertRaises(RuntimeError):
            bridge.local_seat_ack_payload(self.db, events)
        self.db.claim_pending(10)  # pending -> sending（上传队列已认领）
        payload = bridge.local_seat_ack_payload(self.db, events)
        assert payload["ok"] is True
        assert [row["event_id"] for row in payload["event_acks"]] == ids
        assert all(row["committed"] is True for row in payload["event_acks"])

    def test_ack_payload_rejects_batch_that_is_not_committed(self):
        events = [_event(7)]
        with self.assertRaises(RuntimeError):
            bridge.local_seat_ack_payload(self.db, events)

    def test_ack_payload_ignores_non_dict_entries_and_caps_batch(self):
        ids = self.seed(2)
        self.db.claim_pending(10)
        events = [_event(0), "not-an-event", None, _event(1)]
        payload = bridge.local_seat_ack_payload(self.db, events)
        assert [row["event_id"] for row in payload["event_acks"]] == ids
        too_many = [_event(index) for index in range(bridge.LOCAL_SEAT_ACK_BATCH_LIMIT + 5)]
        fake = _FakeDB()
        capped = bridge.local_seat_ack_payload(fake, too_many)
        assert len(capped["event_acks"]) == bridge.LOCAL_SEAT_ACK_BATCH_LIMIT
        assert fake.calls == 1, "整批只允许一次状态查询"


class _FakeDB:
    """只回 committed 的假库，用来验证批量上限。"""

    def __init__(self) -> None:
        self.calls = 0

    def event_statuses(self, event_ids):  # noqa: ANN001
        self.calls += 1
        assert self.calls == 1, "ack 必须只查一次库"
        return {str(event_id): "sending" for event_id in event_ids}


class SqlitePragmaTests(StateDBFixture):
    """WAL 只在建库时设置一次，连接不再重复设置；busy_timeout 兜住锁竞争。"""

    def test_journal_mode_persists_without_per_connection_pragma(self):
        with closing(self.db.connect()) as connection:
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        assert mode == "wal"

    def test_busy_timeout_is_set_per_connection(self):
        with closing(self.db.connect()) as connection:
            timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        assert timeout == 8000


class EventsIndexTests(StateDBFixture):
    """会话快照的 `ORDER BY created_at DESC LIMIT ?` 必须有索引，否则每次重建全表排序。"""

    def test_created_at_index_exists(self):
        with closing(self.db.connect()) as connection:
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
                ).fetchall()
            }
        assert "events_created_at" in names

    def test_created_at_index_is_used_by_session_scan(self):
        self.seed(3)
        with closing(self.db.connect()) as connection:
            plan = " ".join(
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT payload,status FROM events "
                    "ORDER BY created_at DESC LIMIT 20000"
                ).fetchall()
            )
        assert "events_created_at" in plan, plan


class SessionSnapshotStallTests(StateDBFixture):
    """会话快照重建不能占着全局锁：否则状态探测/入库全部排队。"""

    def _note(self, count: int = 3) -> None:
        buyer = "2209262149113.1-126446588.1#11001@cntaobao"
        for index in range(count):
            self.db.upsert_event(_event(index, buyer=buyer))

    def test_snapshot_build_runs_outside_the_global_lock(self):
        self._note(3)
        started = threading.Event()
        release = threading.Event()
        original = self.db._build_workbench_sessions

        def slow_build():
            started.set()
            release.wait(10)
            return original()

        self.db._build_workbench_sessions = slow_build
        result: list = []
        worker = threading.Thread(target=lambda: result.append(self.db.workbench_sessions()))
        worker.start()
        assert started.wait(5), "快照重建没有开始"
        try:
            # 重建被卡住期间，其它需要全局锁的操作必须还能跑。
            began = time.monotonic()
            self.db.counts()
            elapsed = time.monotonic() - began
            assert elapsed < 1.0, f"快照重建占着全局锁 {elapsed:.2f}s"
            self.db.event_statuses([])
        finally:
            release.set()
            worker.join(10)
        assert result and len(result[0]) >= 1

    def test_snapshot_is_cached_between_calls(self):
        self._note(3)
        first = self.db.workbench_sessions()
        second = self.db.workbench_sessions()
        assert first == second
        assert self.db._session_cache_at > 0.0

    def test_snapshot_content_matches_events_after_refactor(self):
        self._note(3)
        sessions = self.db.workbench_sessions()
        assert len(sessions) == 1
        row = sessions[0]
        assert row["account"] == "联想官方旗舰店:瑞秋"
        assert row["message_count"] == 3
        assert row["last_message"].startswith("stall probe")
        assert row["ai_mode"] == "ai"


class DeliveryTimeoutBudgetTests(unittest.TestCase):
    """本地自检超时预算：1.5s 会常态超时，默认必须是 8s。"""

    def _post_with_captured_timeout(self, cfg: dict) -> float:
        captured: dict = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true, "event_acks": [{"event_id": "x", "committed": true}]}'

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            captured["timeout"] = timeout
            return _Response()

        app = SimpleNamespace(config=_Config(cfg, gateway_url="http://127.0.0.1:18776"))
        worker = bridge.DeliveryWorker(app)
        original = bridge.urllib.request.urlopen
        bridge.urllib.request.urlopen = fake_urlopen
        try:
            worker.post([{"event_id": "x"}])
        finally:
            bridge.urllib.request.urlopen = original
        return captured["timeout"]

    def test_default_local_delivery_timeout_is_generous(self):
        timeout = self._post_with_captured_timeout({})
        assert timeout == 8.0, f"默认自检超时是 {timeout}s，低于本地繁忙时的合理预算"

    def test_explicit_config_still_wins(self):
        assert self._post_with_captured_timeout({"delivery_timeout_seconds": 3}) == 3.0
