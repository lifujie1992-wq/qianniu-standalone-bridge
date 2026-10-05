"""Persistent WebSocket event channel to the cloud brain (protocol_version 1).

The brain accepts the same inbound event envelope over two transports:

    HTTP  POST /api/bridge/v1/events   (batch, one round-trip per batch)
    WS    GET  /api/bridge/v1/ws       (persistent, one frame per event)

WS is the low-latency path: a captured message is written to the socket the
moment it is claimed, instead of waiting for the upload worker to build a batch
and for a fresh TCP/TLS handshake. The HTTP path stays as the fallback.

Wire contract (mirrors the server's `bridge_ws_v1` feature):

    client -> server   {"type":"event","event_id","sequence","account",
                        "buyer_id","role","content","msg_id","captured_at_ms",
                        "payload"}
                       {"type":"command_ack","command_id","agent_id",
                        "status":"received"}
                       {"type":"pong","ts":N}
    server -> client   {"type":"ready","protocol_version":1,"connection_id",
                        "max_inflight":N,"agent_id":...}
                       {"type":"ack","event_id","status":"accepted|duplicate|
                        rejected","error_code":...}
                       {"type":"result","event_id","status","error_code",
                        "server_processed_at_ms":N}   # async business status
                       {"type":"command"|"command_push","command":{...}}  # push
                       {"type":"ping","ts":N}

Auth is `Authorization: Bearer <token>` on the upgrade request (the HTTP path
uses the X-Agent-Token header instead). A token is bound to one device, so a
wrong/missing X-Device-Id is rejected with close code 4001 and retrying cannot
help.

Command *execution results* still travel over HTTP
(`POST /api/bridge/v1/commands/{id}/result`); only the push side moved to WS.
The HTTP long-poll keeps running as a fallback, and command handling is
idempotent on command id, so a command delivered by both channels still runs at
most once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from app_version import VERSION


log = logging.getLogger("qianniu.bridge")

PROTOCOL_VERSION = 1
DEFAULT_WS_PATH = "/api/bridge/v1/ws"
MAX_FRAME_BYTES = 4 * 1024 * 1024
PING_INTERVAL = 20.0
PING_TIMEOUT = 10.0
READY_TIMEOUT_SECONDS = 15.0
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0
_RESULT_BACKLOG_LIMIT = 10000

# Fields that live at the top level of a frame; everything else is folded into
# `payload`. Kept in sync with the server's bridge_ws_v1 frame parser.
_FRAME_FIELDS = frozenset((
    "event_id", "sequence", "account", "buyer_id", "role", "content", "msg_id",
    "captured_at_ms", "payload",
))
# The server derives agent_id from the bearer token, and the idempotency key is
# an HTTP-only header concern, so neither belongs in the frame payload.
_PAYLOAD_DROP = frozenset(("agent_id", "idempotency_key"))


class BrainWsError(RuntimeError):
    """WS transport could not be used; the caller should fall back to HTTP."""


class BrainWsUnavailable(BrainWsError):
    """No usable connection right now (not started / not ready / stopped)."""


class BrainWsAuthFailed(BrainWsError):
    """Server closed with 4001: token or device binding mismatch. Retrying is
    pointless until the token is reissued."""


class BrainWsProtocolMismatch(BrainWsError):
    """`ready.protocol_version` is not the version this client speaks."""


def ws_url_from_server_url(server_url: str, path: str = DEFAULT_WS_PATH) -> str:
    """Derive the WS URL from the configured HTTP brain base URL.

    The HTTP and WS endpoints share the same host and port (18765), so only the
    scheme has to change.
    """
    base = str(server_url or "").strip().rstrip("/")
    if not base:
        raise BrainWsUnavailable("brain_server_url is empty")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not base.startswith(("ws://", "wss://")):
        raise BrainWsUnavailable("brain_server_url is neither http(s) nor ws(s)")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _connect_header_kwarg() -> str:
    """`websockets` renamed extra_headers -> additional_headers in 14.0.

    The packaged runtime ships 13.x, but a source checkout may have anything,
    so detect the accepted keyword instead of pinning one.
    """
    try:
        import inspect

        import websockets

        parameters = inspect.signature(websockets.connect).parameters
        if "additional_headers" in parameters:
            return "additional_headers"
        if "extra_headers" in parameters:
            return "extra_headers"
    except Exception:  # noqa: BLE001 - detection failure falls back below
        pass
    return "additional_headers"


def build_event_frame(event: dict, sequence: int) -> dict:
    """Flatten an internal event dict into the wire frame.

    account/buyer_id must stay top level because the server uses them to route
    and persist; the remaining keys ride along inside `payload` unchanged.
    """
    event = event if isinstance(event, dict) else {}
    captured_ms = event.get("captured_at_ms")
    if not captured_ms:
        try:
            captured_ms = int(round(float(event.get("captured_at") or 0.0) * 1000))
        except (TypeError, ValueError):
            captured_ms = 0
    payload = {
        key: value for key, value in event.items()
        if key not in _FRAME_FIELDS and key not in _PAYLOAD_DROP
    }
    return {
        "type": "event",
        "event_id": str(event.get("event_id") or event.get("idempotency_key") or ""),
        "sequence": int(sequence),
        "account": str(event.get("account") or ""),
        "buyer_id": str(event.get("buyer_id") or ""),
        "role": str(event.get("role") or ""),
        "content": event.get("content"),
        "msg_id": str(event.get("original_msg_id") or event.get("msg_id") or ""),
        "captured_at_ms": int(captured_ms or 0),
        "payload": payload,
    }


def ack_row_for(event: dict, status: str, error_code: str) -> Optional[dict]:
    """Translate a WS ack frame into the same row shape the HTTP path returns.

    The upload worker decides "committed / retry" from these rows, so the
    semantics must match `_flush_events`:

      accepted / duplicate          -> committed, never retried
      rejected + PERSISTENCE_UNAVAILABLE with a missing account/buyer_id
                                    -> terminal (the frame is malformed and will
                                       never become persistable)
      rejected + PERSISTENCE_UNAVAILABLE otherwise
                                    -> retryable
      rejected otherwise            -> terminal client bug
    """
    event_id = str(event.get("event_id") or event.get("idempotency_key") or "")
    if status in ("accepted", "duplicate"):
        return {"event_id": event_id, "status": "accepted",
                "committed": True, "retryable": False}
    if error_code == "PERSISTENCE_UNAVAILABLE":
        missing_key = not str(event.get("account") or "").strip() \
            or not str(event.get("buyer_id") or "").strip()
        if missing_key:
            return {"event_id": event_id, "status": "rejected", "committed": False,
                    "retryable": False, "reason": error_code}
        return {"event_id": event_id, "status": "retry", "committed": False,
                "retryable": True, "reason": error_code}
    return {"event_id": event_id, "status": "rejected", "committed": False,
            "retryable": False, "reason": error_code or status or "rejected"}


class _Batch:
    """One send_events call: collects the acks for its own event_ids."""

    __slots__ = ("events", "rows", "done", "_lock")

    def __init__(self, events: list[dict]):
        self.events: dict[str, dict] = {
            str(e.get("event_id") or e.get("idempotency_key") or ""): e for e in events
        }
        self.rows: dict[str, dict] = {}
        self.done = threading.Event()
        self._lock = threading.Lock()

    def on_ack(self, event_id: str, row: dict) -> None:
        with self._lock:
            if event_id not in self.events or event_id in self.rows:
                return
            self.rows[event_id] = row
            complete = len(self.rows) >= len(self.events)
        if complete:
            self.done.set()


class BrainEventChannel:
    """A single background-thread WS connection shared by all upload workers.

    `send_events` is a synchronous, blocking call that returns the same
    `{"ack_version":1,"event_acks":[...]}` shape as the HTTP endpoint, so the
    caller can swap transports without touching its retry logic.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        token: str,
        agent_id: str,
        device_id: str = "",
        platform: str = "taobao",
        user_agent: str = "",
        on_command: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.ws_url = str(ws_url or "")
        self.token = str(token or "")
        self.agent_id = str(agent_id or "")
        self.device_id = str(device_id or "")
        self.platform = str(platform or "taobao")
        self.user_agent = str(user_agent or f"QianniuStandaloneBridge/{VERSION}")
        self._on_command = on_command

        self._state_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Any = None
        self._outbound: Optional[asyncio.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        self._stop = threading.Event()
        self._sequence = 0

        self._owner_lock = threading.Lock()
        self._owner: dict[str, _Batch] = {}

        self._results: list[dict] = []
        self._results_lock = threading.Lock()

        self.last_error = ""
        self.max_inflight = 0
        self.connected_at = 0.0
        self._attempt_established = False
        self.frames_sent = 0
        self.acks_received = 0
        self.results_received = 0
        self.commands_received = 0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            try:
                import websockets  # noqa: F401
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"websockets unavailable: {exc}"
                log.warning("brain WS channel disabled: %s", self.last_error)
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._thread_main, name="brain-ws", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        # Close from the loop so a pending `async for raw in ws` wakes up.
        with self._state_lock:
            loop, ws, thread = self._loop, self._ws, self._thread
        if loop is not None and ws is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._state_lock:
            self._thread = None
            self._loop = None
            self._ws = None
            self._outbound = None
            self._connected = False

    @property
    def available(self) -> bool:
        with self._state_lock:
            return bool(self._connected)

    def status(self) -> dict:
        with self._state_lock:
            connected = bool(self._connected)
            connected_at = self.connected_at
            outbound = self._outbound
        pending = outbound.qsize() if outbound is not None else 0
        with self._owner_lock:
            waiting = len(self._owner)
        return {
            "connected": connected,
            "url": self.ws_url,
            "protocol_version": PROTOCOL_VERSION,
            "max_inflight": self.max_inflight,
            "connected_seconds": round(time.time() - connected_at, 1) if connected else 0.0,
            "frames_sent": self.frames_sent,
            "acks_received": self.acks_received,
            "results_received": self.results_received,
            "commands_received": self.commands_received,
            "pending_frames": pending,
            "awaiting_batches": waiting,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------ synchronous API
    def send_events(self, batch: list[dict], *, timeout: float = 10.0) -> dict:
        """Send events over WS and wait for their acks.

        Returns the HTTP-compatible ack dict. Raises BrainWsUnavailable when the
        connection is down so the caller can retry over HTTP; events without an
        event_id are skipped rather than sent.
        """
        events = [e for e in (batch or []) if isinstance(e, dict)]
        events = [e for e in events
                  if str(e.get("event_id") or e.get("idempotency_key") or "")]
        if not events:
            return {"ack_version": 1, "event_acks": []}

        with self._state_lock:
            loop, outbound = self._loop, self._outbound
            if not self._connected or loop is None or outbound is None or loop.is_closed():
                raise BrainWsUnavailable(self.last_error or "brain WS not connected")

        waiter = _Batch(events)
        with self._owner_lock:
            for event_id in waiter.events:
                self._owner[event_id] = waiter

        sent = 0
        try:
            for event in events:
                with self._state_lock:
                    self._sequence += 1
                    sequence = self._sequence
                encoded = json.dumps(build_event_frame(event, sequence), ensure_ascii=False)
                if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
                    # The server drops the whole connection on an oversized
                    # frame; skip it here and let the HTTP path deal with it.
                    log.warning("brain WS frame over %d bytes, skipped event_id=%s",
                                MAX_FRAME_BYTES, event.get("event_id"))
                    continue
                asyncio.run_coroutine_threadsafe(outbound.put(encoded), loop)
                sent += 1
            if not sent:
                raise BrainWsUnavailable("all frames were too large to send")
            self.frames_sent += sent
        except BrainWsUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BrainWsUnavailable(f"failed to enqueue WS frames: {exc}") from exc
        finally:
            if sent == 0:
                with self._owner_lock:
                    for event_id in waiter.events:
                        self._owner.pop(event_id, None)

        waiter.done.wait(timeout=max(0.1, float(timeout)))
        with self._owner_lock:
            for event_id in waiter.events:
                self._owner.pop(event_id, None)
        # Only the acks that actually arrived are reported; the caller keeps the
        # rest pending and re-sends them on the next round.
        with waiter._lock:
            rows = [waiter.rows[i] for i in waiter.events if i in waiter.rows]
        return {"ack_version": 1, "event_acks": rows}

    def drain_results(self, limit: int = 200) -> list[dict]:
        """Take the async business results (processed/rejected/retry) buffered
        from the server. Purely informational; nothing in the upload path
        depends on it."""
        with self._results_lock:
            rows = self._results[:limit]
            del self._results[:limit]
            return rows

    # -------------------------------------------------------- thread loop
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"ws thread exited: {exc}"
            log.exception("brain WS thread crashed")
        finally:
            with self._state_lock:
                self._connected = False

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            self._loop = loop
            self._outbound = asyncio.Queue(maxsize=4096)
        delay = RECONNECT_MIN_SECONDS
        while not self._stop.is_set():
            self._attempt_established = False
            try:
                await self._session()
                delay = RECONNECT_MIN_SECONDS
            except asyncio.CancelledError:
                raise
            except BrainWsAuthFailed as exc:
                # 4001: the token/device binding is wrong. Only a reissued token
                # can fix it, so back off instead of hammering the server.
                self.last_error = f"auth failed: {exc}"
                log.error("brain WS auth failed (backing off): %s", exc)
                delay = RECONNECT_MAX_SECONDS
            except BrainWsProtocolMismatch as exc:
                self.last_error = str(exc)
                log.error("brain WS protocol mismatch, falling back to HTTP: %s", exc)
                delay = RECONNECT_MAX_SECONDS
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"connection lost: {exc}"
                # Backoff only grows while the session never became ready, so a
                # healthy connection that drops recovers within a second.
                if self._attempt_established:
                    delay = RECONNECT_MIN_SECONDS
                log.warning("brain WS disconnected, retrying in %.0fs: %s", delay, exc)
            with self._state_lock:
                self._connected = False
                self._ws = None
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX_SECONDS, max(RECONNECT_MIN_SECONDS, delay * 2.0))

    async def _session(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed

        headers = {
            # WS uses Bearer auth, unlike the X-Agent-Token header on HTTP.
            "Authorization": f"Bearer {self.token}",
            "X-Agent-Id": self.agent_id,
            "X-Device-Id": self.device_id or self.agent_id,
            "X-Platform": self.platform,
            "User-Agent": self.user_agent,
        }
        kwargs: dict[str, Any] = {
            "max_size": MAX_FRAME_BYTES,
            "ping_interval": PING_INTERVAL,
            "ping_timeout": PING_TIMEOUT,
        }
        kwargs[_connect_header_kwarg()] = headers

        async with websockets.connect(self.ws_url, **kwargs) as ws:
            try:
                ready = json.loads(await asyncio.wait_for(
                    ws.recv(), timeout=READY_TIMEOUT_SECONDS))
            except ConnectionClosed as exc:
                # A failed auth closes during the handshake; surface it as such
                # instead of a generic disconnect.
                if getattr(exc, "code", None) == 4001 or \
                        getattr(getattr(exc, "rcvd", None), "code", None) == 4001:
                    reason = getattr(getattr(exc, "rcvd", None), "reason", "") or ""
                    raise BrainWsAuthFailed(reason or "close code 4001") from exc
                raise
            if not isinstance(ready, dict) or ready.get("type") != "ready":
                raise BrainWsProtocolMismatch(f"expected ready, got: {str(ready)[:120]}")
            version = ready.get("protocol_version")
            if version != PROTOCOL_VERSION:
                raise BrainWsProtocolMismatch(
                    f"protocol_version={version} requires {PROTOCOL_VERSION}")

            self.max_inflight = int(ready.get("max_inflight") or 0)
            with self._state_lock:
                self._connected = True
                self._ws = ws
                self.connected_at = time.time()
                self._attempt_established = True
                self.last_error = ""
            log.info("brain WS connected connection_id=%s max_inflight=%s",
                     ready.get("connection_id"), self.max_inflight)

            sender = asyncio.create_task(self._sender(ws))
            try:
                async for raw in ws:
                    self._on_frame(raw)
            finally:
                sender.cancel()
                with self._state_lock:
                    self._connected = False
                    self._ws = None

    async def _sender(self, ws) -> None:
        outbound = self._outbound
        if outbound is None:
            return
        while True:
            encoded = await outbound.get()
            await ws.send(encoded)

    def _send_control(self, frame: dict) -> None:
        """Queue a non-event control frame (command_ack / pong).

        Frames ride the same outbound queue as events, so ordering with the
        sender task is preserved and no lock is held across a send.
        """
        with self._state_lock:
            loop, outbound = self._loop, self._outbound
            connected = self._connected
        if not connected or loop is None or outbound is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                outbound.put(json.dumps(frame, ensure_ascii=False)), loop)
        except Exception:  # noqa: BLE001
            log.warning("failed to send brain WS control frame: %s", frame.get("type"))

    # -------------------------------------------------------- frame parsing
    def _on_frame(self, raw) -> None:
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "ack":
            self._on_ack(frame)
        elif kind == "result":
            self._on_result(frame)
        elif kind == "ping":
            # Some builds use an application-level ping instead of the
            # protocol-level one; answer both so neither side idles out.
            self._send_control({"type": "pong", "ts": time.time()})
        elif kind in {"command", "command_push"} or frame.get("command_id"):
            self._on_command_frame(frame)

    def _on_command_frame(self, frame: dict) -> None:
        """Handle a command pushed over the WS channel.

        Accepts the shapes the center has used: a `command` object, a flat
        frame with `command_id`, or fields nested under `payload`.
        """
        command = frame.get("command")
        if not isinstance(command, dict):
            command = dict(frame)
        payload = frame.get("payload")
        if isinstance(payload, dict):
            command = {**payload, **command}
        command.setdefault("id", frame.get("command_id"))
        command_id = str(command.get("id") or command.get("command_id") or "")
        self.commands_received += 1
        if command_id:
            # Ack receipt immediately so the center stops redelivering; the
            # execution result is reported separately once the command runs.
            self._send_control({
                "type": "command_ack",
                "command_id": command_id,
                "agent_id": self.agent_id,
                "status": "received",
            })
        if self._on_command is not None:
            try:
                self._on_command(command)
            except Exception:  # noqa: BLE001
                log.exception("brain WS command callback failed")

    def _on_ack(self, frame: dict) -> None:
        event_id = str(frame.get("event_id") or "")
        if not event_id:
            return
        self.acks_received += 1
        with self._owner_lock:
            waiter = self._owner.get(event_id)
        if waiter is None:
            return
        event = waiter.events.get(event_id) or {}
        row = ack_row_for(event, str(frame.get("status") or ""),
                          str(frame.get("error_code") or ""))
        if row is not None:
            waiter.on_ack(event_id, row)

    def _on_result(self, frame: dict) -> None:
        event_id = str(frame.get("event_id") or "")
        if not event_id:
            return
        self.results_received += 1
        row = {
            "event_id": event_id,
            "status": str(frame.get("status") or ""),
            "error_code": str(frame.get("error_code") or ""),
            "processed_at_ms": int(frame.get("server_processed_at_ms") or 0),
        }
        with self._results_lock:
            # Business results are informational; cap the buffer so a long run
            # without draining cannot grow memory without bound.
            if len(self._results) < _RESULT_BACKLOG_LIMIT:
                self._results.append(row)
