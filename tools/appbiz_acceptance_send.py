from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import frida


AGENT = r"""
'use strict';

const appBiz = Process.getModuleByName('AppBiz.dll');
const expectedVtable = appBiz.base.add(0x18af478);
const service = ptr('__SERVICE__');
if (!service.readPointer().equals(expectedVtable)) {
  throw new Error('service vtable mismatch');
}
const helper = Module.load(__HELPER__);
const sendText = new NativeFunction(
  helper.getExportByName('appbiz_send_text_v1'),
  'int',
  ['pointer', 'pointer', 'pointer', 'pointer', 'pointer']
);

rpc.exports = {
  sendonce(ccode, content, pcsource) {
    const first = Memory.allocUtf8String(String(ccode));
    const second = Memory.allocUtf8String(String(content));
    const third = Memory.allocUtf8String(String(pcsource));
    return sendText(service, appBiz.base, first, second, third);
  },
};
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one non-repeatable AppBiz acceptance send.")
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--service", required=True)
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--ccode", required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--pcsource", default="QianniuStandaloneBridge/1.1.0")
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()

    stable = json.dumps(
        {"ccode": args.ccode, "content": args.content, "pcsource": args.pcsource},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_hash = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    if args.receipt.exists():
        raise SystemExit(f"acceptance receipt already exists: {args.receipt}")

    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "status": "armed",
        "armed_at": time.time(),
        "pid": args.pid,
        "service": args.service,
        "payload_hash": payload_hash,
        "content_sha256": hashlib.sha256(args.content.encode("utf-8")).hexdigest(),
    }
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    session = frida.get_local_device().attach(args.pid)
    source = AGENT.replace("__SERVICE__", args.service).replace(
        "__HELPER__", json.dumps(str(args.helper.resolve()))
    )
    script = session.create_script(source)
    try:
        script.load()
        result = int(script.exports_sync.sendonce(args.ccode, args.content, args.pcsource))
        receipt.update(status="invoked" if result == 0 else "rejected", result=result, invoked_at=time.time())
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(receipt, indent=2))
        return 0 if result == 0 else 1
    finally:
        try:
            script.unload()
        finally:
            session.detach()


if __name__ == "__main__":
    raise SystemExit(main())
