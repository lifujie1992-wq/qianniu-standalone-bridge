"""Latency P0 regressions: ingress gate and outbound send pool.

These are the two knobs that mirror the latency patches validated on the PDD
bridge (immediate ingress + concurrent command sender):

- ingress: ``brain_event_delay_seconds`` gates how long a captured message waits
  in the local brain queue before the first upload. It used to be 2.5s; the
  event thread is woken on enqueue, so the gate was the only real delay.
- egress: ``command_sender_pool_enabled`` decouples fetching commands from
  sending them, so one command's receipt wait (2s typical, 15s worst case) no
  longer holds the fetch loop.
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests must never reach the shipped brain deployment.
os.environ.setdefault("QN_BRAIN_SERVER_URL", "http://127.0.0.1:1")

import standalone_bridge as bridge  # noqa: E402


class _Config(dict):
    def get(self, key: str, default=None):  # noqa: ANN001
        return dict.get(self, key, default)


def _app(**config) -> SimpleNamespace:
    return SimpleNamespace(config=_Config(config), stop_event=threading.Event())


class IngressGateTests(unittest.TestCase):
    """How long a captured event waits before the first upload attempt."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bridge.StateDB(Path(self.tmp.name) / "latency.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> str:
        event_id, _changed = self.db.upsert_event({
            "platform": "taobao",
            "role": "user",
            "content": "latency probe",
            "account": "seller",
            "buyer_id": "buyer#1@cntaobao",
            "buyer_nick": "buyer",
            "msg_id": "m-latency-1",
            "ts": time.time(),
            "source": "test",
        })
        self.assertTrue(self.db.enqueue_brain_event(event_id))
        return event_id

    def test_gate_holds_the_event_then_releases_it(self) -> None:
        event_id = self._seed()
        self.assertEqual(
            self.db.claim_brain_events(2.5, 10), [],
            "a 2.5s gate must not release a freshly queued event",
        )
        claimed = self.db.claim_brain_events(0.0, 10)
        self.assertEqual([row["event_id"] for row in claimed], [event_id])

    def test_claimed_event_carries_its_payload(self) -> None:
        event_id = self._seed()
        claimed = self.db.claim_brain_events(0.0, 10)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["event_id"], event_id)
        self.assertEqual(claimed[0]["payload"].get("content"), "latency probe")

    def test_enqueue_wakes_the_event_thread(self) -> None:
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        # The wake already existed; if it disappears the gate becomes a poll
        # interval again and the saving is lost.
        self.assertIn("brain.wakeup.set()", source)

    def test_default_gate_is_subsecond(self) -> None:
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn('brain_event_delay_seconds", 0.2', source)
        self.assertNotIn('brain_event_delay_seconds", 2.5', source)


class SenderPoolTests(unittest.TestCase):
    """Egress: fetching commands must not wait for the previous receipt."""

    def _connector(self, **config):
        connector = bridge.BrainConnector(_app(**config))
        self.calls: list[dict] = []
        self.calls_lock = threading.Lock()

        def fake_handle(command):
            with self.calls_lock:
                self.calls.append(command)

        connector.handle_command = fake_handle  # type: ignore[assignment]
        return connector

    def test_pool_disabled_keeps_the_old_inline_path(self) -> None:
        connector = self._connector(command_sender_pool_enabled=False)
        connector.start_sender_pool()
        self.assertFalse(connector._sender_threads, "no worker may start when disabled")
        self.assertFalse(connector.sender_pool_ready())
        connector.dispatch_command({"id": "c1"})
        self.assertEqual([c["id"] for c in self.calls], ["c1"])

    def test_pool_enabled_dispatches_without_blocking(self) -> None:
        connector = self._connector(command_sender_pool_enabled=True, command_sender_workers=2)
        connector.start_sender_pool()
        try:
            self.assertTrue(connector.sender_pool_ready())
            self.assertEqual(len(connector._sender_threads), 2)
            started = time.monotonic()
            connector.dispatch_command({"id": "c1"})
            self.assertLess(time.monotonic() - started, 0.05, "dispatch must not run the send inline")
            for _ in range(100):
                if self.calls:
                    break
                time.sleep(0.01)
            self.assertEqual([c["id"] for c in self.calls], ["c1"])
        finally:
            connector.app.stop_event.set()

    def test_same_conversation_is_serialized(self) -> None:
        connector = self._connector(command_sender_pool_enabled=True, command_sender_workers=4)
        first = connector.sender_conversation_lock({"account": "a", "buyer_id": "b"})
        second = connector.sender_conversation_lock({"account": "a", "buyer_id": "b"})
        other = connector.sender_conversation_lock({"account": "a", "buyer_id": "c"})
        self.assertIs(first, second, "same buyer must share one lock")
        self.assertIsNot(first, other, "different buyers must not share a lock")

    def test_full_pool_falls_back_to_inline_send(self) -> None:
        connector = self._connector(command_sender_pool_enabled=True, command_sender_workers=1)
        connector.start_sender_pool()
        try:
            connector._sender_queue = queue.Queue(maxsize=1)
            connector._sender_queue.put_nowait({"id": "held"})
            connector.dispatch_command({"id": "overflow"})
            # The overflow command ran inline rather than being dropped.
            self.assertEqual([c["id"] for c in self.calls], ["overflow"])
        finally:
            connector.app.stop_event.set()

    def test_worker_survives_a_failing_command(self) -> None:
        connector = self._connector(command_sender_pool_enabled=True, command_sender_workers=1)
        seen: list[str] = []
        seen_lock = threading.Lock()

        def flaky(command):
            with seen_lock:
                seen.append(str(command.get("id")))
            if command.get("id") == "boom":
                raise RuntimeError("send failed")

        connector.handle_command = flaky  # type: ignore[assignment]
        connector.start_sender_pool()
        try:
            connector.dispatch_command({"id": "boom"})
            connector.dispatch_command({"id": "after"})
            for _ in range(200):
                if len(seen) >= 2:
                    break
                time.sleep(0.01)
            self.assertEqual(seen, ["boom", "after"], "a failing command must not kill the worker")
        finally:
            connector.app.stop_event.set()

    def test_defaults_enable_the_pool(self) -> None:
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn('"command_sender_pool_enabled", True', source)
        self.assertIn('"command_sender_workers", 6', source)


