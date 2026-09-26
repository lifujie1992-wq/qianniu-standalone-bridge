import argparse
import json
import re
import tempfile
import urllib.parse
import zipfile
from pathlib import Path


CHAT_ENTRY = "web_chat-packer/recent.html"
INJECTION_TAG = "data-qn-standalone-bridge"
INJECTION_RE = re.compile(
    r'<script\b[^>]*\bdata-qn-standalone-bridge\s*=\s*["\'][^"\']*["\'][^>]*>'
    r"[\s\S]*?</script>",
    re.IGNORECASE,
)


def build_injection(config: dict, bridge_source: str) -> str:
    host = str(config.get("ws_host") or "")
    port = int(config.get("ws_port") or 0)
    token = str(config.get("browser_token") or "")
    if host != "127.0.0.1" or not 1 <= port <= 65535 or not token:
        raise ValueError("browser bridge requires a loopback host, valid port, and token")
    query = urllib.parse.urlencode({"token": token})
    ws_url = f"ws://{host}:{port}/?{query}"
    # Keep this in sync with launcher.build_injection: history_poll defaults to
    # off because GetNewMsg/PeekNewMsg advance Qianniu's own message cursor.
    def _int_option(key: str, default: int) -> int:
        try:
            return int(config.get(key, default))
        except (TypeError, ValueError):
            return default

    options = {
        "invoke_observer": bool(config.get("bridge_invoke_observer", True)),
        "ws_mirror": bool(config.get("bridge_ws_mirror", True)),
        "history_poll": bool(config.get("bridge_history_poll", False)),
        "discovery_poll": bool(config.get("bridge_discovery_poll", True)),
        "dom_scan_interval_ms": _int_option("bridge_passive_dom_ms", 5000),
        "cache_scan_interval_ms": _int_option("bridge_passive_cache_ms", 10000),
    }
    return (
        f'<script {INJECTION_TAG}="v1">\n'
        f"window.__qn_standalone_ws_url={json.dumps(ws_url)};\n"
        f"window.__qn_standalone_options={json.dumps(options, ensure_ascii=False)};\n"
        f"{bridge_source.rstrip()}\n"
        "</script>"
    )


def inject_zip(path: Path, injection: str) -> bool:
    with zipfile.ZipFile(path, "r") as source:
        original = source.read(CHAT_ENTRY).decode("utf-8")
        if original.count(INJECTION_TAG) == 1 and injection in original:
            return False
        cleaned = INJECTION_RE.sub("", original)
        if "</body>" not in cleaned.lower():
            raise RuntimeError(f"chat entry has no body end tag: {path}")
        html = re.sub(
            r"</body>",
            lambda _match: injection + "\n</body>",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        )
        if html.count(INJECTION_TAG) != 1:
            raise RuntimeError(f"standalone injection count is invalid: {path}")
        if html == original:
            return False

        with tempfile.NamedTemporaryFile(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(temporary_path, "w") as target:
                for item in source.infolist():
                    data = source.read(item.filename)
                    if item.filename.replace("\\", "/") == CHAT_ENTRY:
                        data = html.encode("utf-8")
                    target.writestr(item, data)
            source.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("bridge", type=Path)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    bridge_source = args.bridge.read_text(encoding="utf-8-sig")
    injection = build_injection(config, bridge_source)
    changed = 0
    archives = sorted(args.runtime.rglob("webui.zip"))
    if not archives:
        raise FileNotFoundError(f"no webui.zip found under {args.runtime}")
    for path in archives:
        changed += int(inject_zip(path, injection))
    print(f"standalone_webui_archives={len(archives)} changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
