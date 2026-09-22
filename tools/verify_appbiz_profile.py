"""Verify an AppBiz hook profile against the live Qianniu process.

Run this after a client upgrade to prove the offsets in appbiz_agent.js really
point at the functions they claim, instead of waiting for a send to fail.

It reads the AppBiz.dll image in the running process, checks that every profile
RVA is executable, and confirms the two vtable slots the agent asserts at attach
time. Read-only: no hooks are installed, nothing is sent.

Usage:
    py -3.10 tools/verify_appbiz_profile.py [--pid N] [--profile 9.97.81N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import frida
import psutil

# Mirrors HOOK_PROFILES in appbiz_agent.js. Kept here so the checker can run
# standalone against a specific build.
PROFILES: dict[str, dict[str, str]] = {
    "0x1f05000": {
        "version": "9.97.59N",
        "serviceVtable": "0x18af478",
        "secondaryVtable1": "0x18af5e0",
        "secondaryVtable2": "0x18af5f0",
        "serviceGetNewMsg": "0xa6c080",
        "serviceOnMessageArrive": "0xa6f9a0",
        "serviceSendText": "0xa77ff0",
        "messageBizSendText": "0xa59120",
    },
    "0x1f17000": {
        "version": "9.97.74N",
        "serviceVtable": "0x18bdee8",
        "secondaryVtable1": "0x18be050",
        "secondaryVtable2": "0x18be060",
        "serviceGetNewMsg": "0xa77b50",
        "serviceOnMessageArrive": "0xa7b470",
        "serviceSendText": "0xa83af0",
        "messageBizSendText": "0xa64bf0",
    },
    "0x1f20000": {
        "version": "9.97.81N",
        "serviceVtable": "0x18c4c78",
        "secondaryVtable1": "0x18c4de0",
        "secondaryVtable2": "0x18c4df0",
        "serviceGetNewMsg": "0xa798a0",
        "serviceOnMessageArrive": "0xa7d1c0",
        "serviceSendText": "0xa85840",
        "messageBizSendText": "0xa66940",
    },
}

AGENT = """
rpc.exports = {
  probe: function (profileJson) {
    var profile = JSON.parse(profileJson);
    var mod = Process.getModuleByName('AppBiz.dll');
    function at(rva) { return mod.base.add(parseInt(rva, 16)); }
    function executable(addr) {
      var range = Process.findRangeByAddress(addr);
      return !!(range && range.protection.indexOf('x') >= 0);
    }
    function vtableShape(addr, entries) {
      // MSVC vtables for classes with multiple inheritance mix executable
      // function pointers with non-executable data slots (secondary vtable
      // references, adjustor data), so require a majority of executable slots
      // instead of all of them.
      var executable = 0;
      for (var i = 0; i < entries; i++) {
        var slot = addr.add(i * 8).readPointer();
        var range = Process.findRangeByAddress(slot);
        if (range && range.protection.indexOf('x') >= 0) executable += 1;
      }
      return executable >= Math.ceil(entries / 2);
    }
    var checks = {};
    ['serviceGetNewMsg', 'serviceOnMessageArrive', 'serviceSendText', 'messageBizSendText']
      .forEach(function (name) { checks['executable:' + name] = executable(at(profile[name])); });
    ['secondaryVtable1', 'secondaryVtable2', 'messageBizVtable'].forEach(function (name) {
      if (profile[name]) checks['shape:' + name] = vtableShape(at(profile[name]), 4);
    });
    var service = at(profile.serviceVtable);
    checks['serviceVtable+0x18 == GetNewMsg'] =
      service.add(0x18).readPointer().equals(at(profile.serviceGetNewMsg));
    checks['serviceVtable+0x90 == SendText'] =
      service.add(0x90).readPointer().equals(at(profile.serviceSendText));
    checks['shape:serviceVtable'] = vtableShape(service, 4);
    return {
      module: mod.name,
      module_base: mod.base.toString(),
      module_size: mod.size,
      size_key: '0x' + mod.size.toString(16),
      checks: checks
    };
  }
};
"""


def find_qianniu() -> int:
    for process in psutil.process_iter(["pid", "name"]):
        if str(process.info.get("name") or "").lower() == "aliworkbench.exe":
            return int(process.info["pid"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an AppBiz profile inside the live client")
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--profile", default="", help="profile label to use, e.g. 9.97.81N")
    parser.add_argument("--scan-root", type=Path, help="bridge root, used only to report the agent table")
    args = parser.parse_args()

    pid = args.pid or find_qianniu()
    if not pid:
        print("Qianniu is not running", file=sys.stderr)
        return 2

    session = frida.get_local_device().attach(pid)
    try:
        script = session.create_script(AGENT)
        script.load()
        # First pass with an empty profile just to read the module size.
        probe = script.exports_sync.probe(json.dumps({}))
    except Exception as error:  # noqa: BLE001
        session.detach()
        print(f"probe failed: {error}", file=sys.stderr)
        return 2

    size_key = probe["size_key"]
    profile = PROFILES.get(size_key)
    if profile is None:
        session.detach()
        print(f"no known profile for {probe['module']} size {size_key}")
        print("run tools/map_appbiz_offsets.py against a known build to derive one")
        return 1

    try:
        result = script.exports_sync.probe(json.dumps(profile))
    finally:
        session.detach()

    print(f"process     : {pid}")
    print(f"module      : {result['module']} base={result['module_base']} size={result['size_key']}")
    print(f"profile     : {profile['version']}")
    print()
    failed = []
    for name, ok in result["checks"].items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failed.append(name)

    print()
    if failed:
        print(f"VERDICT: profile does NOT match this build ({len(failed)} failing check(s))")
        return 1
    print("VERDICT: profile matches the live build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