class EventUploadPoolTests(unittest.TestCase):
    """Ingress fan-out: batches upload concurrently instead of one round-trip
    at a time, and the command loop polls before retrying results."""

    def _connector(self, **config):
        return bridge.BrainConnector(_app(**config))

    def test_default_concurrency_is_four(self) -> None:
        self.assertEqual(self._connector().event_upload_worker_count(), 4)

    def test_concurrency_is_clamped(self) -> None:
        self.assertEqual(
            self._connector(event_upload_concurrency=99).event_upload_worker_count(), 8
        )
        # 0 是假值，按默认处理（与 command_sender_workers 的 `or N` 一致）；
        # 要串行请显式写 1。
        self.assertEqual(
            self._connector(event_upload_concurrency=0).event_upload_worker_count(), 4
        )
        self.assertEqual(
            self._connector(event_upload_concurrency=1).event_upload_worker_count(), 1
        )
        self.assertEqual(
            self._connector(event_upload_concurrency="bad").event_upload_worker_count(), 4
        )

    def test_batch_size_is_clamped_to_claim_limit(self) -> None:
        self.assertEqual(self._connector().event_upload_batch_size(), 100)
        self.assertEqual(
            self._connector(event_upload_batch_size=500).event_upload_batch_size(), 100
        )
        self.assertEqual(
            self._connector(event_upload_batch_size=7).event_upload_batch_size(), 7
        )

    def test_concurrent_claims_never_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = bridge.StateDB(Path(tmp) / "latency.sqlite3")
            seeded: list[str] = []
            for index in range(60):
                event_id, _changed = db.upsert_event({
                    "platform": "taobao",
                    "role": "user",
                    "content": f"burst {index}",
                    "account": "seller",
                    "buyer_id": "buyer#1@cntaobao",
                    "buyer_nick": "buyer",
                    "msg_id": f"m-burst-{index}",
                    "ts": time.time(),
                    "source": "test",
                })
                self.assertTrue(db.enqueue_brain_event(event_id))
                seeded.append(event_id)

            claimed: list[str] = []
            guard = threading.Lock()

            def worker() -> None:
                while True:
                    rows = db.claim_brain_events(0.0, 10)
                    if not rows:
                        return
                    with guard:
                        claimed.extend(str(row["event_id"]) for row in rows)
                    db.finish_brain_events(
                        rows, {str(row["event_id"]) for row in rows}
                    )

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(claimed), len(set(claimed)), "no event claimed twice")
            self.assertEqual(sorted(claimed), sorted(seeded), "every event claimed once")
            self.assertEqual(db.brain_event_counts()["pending"], 0)

    def test_event_wakeup_is_dedicated(self) -> None:
        connector = self._connector()
        self.assertIsNot(connector.event_wakeup, connector.wakeup)

    def test_source_starts_pool_and_polls_commands_first(self) -> None:
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn("event_upload_concurrency", source)
        self.assertIn("event_upload_loop", source)
        commands = source[source.index("def run_commands"):]
        commands = commands[: commands.index("def run_events")]
        self.assertLess(
            commands.index("self.pull_commands()"),
            commands.index("self.retry_command_results()"),
            "command polling must run before result retries",
        )


