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
    r"^\[\d{2}-\d{2} \d{2}:\d{2}:\d{2} \d+ (?P<pid>\d+) \d+ [A-Z]+\] app "
    r"\[CHAT (?P<account>[^\]\r\n]{1,128}?)#3#(?P<seat>\d+) "
    r"(?P=account)#3#(?P=seat)\]"
)
CONTEXT_SINGLE = re.compile(
    r"^\[\d{2}-\d{2} \d{2}:\d{2}:\d{2} \d+ (?P<pid>\d+) \d+ [A-Z]+\] app "
    r"\[CHAT (?P<account>[^\]\r\n]{1,128}?)#3#(?P<seat>\d+)\]"
)
EVENT = re.compile(
    r"(?:strEvent|strFullEvent)=(im\.(?:singlemsg\.(?:onReceiveNewMsg|"
    r"onShopRobotReceriveNewMsgs|onShopRobotLocalSendMsgs)|uiutil\.onConversationChange))[, ]"
)
JSON_START = re.compile(r"(?:jsonStr|w_json_str)\s*=\s*")
MAX_LINE = 256 * 1024


def notification_payload(line: str, now: float) -> tuple[Any, Any] | None:
    if len(line) > MAX_LINE:
        return None
    context = CONTEXT.search(line) or CONTEXT_SINGLE.search(line)
    marker = JSON_START.search(line)
    if not context or not marker or not EVENT.search(line[context.end():marker.start()]):
        return None
    try:
        # Native timestamps omit the year. Reject old history and future lines.
        current = datetime.fromtimestamp(now)
        stamp = datetime.strptime(f"{current.year}-{line[1:15]}", "%Y-%m-%d %H:%M:%S")
        if stamp.timestamp() > now + 86400:
            stamp = stamp.replace(year=stamp.year - 1)
        if not now - 86400 <= stamp.timestamp() <= now + 300:
            return None
        payload, _end = json.JSONDecoder().raw_decode(line[marker.end():])
    except (ValueError, TypeError, OverflowError):
        return None
    return context, payload


def nickname_records(line: str, now: float) -> list[tuple[str, str, str]]:
    """Accept only an explicit shop + full ccode + matching buyer identity."""
    decoded = notification_payload(line, now)
    if decoded is None:
        return []
    context, payload = decoded
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


def local_reply_records(line: str, now: float) -> list[tuple[str, str, float]]:
    """Read native local-send notifications, never unsent text or AI drafts."""
    if not re.search(r"(?:strEvent|strFullEvent)=im\.singlemsg\.onShopRobotLocalSendMsgs[, ]", line):
        return []
    decoded = notification_payload(line, now)
    if decoded is None:
        return []
    context, payload = decoded
    results = []
    for row in (payload if isinstance(payload, list) else [payload])[:200]:
        if not isinstance(row, dict) or not isinstance(row.get("cid"), dict):
            continue
        buyer_id = str(row["cid"].get("ccode") or "")
        match = CCODE.fullmatch(buyer_id)
        if not match or not isinstance(row.get("newmsgs"), list):
            continue
        for message in row["newmsgs"][:100]:
            if not isinstance(message, dict):
                continue
            sender, recipient, mcode = (message.get(key) for key in ("fromId", "toId", "mcode"))
            if not all(isinstance(value, dict) for value in (sender, recipient, mcode)):
                continue
            if str(sender.get("targetId") or "") != context.group("seat"):
                continue
            if str(recipient.get("targetId") or "") != match.group("buyer"):
                continue
            if not (mcode.get("messageId") or mcode.get("clientId")):
                continue
            try:
                stamp = float(message.get("sendTime") or 0)
                if stamp > 1e14:
                    stamp /= 1e6
                elif stamp > 1e11:
                    stamp /= 1000
            except (TypeError, ValueError, OverflowError):
                continue
            if now - 86400 <= stamp <= now + 300:
                results.append((context.group("account"), buyer_id, stamp))
    return results


