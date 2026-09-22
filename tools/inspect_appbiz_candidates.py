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

function readable(address) {
  return !address.isNull() && Process.findRangeByAddress(address) !== null;
}

function usefulString(value) {
  if (typeof value !== 'string' || value.length < 3 || value.length > 256) return false;
  return !/[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]/.test(value);
}

function readStdString(address) {
  try {
    const size = Number(address.add(0x10).readU64().toString());
    const capacity = Number(address.add(0x18).readU64().toString());
    if (!Number.isSafeInteger(size) || !Number.isSafeInteger(capacity)) return null;
    if (size < 3 || size > 256 || capacity < size || capacity > 4096) return null;
    const data = capacity >= 16 ? address.readPointer() : address;
    if (!readable(data)) return null;
    const value = data.readUtf8String(size);
    return usefulString(value) ? value : null;
  } catch (_) {
    return null;
  }
}

function scanStdStrings(base, size) {
  const output = [];
  const seen = new Set();
  for (let offset = 0; offset <= size - 32; offset += 8) {
    const value = readStdString(base.add(offset));
    if (value === null || seen.has(value)) continue;
    seen.add(value);
    output.push({offset: '0x' + offset.toString(16), value: value});
  }
  return output;
}

function scanLinkedStrings(base, size) {
  const output = [];
  const seenPointers = new Set();
  const seenValues = new Set();
  for (let offset = 0; offset < size; offset += Process.pointerSize) {
    let target;
    try {
      target = base.add(offset).readPointer();
    } catch (_) {
      continue;
    }
    const key = target.toString();
    if (!readable(target) || seenPointers.has(key)) continue;
    seenPointers.add(key);
    for (const item of scanStdStrings(target, 0x300)) {
      if (seenValues.has(item.value)) continue;
      seenValues.add(item.value);
      output.push({
        pointer_offset: '0x' + offset.toString(16),
        target: key,
        string_offset: item.offset,
        value: item.value,
      });
      if (output.length >= 80) return output;
    }
  }
  return output;
}

function scanConversationMap(messageBiz) {
  const output = [];
  const seenNodes = new Set();
  let sentinel;
  let node;
  try {
    sentinel = messageBiz.add(0x4a8).readPointer();
    node = sentinel.readPointer();
  } catch (_) {
    return output;
  }
  while (readable(node) && !node.equals(sentinel) && output.length < 512) {
    const key = node.toString();
    if (seenNodes.has(key)) break;
    seenNodes.add(key);
    const strings = scanStdStrings(node, 0x78);
    output.push({node: key, strings: strings});
    try {
      node = node.readPointer();
    } catch (_) {
      break;
    }
  }
  return output;
}

function scanCandidates() {
  const candidates = [];
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
        const convBiz = object.add(0x578).readPointer();
        candidates.push({
          address: object.toString(),
          conv_biz: convBiz.toString(),
          conv_biz_readable: readable(convBiz),
          object_strings: scanStdStrings(object, 0x700),
          linked_strings: scanLinkedStrings(object, 0x700),
          conv_biz_strings: readable(convBiz) ? scanStdStrings(convBiz, 0x500) : [],
          conv_biz_linked_strings: readable(convBiz) ? scanLinkedStrings(convBiz, 0x500) : [],
          conversation_map: readable(convBiz) ? scanConversationMap(convBiz) : [],
        });
      } catch (_) {}
    }
  }
  return candidates;
}

rpc.exports = {
  status() {
    return {
      module_base: appBiz.base.toString(),
      candidates: scanCandidates(),
    };
  },
};
"""

MONITOR_AGENT = r"""
'use strict';

const appBiz = Process.getModuleByName('AppBiz.dll');
const hooks = [
  ['message_arrive', 0xa6f9a0],
  ['roam_result', 0xa723b0],
  ['roam_one', 0xa74510],
  ['roam_many', 0xa75460],
  ['official_send', 0xa77ff0],
];

for (const [reason, rva] of hooks) {
  Interceptor.attach(appBiz.base.add(rva), {
    onEnter(args) {
      send({event: 'service_call', reason: reason, service: args[0].toString()});
    },
  });
}

send({event: 'monitor_ready'});
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pid", type=int)
    parser.add_argument("--monitor-seconds", type=float, default=0.0)
    args = parser.parse_args()

    session = frida.get_local_device().attach(args.pid)
    events = []
    source = MONITOR_AGENT if args.monitor_seconds > 0 else AGENT
    script = session.create_script(source)
    script.on("message", lambda message, _data: events.append(message.get("payload", message)))
    try:
        script.load()
        if args.monitor_seconds > 0:
            time.sleep(args.monitor_seconds)
            print(json.dumps(events, indent=2))
        else:
            print(json.dumps(script.exports_sync.status(), indent=2))
    finally:
        try:
            script.unload()
        finally:
            session.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