class CaptureCadenceTests(unittest.TestCase):
    """Ingress cadence: passive scans are configurable and a capture miss
    schedules a debounced recovery scan instead of waiting for the next tick."""

    def test_bridge_intervals_are_configurable(self) -> None:
        source = (ROOT / "browser_bridge.js").read_text(encoding="utf-8")
        self.assertIn("dom_scan_interval_ms", source)
        self.assertIn("cache_scan_interval_ms", source)
        self.assertIn("function clampIntervalMs(", source)
        self.assertIn("function scheduleRecoveryScan(", source)
        self.assertIn('scheduleRecoveryScan("local_retry_exhausted")', source)
        self.assertNotIn("var PASSIVE_DOM_INTERVAL_MS = 10000;", source)
        self.assertNotIn("var PASSIVE_CACHE_INTERVAL_MS = 30000;", source)

    def test_launcher_injects_cadence_options(self) -> None:
        source = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn("def int_config_value(", source)
        self.assertIn('"dom_scan_interval_ms": int_config_value(', source)
        self.assertIn('"cache_scan_interval_ms": int_config_value(', source)
        self.assertIn("bridge_passive_dom_ms", source)
        self.assertIn("bridge_passive_cache_ms", source)

    def test_injector_tool_keeps_the_same_options(self) -> None:
        source = (ROOT / "tools" / "inject_runtime_webui.py").read_text(encoding="utf-8")
        self.assertIn('"dom_scan_interval_ms": _int_option(', source)
        self.assertIn('"cache_scan_interval_ms": _int_option(', source)

    def test_config_revision_backfills_cadence(self) -> None:
        source = (ROOT / "config_defaults.py").read_text(encoding="utf-8")
        from config_defaults import apply_operational_defaults
        config = {"config_defaults_revision": 4}
        apply_operational_defaults(config)
        self.assertEqual(config["bridge_passive_dom_ms"], 5000)
        self.assertEqual(config["bridge_passive_cache_ms"], 10000)
        self.assertIn('setdefault("bridge_passive_dom_ms", 5000)', source)
        self.assertIn('setdefault("bridge_passive_cache_ms", 10000)', source)

    def test_diagnostics_forward_recovery_counters(self) -> None:
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        for key in (
            "passive_dom_interval_ms",
            "passive_cache_interval_ms",
            "recovery_scan_requests",
            "recovery_scan_runs",
            "recovery_scan_last_reason",
        ):
            self.assertIn(f'"{key}"', source, f"{key} must be in the diagnostics whitelist")

    def test_heartbeat_snapshot_carries_recovery_counters(self) -> None:
        # The heartbeat rebuilds a curated snapshot every few seconds and overwrites
        # the full hi diagnostics, so the new counters must be listed there too.
        source = (ROOT / "browser_bridge.js").read_text(encoding="utf-8")
        for key in (
            "passive_dom_interval_ms",
            "passive_cache_interval_ms",
            "recovery_scan_requests",
            "recovery_scan_runs",
            "recovery_scan_last_reason",
        ):
            self.assertIn(f"{key}: diagnostics.{key},", source, f"{key} missing from heartbeat snapshot")


