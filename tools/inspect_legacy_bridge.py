from __future__ import annotations

import argparse
import inspect
import json
import marshal
import struct
import sys
import types
import zlib
from pathlib import Path
from typing import Any


def nested(code: types.CodeType):
    yield code
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from nested(value)


def load_pyz_code(root: Path, module_name: str) -> types.CodeType:
    sys.path.insert(0, str(root))
    try:
        from repack_v0516 import _read_archive
    finally:
        sys.path.pop(0)
    _bootloader, entries, _pyvers, _pylib = _read_archive(root / "QianniuBridgeAgent.exe")
    entry = next(item for item in entries if item["name"] == "PYZ.pyz" and item["typecode"] == b"z")
    raw = zlib.decompress(entry["blob"]) if entry["compressed"] else entry["blob"]
    offset = struct.unpack("!I", raw[8:12])[0]
    toc = marshal.loads(raw[offset:])
    if isinstance(toc, list):
        toc = dict(toc)
    _kind, position, length = toc[module_name]
    return marshal.loads(zlib.decompress(raw[position : position + length]))


def load_client(root: Path) -> types.ModuleType:
    bridge = types.ModuleType("bridge")
    bridge.__path__ = []
    bridge.__version__ = "0.5.16"
    previous = sys.modules.get("bridge")
    sys.modules["bridge"] = bridge
    try:
        module = types.ModuleType("bridge.client")
        module.__package__ = "bridge"
        exec(load_pyz_code(root, "bridge.client"), module.__dict__)
        return module
    finally:
        if previous is None:
            sys.modules.pop("bridge", None)
        else:
            sys.modules["bridge"] = previous


def method_probe(client: Any, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []

    def capture(method: str, path: str, body: Any = None, **request_kwargs: Any) -> dict[str, Any]:
        calls.append({
            "method": method,
            "path": path,
            "body": body,
            "kwargs": request_kwargs,
        })
        return {"ok": True, "commands": []}

    client._request = capture
    result = getattr(client, name)(*args, **kwargs)
    return {"calls": calls, "result": result}


def code_summary(code: types.CodeType) -> dict[str, Any]:
    return {
        "args": list(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]),
        "names": list(code.co_names),
        "strings": [value for value in code.co_consts if isinstance(value, str)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the legacy packaged bridge protocol")
    parser.add_argument("legacy_root", type=Path)
    args = parser.parse_args()
    root = args.legacy_root.resolve()
    module = load_client(root)
    client_type = module.BridgeClient
    init = inspect.signature(client_type)
    client = client_type(
        "http://brain.example:18765",
        "probe-token",
        "probe-agent",
        "Probe seat",
        local_workbench_url="",
        dual_write_local=False,
    )
    probes = {
        "register": method_probe(client, "register"),
        "heartbeat": method_probe(client, "heartbeat", {"platform": "taobao", "ready": True}),
        "upload_events": method_probe(client, "upload_events", [{"event_id": "event-1"}]),
        "pull_commands": method_probe(client, "pull_commands", wait_seconds=1.25),
        "report_command_result": method_probe(client, "report_command_result", "command-1", {"ok": True}),
    }
    modules: dict[str, Any] = {}
    for module_name in ("bridge.client", "bridge.agent"):
        root_code = load_pyz_code(root, module_name)
        modules[module_name] = {
            code.co_name: code_summary(code)
            for code in nested(root_code)
            if code.co_name in {
                "register",
                "heartbeat",
                "upload_events",
                "pull_commands",
                "report_command_result",
                "_handle_command",
                "_execute_command",
                "run",
            }
            or "command" in code.co_name.lower()
            or "reply" in code.co_name.lower()
            or "handoff" in code.co_name.lower()
        }
    print(json.dumps({
        "client_signature": str(init),
        "probes": probes,
        "modules": modules,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
