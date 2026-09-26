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
        self.assertIn("CONFIG_DEFAULTS_REVISION = 5", source)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
