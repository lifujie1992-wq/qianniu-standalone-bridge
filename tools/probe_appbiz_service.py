import argparse
import json
import time
from pathlib import Path

import frida
import psutil


APPBIZ_VTABLE_RVA = 0x18AF478
APPBIZ_SECONDARY_VTABLE_1_RVA = 0x18AF5E0
APPBIZ_SECONDARY_VTABLE_2_RVA = 0x18AF5F0
APP_SERVICE_SEND_TEXT_RVA = 0xA77FF0
MESSAGE_BIZ_SEND_TEXT_RVA = 0xA59120
MESSAGE_BIZ_VTABLE_RVA = 0x18AD4F8


AGENT_SOURCE = r"""
'use strict';

const appBiz = Process.getModuleByName('AppBiz.dll');
const serviceVtable = appBiz.base.add(__VTABLE_RVA__);
const secondaryVtable1 = appBiz.base.add(__SECONDARY_VTABLE_1_RVA__);
const secondaryVtable2 = appBiz.base.add(__SECONDARY_VTABLE_2_RVA__);
const serviceSendText = appBiz.base.add(__SERVICE_SEND_RVA__);
const messageBizSendText = appBiz.base.add(__MESSAGE_SEND_RVA__);
const messageBizVtable = appBiz.base.add(__MESSAGE_VTABLE_RVA__);
const captureContent = __CAPTURE_CONTENT__;
const candidates = [];
let serviceCalls = 0;
let messageBizCalls = 0;

function readMsvcString(address) {
  try {
    if (address.isNull()) return {ok: false, error: 'null'};
    const size = Number(address.add(0x10).readU64().toString());
    const capacity = Number(address.add(0x18).readU64().toString());
    if (!Number.isSafeInteger(size) || size < 0 || size > 1024 * 1024) {
      return {ok: false, error: 'invalid_size', size: size, capacity: capacity};
    }
    const data = capacity >= 16 ? address.readPointer() : address;
    const value = size ? data.readUtf8String(size) : '';
    return {
      ok: true,
      size: size,
      capacity: capacity,
      value: captureContent ? value : undefined,
    };
  } catch (error) {
    return {ok: false, error: String(error)};
  }
}

function readHex(address, length) {
  try {
    if (address.isNull()) return null;
    const bytes = new Uint8Array(address.readByteArray(length));
    return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  } catch (error) {
    return 'error:' + String(error);
  }
}

function scanMsvcStrings(address, span) {
  const rows = [];
  if (address === null || address.isNull()) return rows;
  for (let offset = 0; offset + 0x20 <= span && rows.length < 40; offset += 8) {
    const value = readMsvcString(address.add(offset));
    if (!value.ok || value.size < 2 || value.size > 256 || value.capacity < value.size || value.capacity > 4096) continue;
    if (captureContent && !/^[\x20-\x7e\u4e00-\u9fff]+$/.test(value.value || '')) continue;
    rows.push({offset: '0x' + offset.toString(16), size: value.size, value: value.value});
  }
  return rows;
}

for (const range of Process.enumerateRanges({protection: 'rw-', coalesce: true})) {
  let matches = [];
  try {
    matches = Memory.scanSync(range.base, range.size, serviceVtable.toMatchPattern());
  } catch (_) {
    continue;
  }
  for (const match of matches) {
    if (candidates.length >= 64) break;
    const object = match.address;
    let secondary1 = 'unreadable';
    let secondary2 = 'unreadable';
    let messageBiz = 'unreadable';
    let messageBizObjectVtable = 'unreadable';
    try { secondary1 = object.add(872).readPointer().toString(); } catch (_) {}
    try { secondary2 = object.add(888).readPointer().toString(); } catch (_) {}
    try {
      const pointer = object.add(0x578).readPointer();
      messageBiz = pointer.toString();
      messageBizObjectVtable = pointer.readPointer().toString();
    } catch (_) {}
    candidates.push({
      address: object.toString(),
      secondary_vtable_1: secondary1,
      secondary_vtable_1_valid: secondary1 === secondaryVtable1.toString(),
      secondary_vtable_2: secondary2,
      secondary_vtable_2_valid: secondary2 === secondaryVtable2.toString(),
      message_biz: messageBiz,
      message_biz_vtable: messageBizObjectVtable,
      message_biz_valid: messageBizObjectVtable === messageBizVtable.toString(),
      service_strings: scanMsvcStrings(object, 0x700),
      message_biz_strings: messageBiz === 'unreadable' ? [] : scanMsvcStrings(ptr(messageBiz), 0x700),
    });
  }
  if (candidates.length >= 64) break;
}

Interceptor.attach(serviceSendText, {
  onEnter(args) {
    serviceCalls += 1;
    send({
      event: 'app_service_send_text',
      this_pointer: args[0].toString(),
      object_is_candidate: candidates.some(item => item.address === args[0].toString()),
      arg1: readMsvcString(args[1]),
      arg2: readMsvcString(args[2]),
      arg3: readMsvcString(args[3]),
      arg4_pointer: args[4].toString(),
      arg4_first_96_bytes: readHex(args[4], 96),
      arg5_pointer: args[5].toString(),
      arg5_first_64_bytes: readHex(args[5], 64),
    });
  },
});

Interceptor.attach(messageBizSendText, {
  onEnter(args) {
    messageBizCalls += 1;
    send({
      event: 'message_biz_send_text',
      this_pointer: args[0].toString(),
      arg1: readMsvcString(args[1]),
      arg2: readMsvcString(args[2]),
      arg3: readMsvcString(args[3]),
      arg4_pointer: args[4].toString(),
      arg4_first_96_bytes: readHex(args[4], 96),
      arg5_pointer: args[5].toString(),
      arg5_first_64_bytes: readHex(args[5], 64),
    });
  },
});

send({
  event: 'probe_ready',
  module_base: appBiz.base.toString(),
  service_vtable: serviceVtable.toString(),
  service_send_text: serviceSendText.toString(),
  message_biz_send_text: messageBizSendText.toString(),
  service_object_candidates: candidates,
});

rpc.exports.status = function () {
  return {
    module_base: appBiz.base.toString(),
    service_object_candidates: candidates,
    service_calls: serviceCalls,
    message_biz_calls: messageBizCalls,
  };
};
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe the 9.97 AppBiz text-send path without calling it")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--capture-content", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if not psutil.pid_exists(args.pid):
        raise SystemExit(f"PID does not exist: {args.pid}")
    source = AGENT_SOURCE.replace("__VTABLE_RVA__", hex(APPBIZ_VTABLE_RVA))
    source = source.replace("__SECONDARY_VTABLE_1_RVA__", hex(APPBIZ_SECONDARY_VTABLE_1_RVA))
    source = source.replace("__SECONDARY_VTABLE_2_RVA__", hex(APPBIZ_SECONDARY_VTABLE_2_RVA))
    source = source.replace("__SERVICE_SEND_RVA__", hex(APP_SERVICE_SEND_TEXT_RVA))
    source = source.replace("__MESSAGE_SEND_RVA__", hex(MESSAGE_BIZ_SEND_TEXT_RVA))
    source = source.replace("__MESSAGE_VTABLE_RVA__", hex(MESSAGE_BIZ_VTABLE_RVA))
    source = source.replace("__CAPTURE_CONTENT__", "true" if args.capture_content else "false")

    events = []
    session = None
    script = None
    probe_error = ""
    status = {}

    def on_message(message, _data):
        if message.get("type") == "send":
            events.append(message.get("payload"))
        else:
            events.append({"event": "frida_error", "detail": message})

    try:
        session = frida.get_local_device().attach(args.pid)
        script = session.create_script(source)
        script.on("message", on_message)
        script.load()
        time.sleep(max(0.5, args.seconds))
        status = script.exports_sync.status()
    except Exception as error:
        probe_error = f"{type(error).__name__}: {error}"
    finally:
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass

    output = {
        "pid": args.pid,
        "process_alive": psutil.pid_exists(args.pid),
        "capture_content": args.capture_content,
        "status": status,
        "events": events,
        "probe_error": probe_error,
        "send_calls_initiated_by_probe": 0,
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if not probe_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