class RealtimePriorityClaimTests(unittest.TestCase):
    """Real-time captures must not queue behind replayed history.

    Production sees clients with hundreds of pending ``brain_events`` (transfer
    history shards, WS reconnect replays). A plain ``ORDER BY queued_at`` FIFO
    puts a brand new buyer message behind that whole backlog, which is what
    showed up as an 8~29s client-side upload delay.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bridge.StateDB(Path(self.tmp.name) / "priority.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self, name: str, age_seconds: float) -> str:
        event_id, _changed = self.db.upsert_event({
            "platform": "taobao",
            "role": "user",
            "content": f"probe {name}",
            "account": "seller",
            "buyer_id": "buyer#1@cntaobao",
            "buyer_nick": "buyer",
            "msg_id": f"m-{name}",
            "ts": time.time(),
            "source": "test",
        })
        self.assertTrue(self.db.enqueue_brain_event(event_id))
        if age_seconds > 0:
            stamp = time.time() - age_seconds
            with self.db.lock:
                connection = self.db.connect()
                try:
                    connection.execute(
                        "UPDATE events SET created_at=? WHERE event_id=?",
                        (stamp, event_id),
                    )
                    connection.execute(
                        "UPDATE brain_events SET queued_at=? WHERE event_id=?",
                        (stamp, event_id),
                    )
                    connection.commit()
                finally:
                    connection.close()
        return event_id

    def test_fresh_capture_is_claimed_before_the_backlog(self) -> None:
        backlog = [self._seed(f"old-{index}", 600) for index in range(10)]
        fresh = self._seed("fresh", 0)
        rows = self.db.claim_brain_events(0.0, 20)
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[0]["event_id"], fresh, "the live message must go first")
        self.assertEqual({row["event_id"] for row in rows[1:]}, set(backlog))

    def test_backlog_keeps_a_reserved_share_under_live_flood(self) -> None:
        backlog = {self._seed(f"old-{index}", 600) for index in range(200)}
        live = {self._seed(f"live-{index}", 0) for index in range(100)}
        rows = self.db.claim_brain_events(0.0, 100)
        self.assertEqual(len(rows), 100, "a full batch must still be claimed")
        claimed = {str(row["event_id"]) for row in rows}
        self.assertEqual(len(claimed & live), 75, "live messages keep the live quota")
        self.assertEqual(
            len(claimed & backlog), 25, "backlog keeps its reserved share (limit//4)"
        )

    def test_backlog_is_not_starved_and_never_claimed_twice(self) -> None:
        backlog = [self._seed(f"old-{index}", 600) for index in range(30)]
        for index in range(100):
            self._seed(f"live-{index}", 0)
        claimed: list[str] = []
        for _ in range(10):
            rows = self.db.claim_brain_events(0.0, 20)
            if not rows:
                break
            ids = [str(row["event_id"]) for row in rows]
            self.assertEqual(len(ids), len(set(ids)), "no duplicate claim in a batch")
            claimed.extend(ids)
            self.db.finish_brain_events(rows, set(ids))
        self.assertEqual(len(claimed), len(set(claimed)), "no event claimed twice")
        self.assertTrue(
            set(backlog) <= set(claimed),
            "historical backlog must still drain while live traffic flows",
        )

    def test_window_zero_keeps_the_legacy_fifo(self) -> None:
        first = self._seed("first", 600)
        second = self._seed("second", 300)
        rows = self.db.claim_brain_events(0.0, 10, 0.0)
        self.assertEqual([row["event_id"] for row in rows], [first, second])

    def test_upload_loop_passes_the_live_window(self) -> None:
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn("event_live_window_seconds", source)
        self.assertIn("self.event_live_window_seconds(),", source)


class WsAckFailoverTests(unittest.TestCase):
    """A stalled WS link must not cost a full ack timeout on every batch."""

    def _connector(self, **config):
        return bridge.BrainConnector(_app(**config))

    def test_default_ack_timeout_is_short(self) -> None:
        self.assertEqual(self._connector().event_ws_ack_timeout(), 2.5)

    def test_ack_timeout_is_clamped(self) -> None:
        self.assertEqual(
            self._connector(brain_ws_ack_timeout_seconds=0).event_ws_ack_timeout(), 0.3
        )
        self.assertEqual(
            self._connector(brain_ws_ack_timeout_seconds=120).event_ws_ack_timeout(), 30.0
        )
        self.assertEqual(
            self._connector(brain_ws_ack_timeout_seconds="bad").event_ws_ack_timeout(),
            2.5,
        )

    def test_consecutive_misses_open_the_breaker(self) -> None:
        connector = self._connector()
        self.assertFalse(connector.event_ws_failover_active())
        connector.note_event_ws_ack(0, 5)
        self.assertFalse(connector.event_ws_failover_active(), "one miss is not enough")
        connector.note_event_ws_ack(0, 5)
        self.assertTrue(connector.event_ws_failover_active(), "two misses short-circuit WS")

    def test_a_single_ack_closes_the_breaker(self) -> None:
        connector = self._connector()
        connector.note_event_ws_ack(0, 5)
        connector.note_event_ws_ack(3, 5)
        self.assertEqual(connector._ws_ack_misses, 0)
        self.assertFalse(connector.event_ws_failover_active())

    def test_breaker_expires(self) -> None:
        connector = self._connector()
        connector.note_event_ws_ack(0, 1)
        connector.note_event_ws_ack(0, 1)
        self.assertTrue(connector.event_ws_failover_active())
        connector._ws_failover_until = time.time() - 1.0
        self.assertFalse(connector.event_ws_failover_active())

    def test_transmit_skips_ws_while_the_breaker_is_open(self) -> None:
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn("event_ws_failover_active()", source)
        self.assertIn("self.note_event_ws_ack(", source)
        self.assertNotIn("brain_ws_ack_timeout_seconds',10.0", source)

    def test_example_config_ships_the_new_defaults(self) -> None:
        import json as _json

        example = _json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["brain_ws_ack_timeout_seconds"], 2.5)
        self.assertEqual(example["brain_ws_failover_misses"], 2)
        self.assertEqual(example["brain_ws_failover_seconds"], 30.0)
        self.assertEqual(example["brain_event_live_window_seconds"], 30.0)

    def test_send_receipt_polling_keeps_the_tight_backoff(self) -> None:
        source = (ROOT / "standalone_bridge.py").read_text(encoding="utf-8")
        self.assertIn("time.sleep(0.025 if elapsed < 0.5 else 0.04)", source)
        self.assertNotIn("time.sleep(0.025 if elapsed < 0.5 else 0.1)", source)

    def _channel_stub(self, *, acked: int = 0, raises=None):
        class _Channel:
            available = True

            def __init__(self) -> None:
                self.calls: list[tuple[int, float]] = []

            def send_events(self, batch, *, timeout=0.0):
                self.calls.append((len(batch), timeout))
                if raises is not None:
                    raise raises
                return {
                    "ack_version": 1,
                    "event_acks": [
                        {
                            "event_id": str(item["event_id"]),
                            "status": "accepted",
                            "committed": True,
                        }
                        for item in batch[:acked]
                    ],
                }

        return _Channel()

    def _connector_with(self, channel):
        connector = self._connector()
        connector.event_channel = lambda: channel  # type: ignore[assignment]
        self.http_calls: list[dict] = []

        def fake_request(method, path, body=None, **_kwargs):
            self.http_calls.append({"method": method, "path": path, "body": body})
            return {"event_acks": [], "accepted": 0}

        connector.request = fake_request  # type: ignore[assignment]
        return connector

    def test_acked_ws_batch_stays_on_ws(self) -> None:
        channel = self._channel_stub(acked=2)
        connector = self._connector_with(channel)
        payload, transport = connector.post_events([{"event_id": "e1"}, {"event_id": "e2"}])
        self.assertEqual(transport, "ws")
        self.assertEqual(len(payload["event_acks"]), 2)
        self.assertEqual(self.http_calls, [], "an acked WS batch must not also POST")
        self.assertFalse(connector.event_ws_failover_active())

    def test_unacked_ws_batch_falls_back_to_http_immediately(self) -> None:
        channel = self._channel_stub(acked=0)
        connector = self._connector_with(channel)
        payload, transport = connector.post_events([{"event_id": "e1"}])
        self.assertEqual(transport, "http")
        self.assertEqual(len(self.http_calls), 1, "zero-ack WS must not wait for the next round")
        self.assertEqual(channel.calls[0][1], 2.5, "the WS wait must use the short timeout")
        self.assertEqual(payload.get("event_acks"), [])

    def test_ws_error_falls_back_to_http(self) -> None:
        channel = self._channel_stub(raises=bridge.BrainWsError("link down"))
        connector = self._connector_with(channel)
        _payload, transport = connector.post_events([{"event_id": "e1"}])
        self.assertEqual(transport, "http")
        self.assertEqual(len(self.http_calls), 1)

    def test_open_breaker_skips_ws_entirely(self) -> None:
        channel = self._channel_stub(acked=1)
        connector = self._connector_with(channel)
        connector.note_event_ws_ack(0, 1)
        connector.note_event_ws_ack(0, 1)
        self.assertTrue(connector.event_ws_failover_active())
        _payload, transport = connector.post_events([{"event_id": "e1"}])
        self.assertEqual(transport, "http")
        self.assertEqual(channel.calls, [], "no WS attempt while the breaker is open")

    def test_migration_shortens_the_legacy_ten_second_ack_timeout(self) -> None:
        from config_defaults import apply_operational_defaults

        config = {"config_defaults_revision": 9, "brain_ws_ack_timeout_seconds": 10.0}
        apply_operational_defaults(config)
        self.assertEqual(config["brain_ws_ack_timeout_seconds"], 2.5)
        self.assertEqual(config["brain_event_live_window_seconds"], 30.0)

        tuned = {"config_defaults_revision": 9, "brain_ws_ack_timeout_seconds": 6.0}
        apply_operational_defaults(tuned)
        self.assertEqual(tuned["brain_ws_ack_timeout_seconds"], 6.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
