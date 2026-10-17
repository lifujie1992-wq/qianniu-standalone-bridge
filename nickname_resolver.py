"""Nickname metadata fallback for native-only Qianniu reception.

Qianniu writes structured IM/conversation notifications to its own app.log even
when recent.html (and therefore imsdk) is not loaded. Read only their identity
metadata. Never replay message bodies or change Qianniu's conversation cursor.
The normal SDK/contact lookup remains available in browser_bridge.js.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nickname_support import valid_nickname

LOG = logging.getLogger("qianniu_standalone.nicknames")
CCODE = re.compile(r"^(?P<buyer>\d+)(?:\.\d+)?-\d+(?:\.\d+)?#\d+@cntaobao$")
CONTEXT = re.compile(
    r"^\[\d{2}-\d{2} \d{2}:\d{2}:\d{2} \d+ \d+ \d+ [A-Z]+\] app "
    r"\[CHAT (?P<account>[^\]\r\n]{1,128}?)#3#(?P<seat>\d+) "
    r"(?P=account)#3#(?P=seat)\]"
)
EVENT = re.compile(
    r"(?:strEvent|strFullEvent)=(im\.(?:singlemsg\.(?:onReceiveNewMsg|"
    r"onShopRobotReceriveNewMsgs|onShopRobotLocalSendMsgs)|uiutil\.onConversationChange))[, ]"
)
JSON_START = re.compile(r"(?:jsonStr|w_json_str)\s*=\s*")
MAX_LINE = 256 * 1024


def nickname_records(line: str, now: float) -> list[tuple[str, str, str]]:
    """Accept only an explicit shop + full ccode + matching buyer identity."""
    if len(line) > MAX_LINE:
        return []
    context = CONTEXT.search(line)
    marker = JSON_START.search(line)
    if not context or not marker or not EVENT.search(line[context.end():marker.start()]):
        return []
    try:
        # Native timestamps omit the year. Reject old history and future lines.
        current = datetime.fromtimestamp(now)
        stamp = datetime.strptime(f"{current.year}-{line[1:15]}", "%Y-%m-%d %H:%M:%S")
        if stamp.timestamp() > now + 86400:
            stamp = stamp.replace(year=stamp.year - 1)
        if not now - 86400 <= stamp.timestamp() <= now + 300:
            return []
        payload, _end = json.JSONDecoder().raw_decode(line[marker.end():])
    except (ValueError, TypeError, OverflowError):
        return []
    account = context.group("account")
    rows = payload if isinstance(payload, list) else [payload]
    results = []
    for row in rows[:200]:
        if not isinstance(row, dict):
            continue
        cid = row.get("cid") if isinstance(row.get("cid"), dict) else row
        buyer_id = str(cid.get("ccode") or (row.get("cid") if isinstance(row.get("cid"), str) else "") or "")
        match = CCODE.fullmatch(buyer_id)
        if not match:
            continue
        target_id = str(cid.get("targetId") or "").removesuffix(".1")
        if target_id and target_id != match.group("buyer"):
            continue
        # cid.nick is the conversation peer, unlike a transfer message's sender
        # (which can be another member of staff). Never read the latter here.
        nick = next((nick for value in (cid.get("nickname"), cid.get("nick"), cid.get("display"))
                     if (nick := valid_nickname(value, buyer_id))), "")
        if not nick:
            messages = row.get("newmsgs") if isinstance(row.get("newmsgs"), list) else [row]
            for message in messages[:100]:
                if not isinstance(message, dict):
                    continue
                sender = message.get("fromId") or message.get("fromid") or message.get("sender")
                if not isinstance(sender, dict):
                    continue
                uid = str(sender.get("targetId") or sender.get("uid") or "").removesuffix(".1")
                if uid != match.group("buyer"):
                    continue
                nick = next((nick for value in (sender.get("nick"), sender.get("nickname"),
                             message.get("senderNick"), message.get("nick"), message.get("senderName"))
                             if (nick := valid_nickname(value, buyer_id))), "")
                if nick:
                    break
        shop = account.split(":", 1)[0]
        if nick and nick != account and nick != shop and not nick.startswith(shop + ":"):
            results.append((account, buyer_id, nick))
    return results


class QianniuNicknameLog:
    """Bounded incremental read of native nickname metadata, including rotation."""

    def __init__(self, root: Path, config: Any):
        self.root = root
        self.config = config
        self.files: dict[Path, tuple[tuple[int, int], int, bytes]] = {}
        self.names: dict[tuple[str, str], tuple[str, float]] = {}
        self.next_discovery = 0.0
        self.paths: list[Path] = []
        self.available = False
        self.last_error = ""

    def discover(self) -> None:
        configured = str(self.config.get("qianniu_log_dir", "") or "").strip()
        directories = [Path(configured)] if configured else []
        # Standard Qianniu data location, on the agent drive or another local
        # drive. No credential stores or encrypted contact databases are read.
        drives = {self.root.anchor, os.environ.get("SystemDrive", "C:") + "\\"}
        if os.name == "nt":
            import ctypes

            mask = ctypes.windll.kernel32.GetLogicalDrives()
            drives.update(f"{chr(65 + index)}:\\" for index in range(26)
                          if mask & (1 << index)
                          and ctypes.windll.kernel32.GetDriveTypeW(f"{chr(65 + index)}:\\") == 3)
        directories.extend(Path(drive) / "AliWorkbenchData/System/log" for drive in sorted(drives) if drive)
        profile = Path.home()
        directories.extend(profile / suffix / "AliWorkbenchData/System/log"
                           for suffix in ("Documents", "AppData/Local", "AppData/Roaming"))
        paths = []
        for directory in directories:
            for name in ("app.log.old", "app.log"):
                path = directory / name
                try:
                    if path.is_file() and path not in paths:
                        paths.append(path)
                except OSError:
                    continue
        self.paths = paths

    def poll(self) -> None:
        now = time.time()
        if time.monotonic() >= self.next_discovery:
            self.next_discovery = time.monotonic() + 60.0
            self.discover()
        self.available = bool(self.paths)
        self.last_error = ""
        for path in self.paths:
            try:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                previous = self.files.get(path)
                if not previous or previous[0] != identity or stat.st_size < previous[1]:
                    offset = max(0, stat.st_size - 8 * 1024 * 1024)
                    remainder = b""
                    skip_fragment = offset > 0
                else:
                    _identity, offset, remainder = previous
                    skip_fragment = False
                with path.open("rb") as stream:
                    stream.seek(offset)
                    chunk = stream.read(2 * 1024 * 1024)
                    offset = stream.tell()
                data = remainder + chunk
                if skip_fragment:
                    data = data.partition(b"\n")[2]
                lines = data.split(b"\n")
                remainder = lines.pop()
                # A truncated final line is kept until the next poll. If it is
                # oversized, retain a sentinel so no middle fragment is parsed.
                if len(remainder) > MAX_LINE:
                    remainder = b"!" * (MAX_LINE + 1)
                self.files[path] = (identity, offset, remainder)
                for raw in lines:
                    if len(raw) > MAX_LINE or b"@cntaobao" not in raw:
                        continue
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    for account, buyer_id, nick in nickname_records(line, now):
                        self.names[(account, buyer_id)] = (nick, now)
            except OSError as error:
                self.last_error = f"native_nickname_log_unavailable: {type(error).__name__}"
        if len(self.names) > 5000:
            self.names = dict(sorted(self.names.items(), key=lambda item: item[1][1], reverse=True)[:5000])
        self.names = {key: value for key, value in self.names.items() if value[1] >= now - 86400}

    def lookup(self, account: str, buyer_id: str) -> str:
        return self.names.get((account, buyer_id), ("", 0))[0]


class NicknameResolver:
    """Resolve missing nicknames promptly; backfill metadata through normal ingest."""

    def __init__(self, app: Any):
        self.app = app
        self.log = QianniuNicknameLog(app.db.path.parent.parent, app.config)
        self.wakeup = threading.Event()
        self.lock = threading.Lock()
        self.pending: dict[tuple[str, str], float] = {}
        self.last_scan = 0.0
        self.resolved = 0
        self.last_error = ""
        self.thread = threading.Thread(target=self.run, name="nickname-resolver", daemon=True)

    def enqueue(self, account: str, buyer_id: str) -> None:
        if not account or not CCODE.fullmatch(buyer_id) or self.app.db.cached_nickname(account, buyer_id):
            return
        with self.lock:
            if len(self.pending) < 1000:
                self.pending.setdefault((account, buyer_id), time.monotonic())
        self.wakeup.set()

    def cached_nickname(self, account: str, buyer_id: str) -> str:
        return valid_nickname(self.log.lookup(account, buyer_id), buyer_id)

    def scan_recent(self) -> None:
        cutoff = time.time() - 24 * 3600
        for item in self.app.db.workbench_sessions():
            if float(item.get("last_ts") or 0) >= cutoff:
                if not valid_nickname(item.get("buyer_nick"), item["buyer_id"]):
                    self.enqueue(item["account"], item["buyer_id"])

    def resolve_pending(self) -> None:
        self.log.poll()
        self.last_error = self.log.last_error
        with self.lock:
            keys = list(self.pending)
        for account, buyer_id in keys:
            nick = self.app.db.cached_nickname(account, buyer_id)
            if not nick:
                nick = self.log.lookup(account, buyer_id)
                if nick:
                    self.app.ingest_event({"type": "nickname_update", "platform": "taobao",
                                           "account": account, "buyer_id": buyer_id,
                                           "nickname": nick, "source": "qianniu_native_nickname_metadata"})
                    if self.app.db.cached_nickname(account, buyer_id):
                        self.resolved += 1
            if nick and self.app.db.cached_nickname(account, buyer_id):
                with self.lock:
                    self.pending.pop((account, buyer_id), None)
        cutoff = time.monotonic() - 86400
        with self.lock:
            self.pending = {key: at for key, at in self.pending.items() if at >= cutoff}
        if not self.log.available and self.pending:
            self.last_error = "native_nickname_log_unavailable"

    def status(self) -> dict[str, Any]:
        with self.lock:
            pending = len(self.pending)
        return {"pending": pending, "resolved": self.resolved, "native_log_available": self.log.available,
                "last_error": self.last_error}

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            try:
                if time.monotonic() - self.last_scan >= 60.0:
                    self.last_scan = time.monotonic()
                    self.scan_recent()
                self.resolve_pending()
            except Exception as error:
                self.last_error = str(error)[:200]
                LOG.debug("nickname worker failed", exc_info=True)
            self.wakeup.wait(1.0)
            self.wakeup.clear()
