"""Load the appbiz adapter inside the live Qianniu process and probe it.

Read-only check: it loads the DLL, resolves every export the agent needs, calls
appbiz_adapter_layout to read the MSVC STL sizes, and detaches. It never calls
send, so no message leaves the account.

Usage:
    py -3.10 tools/verify_adapter_load.py [--dll build/appbiz_adapter.dll]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import frida
import psutil

AGENT = """
rpc.exports = {
  probe: function (path) {
    var mod = Module.load(path);
    var layout = new NativeFunction(
      mod.getExportByName('appbiz_adapter_layout'), 'int', ['pointer', 'pointer', 'pointer']);
    var a = Memory.alloc(4), b = Memory.alloc(4), c = Memory.alloc(4);
    var rc = layout(a, b, c);
    var names = ['appbiz_send_text_v1', 'appbiz_send_text_v2',
                 'appbiz_poll_send_result_v1', 'appbiz_cancel_send_result_v1'];
    var exports = {};
    names.forEach(function (name) { exports[name] = mod.findExportByName(name) !== null; });
    return {
      path: mod.path,
      size: mod.size,
      layout_rc: rc,
      string_size: a.readU32(),
      map_size: b.readU32(),
      func_size: c.readU32(),
      exports: exports
    };
  }
};
"""

EXPECTED_LAYOUT = {"string_size": 32, "map_size": 64, "func_size": 64}


def find_qianniu() -> int:
    for process in psutil.process_iter(["pid", "name"]):
        if str(process.info.get("name") or "").lower() == "aliworkbench.exe":
            return int(process.info["pid"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the adapter inside the running Qianniu process")
    parser.add_argument(
        "--dll",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "build" / "appbiz_adapter.dll",
    )
    args = parser.parse_args()

    dll = args.dll.resolve()
    if not dll.is_file():
        print(f"adapter not found: {dll}", file=sys.stderr)
        return 2

    pid = find_qianniu()
    if not pid:
        print("Qianniu is not running", file=sys.stderr)
        return 2

    session = frida.get_local_device().attach(pid)
    try:
        script = session.create_script(AGENT)
        script.load()
        result = script.exports_sync.probe(str(dll))
    finally:
        session.detach()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    ok = (
        result["layout_rc"] == 0
        and all(result[key] == value for key, value in EXPECTED_LAYOUT.items())
        and all(result["exports"].values())
    )
    print("VERDICT:", "adapter usable" if ok else "adapter NOT usable")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
