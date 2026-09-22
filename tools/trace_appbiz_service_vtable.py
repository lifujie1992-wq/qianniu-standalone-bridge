import argparse
import json
import time

import frida


AGENT = r"""
'use strict';

const appBiz = Process.getModuleByName('AppBiz.dll');
const serviceVtable = appBiz.base.add(0x18af478);
const secondaryVtable1 = appBiz.base.add(0x18af5e0);
const secondaryVtable2 = appBiz.base.add(0x18af5f0);
const candidateAddresses = [];
const candidateSet = new Set();
const activity = {};
const hookedFunctions = new Set();
let hookCount = 0;

function readable(address) {
  if (address === null || address.isNull()) return false;
  const range = Process.findRangeByAddress(address);
  return range !== null && range.protection.indexOf('r') >= 0;
}

function executable(address) {
  if (address === null || address.isNull()) return false;
  const range = Process.findRangeByAddress(address);
  return range !== null && range.protection.indexOf('x') >= 0;
}

function readMsvcString(address) {
  try {
    if (!readable(address)) return null;
    const size = Number(address.add(0x10).readU64().toString());
    const capacity = Number(address.add(0x18).readU64().toString());
    if (!Number.isSafeInteger(size) || size < 3 || size > 512) return null;
    if (!Number.isSafeInteger(capacity) || capacity < size || capacity > 4096) return null;
    const data = capacity >= 16 ? address.readPointer() : address;
    if (!readable(data)) return null;
    const value = data.readUtf8String(size);
    if (/[^\x09\x0a\x0d\x20-\x7e\u0080-\uffff]/.test(value)) return null;
    return value;
  } catch (_) {
    return null;
  }
}

function scanCandidates() {
  for (const range of Process.enumerateRanges({protection: 'rw-', coalesce: true})) {
    let matches = [];
    try {
      matches = Memory.scanSync(range.base, range.size, serviceVtable.toMatchPattern());
    } catch (_) {
      continue;
    }
    for (const match of matches) {
      const object = match.address;
      try {
        if (!object.add(872).readPointer().equals(secondaryVtable1)) continue;
        if (!object.add(888).readPointer().equals(secondaryVtable2)) continue;
        const key = object.toString();
        if (candidateSet.has(key)) continue;
        candidateSet.add(key);
        candidateAddresses.push(key);
        activity[key] = {total: 0, slots: {}, samples: []};
      } catch (_) {}
    }
  }
}

function recordCall(functionAddress, slots, args) {
  const service = args[0].toString();
  if (!candidateSet.has(service)) return;
  const row = activity[service];
  row.total += 1;
  for (const slot of slots) {
    const key = String(slot);
    row.slots[key] = (row.slots[key] || 0) + 1;
  }
  if (row.samples.length >= 40) return;
  const strings = [];
  for (let index = 1; index <= 6; index += 1) {
    const value = readMsvcString(args[index]);
    if (value !== null) strings.push({arg: index, value: value});
  }
  row.samples.push({
    function: functionAddress.toString(),
    rva: '0x' + functionAddress.sub(appBiz.base).toString(16),
    slots: slots,
    strings: strings,
  });
}

function installHooks() {
  const functions = {};
  for (let slot = 0; slot < 44; slot += 1) {
    let address;
    try {
      address = serviceVtable.add(slot * Process.pointerSize).readPointer();
    } catch (_) {
      continue;
    }
    if (!executable(address)) continue;
    const key = address.toString();
    if (!functions[key]) functions[key] = {address: address, slots: []};
    functions[key].slots.push(slot);
  }
  for (const key of Object.keys(functions)) {
    const item = functions[key];
    if (hookedFunctions.has(key)) continue;
    hookedFunctions.add(key);
    Interceptor.attach(item.address, {
      onEnter(args) {
        recordCall(item.address, item.slots, args);
      },
    });
    hookCount += 1;
  }
}

scanCandidates();
installHooks();

rpc.exports = {
  snapshot() {
    return {
      pid: Process.id,
      module_base: appBiz.base.toString(),
      candidate_count: candidateAddresses.length,
      candidates: candidateAddresses,
      hook_count: hookCount,
      activity: activity,
    };
  },
};

send({event: 'ready', pid: Process.id, candidates: candidateAddresses, hook_count: hookCount});
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--output")
    args = parser.parse_args()

    session = frida.get_local_device().attach(args.pid)
    script = session.create_script(AGENT)
    messages = []
    script.on("message", lambda message, _data: messages.append(message))
    try:
        script.load()
        time.sleep(max(0.5, args.seconds))
        result = script.exports_sync.snapshot()
        result["messages"] = messages
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(rendered + "\n")
        print(rendered)
    finally:
        try:
            script.unload()
        finally:
            session.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
