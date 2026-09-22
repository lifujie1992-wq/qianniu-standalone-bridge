from __future__ import annotations

import argparse
import hashlib
import json
import threading

import frida


AGENT = r"""
'use strict';

const wantedLength = __WANTED_LENGTH__;
const patterns = ['2? 00', '3? 00', '4? 00', '5? 00', '6? 00', '7? 00'];
const seen = new Set();

function inspectCandidate(endAddress) {
  const start = endAddress.sub(wantedLength - 1);
  const key = start.toString();
  if (seen.has(key)) return;
  seen.add(key);
  try {
    const bytes = new Uint8Array(start.readByteArray(wantedLength));
    for (let index = 0; index < bytes.length; index += 1) {
      const value = bytes[index];
      if (value === 0 || (value < 0x20 && value !== 0x09 && value !== 0x0a && value !== 0x0d)) {
        return;
      }
    }
    send({event: 'candidate', address: key}, bytes.buffer);
  } catch (_) {}
}

rpc.exports = {
  scan() {
    let matches = 0;
    for (const range of Process.enumerateRanges({protection: 'rw-', coalesce: true})) {
      for (const pattern of patterns) {
        let found = [];
        try {
          found = Memory.scanSync(range.base, range.size, pattern);
        } catch (_) {
          continue;
        }
        for (const match of found) {
          inspectCandidate(match.address);
          matches += 1;
        }
      }
    }
    send({event: 'complete', scanned_end_markers: matches});
    return matches;
  },
};
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover an exact-length UTF-8 buffer from a process by SHA-256."
    )
    parser.add_argument("pid", type=int)
    parser.add_argument("length", type=int)
    parser.add_argument("sha256")
    args = parser.parse_args()

    wanted_hash = args.sha256.lower()
    completed = threading.Event()
    result: dict[str, object] = {"found": False, "scanned_end_markers": 0}

    session = frida.get_local_device().attach(args.pid)
    source = AGENT.replace("__WANTED_LENGTH__", str(args.length))
    script = session.create_script(source)

    def on_message(message: dict[str, object], data: bytes | None) -> None:
        if message.get("type") != "send":
            return
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        event = payload.get("event")
        if event == "candidate" and data is not None:
            if hashlib.sha256(data).hexdigest() == wanted_hash:
                result.update(
                    found=True,
                    address=payload.get("address"),
                    utf8=data.decode("utf-8"),
                )
        elif event == "complete":
            result["scanned_end_markers"] = int(payload.get("scanned_end_markers") or 0)
            completed.set()

    script.on("message", on_message)
    script.load()
    try:
        script.exports_sync.scan()
        completed.wait(5.0)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["found"] else 1
    finally:
        script.unload()
        session.detach()


if __name__ == "__main__":
    raise SystemExit(main())
