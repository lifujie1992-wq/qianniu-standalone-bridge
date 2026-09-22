import argparse
import json
import time
from pathlib import Path

import frida
import psutil


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Attach-only qnmsg compatibility probe")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()

    plugin = (ROOT / "vendor" / "9.77.01_qnmsgplugin_x64.dll").resolve()
    source = (ROOT / "native_agent.js").read_text(encoding="utf-8")
    source = source.replace("__QN_PLUGIN_PATH__", json.dumps(str(plugin)))
    source = source.replace("__QN_ALLOW_EXISTING_PLUGIN__", "false")
    events = []

    session = None
    script = None
    probe_error = ""

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
        time.sleep(max(1.0, args.seconds))
        status = script.exports_sync.status()
    except Exception as error:
        status = {"ready": False}
        probe_error = f"{type(error).__name__}: {error}"
    finally:
        if script is not None:
            try:
                script.exports_sync.shutdown()
            except Exception:
                pass
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
        "status": status,
        "events": events,
        "probe_error": probe_error,
        "send_calls": 0,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if status.get("ready") and not probe_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