class ConversationSelectionWatch:
    """Confirm a jump from new native selection events, never message arrivals.

    Keep a separate cursor so opening a conversation cannot consume nickname
    or sent-message notifications from the resolver's background worker.
    """

    def __init__(self, paths: list[Path], *, process_id: int = 0, recent_bytes: int = 0):
        self.paths = list(paths)
        self.process_id = process_id
        self.cursors: dict[tuple[int, int], tuple[int, bytes]] = {}
        self.selected: dict[str, str] = {}
        for path in self.paths:
            try:
                stat = path.stat()
                self.cursors[(stat.st_dev, stat.st_ino)] = (max(0, stat.st_size - recent_bytes), b"")
            except OSError:
                continue

    def poll(self) -> None:
        for path in self.paths:
            try:
                with path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    identity = (stat.st_dev, stat.st_ino)
                    offset, remainder = self.cursors.get(identity, (0, b""))
                    if stat.st_size < offset:
                        offset, remainder = 0, b""
                    if stat.st_size - offset > 2 * 1024 * 1024:
                        # A long idle gap must not leave a stale current-chat
                        # cache while only reading the start of a large backlog.
                        offset, remainder = stat.st_size - 2 * 1024 * 1024, b""
                        self.selected.clear()
                    stream.seek(offset)
                    data = remainder + stream.read(2 * 1024 * 1024)
                    offset = stream.tell()
                lines = data.split(b"\n")
                remainder = lines.pop()
                if len(remainder) > MAX_LINE:
                    remainder = b"!" * (MAX_LINE + 1)
                self.cursors[identity] = (offset, remainder)
                for raw in lines:
                    if len(raw) > MAX_LINE or b"im.uiutil.onConversationChange" not in raw:
                        continue
                    try:
                        line = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        try:
                            line = raw.decode("gb18030")
                        except UnicodeDecodeError:
                            continue
                    decoded = notification_payload(line, time.time())
                    event = EVENT.search(line)
                    if decoded is None or event is None or event.group(1) != "im.uiutil.onConversationChange":
                        continue
                    context, payload = decoded
                    if self.process_id and int(context.group("pid")) != self.process_id:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    cid = payload.get("cid") if isinstance(payload.get("cid"), dict) else payload
                    ccode = str(cid.get("ccode") or "")
                    match = CCODE.fullmatch(ccode)
                    target = str(cid.get("targetId") or "").removesuffix(".1")
                    if match and (not target or target == match.group("buyer")):
                        self.selected[context.group("account")] = ccode
            except OSError:
                continue


class QianniuNicknameLog:
    """Bounded incremental read of native nickname metadata, including rotation."""

    def __init__(self, root: Path, config: Any):
        self.root = root
        self.config = config
        self.files: dict[Path, tuple[tuple[int, int], int, bytes]] = {}
        self.names: dict[tuple[str, str], tuple[str, float]] = {}
        self.reply_updates: dict[tuple[str, str], float] = {}
        self.next_discovery = 0.0
        self.paths: list[Path] = []
        self.available = False
        self.last_error = ""
        self.lines_read = 0
        self.notifications_read = 0
        self.nickname_matches = 0
        self.decode_errors = 0
        self.discovery_sources: dict[Path, str] = {}

    def discover(self) -> None:
        configured = str(self.config.get("qianniu_log_dir", "") or "").strip()
        directories = [Path(configured)] if configured else []
        direct_paths: list[Path] = []
        def add_path(value: str, base: Path, source: str, is_log: bool = False) -> None:
            value = os.path.expandvars(value.strip().strip('"'))
            if not value or len(value) > 4096:
                return
            path = Path(value)
            if not path.is_absolute():
                path = base / path
            if is_log:
                # AliRender's own --log_file lives next to the native app log.
                directory = path.parent if path.suffix else path
                directories.append(directory)
                self.discovery_sources[directory] = source
            else:
                for directory in (path / "System/log", path / "log", path):
                    directories.append(directory)
                    self.discovery_sources[directory] = source

        # Read the actual running client's path instead of assuming its data
        # lives on the agent drive. This also handles custom install locations.
        runtime_roots = {self.root / "runtime"}
        try:
            import psutil

            for process in psutil.process_iter(["name", "exe", "cmdline"]):
                try:
                    if str(process.info.get("name") or "").lower() not in {"aliworkbench.exe", "alirender.exe"}:
                        continue
                    executable = Path(process.info.get("exe") or "")
                    runtime_roots.update((executable.parent, executable.parent.parent))
                    args = process.info.get("cmdline") or []
                    for index, arg in enumerate(args):
                        if arg.lower().startswith("--log_file="):
                            add_path(arg.split("=", 1)[1], executable.parent, "running_client", True)
                        elif arg.lower() == "--log_file" and index + 1 < len(args):
                            add_path(args[index + 1], executable.parent, "running_client", True)
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
        except ImportError:
            pass
        for runtime in runtime_roots:
            for name in ("version.ini", "AliWorkbench.ini", "config.ini"):
                try:
                    for line in (runtime / name).read_text(encoding="utf-8-sig").splitlines():
                        key, separator, value = line.partition("=")
                        if separator and key.strip().lower() in {"logdir", "logpath", "datapath", "datadir", "userdatadir"}:
                            add_path(value, runtime, "client_config", key.strip().lower().startswith("log"))
                except (OSError, UnicodeError):
                    continue
        if os.name == "nt":
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for key in (r"Software\Alibaba\AliWorkbench", r"Software\Alibaba\Qianniu",
                            r"SOFTWARE\WOW6432Node\Alibaba\AliWorkbench"):
                    try:
                        with winreg.OpenKey(hive, key) as handle:
                            for index in range(winreg.QueryInfoKey(handle)[1]):
                                name, value, _kind = winreg.EnumValue(handle, index)
                                if isinstance(value, str) and name.lower() in {"datapath", "datadir", "userdatadir", "logdir", "logpath"}:
                                    add_path(value, self.root, "client_registry", name.lower().startswith("log"))
                    except OSError:
                        continue
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
            try:
                if directory.is_file():
                    direct_paths.append(directory)
                    continue
            except OSError:
                continue
            for name in ("app.log.old", "app.log"):
                path = directory / name
                try:
                    if path.is_file() and path not in paths:
                        paths.append(path)
                        self.discovery_sources[path] = self.discovery_sources.get(directory, "configured" if configured and directory == Path(configured) else "standard_location")
                except OSError:
                    continue
        self.paths = list(dict.fromkeys(paths + direct_paths))

    def poll(self) -> None:
        now = time.time()
        self.reply_updates = {}
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
                        line = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        try:
                            line = raw.decode("gb18030")
                        except UnicodeDecodeError:
                            self.decode_errors += 1
                            continue
                    self.lines_read += 1
                    if EVENT.search(line):
                        self.notifications_read += 1
                    for account, buyer_id, nick in nickname_records(line, now):
                        self.names[(account, buyer_id)] = (nick, now)
                        self.nickname_matches += 1
                    for account, buyer_id, stamp in local_reply_records(line, now):
                        key = (account, buyer_id)
                        self.reply_updates[key] = max(self.reply_updates.get(key, 0), stamp)
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
        if self.log.reply_updates:
            self.app.db.remember_reply_receipts([
                (account, buyer_id, stamp) for (account, buyer_id), stamp in self.log.reply_updates.items()
            ])
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
                "log_files": [{"path": str(path), "source": self.log.discovery_sources.get(path, "configured")}
                              for path in self.log.paths],
                "lines_read": self.log.lines_read, "notifications_read": self.log.notifications_read,
                "nickname_matches": self.log.nickname_matches, "decode_errors": self.log.decode_errors,
                "last_error": self.last_error or ("nickname_mapping_not_found" if pending else "")}

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
