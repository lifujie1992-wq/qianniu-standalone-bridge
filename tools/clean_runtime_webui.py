import argparse
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


CHAT_ENTRY = "web_chat-packer/recent.html"
SCRIPT_TAG = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</script>",
    re.IGNORECASE,
)
BRIDGE_MARKERS = (
    "data-qn-bridge",
    "127.0.0.1:41010",
    "127.0.0.1:41011",
    "__qn_bridge_v",
    "qn-bridge-inline-v",
)


def remove_old_bridge_scripts(html: str) -> tuple[str, int]:
    removed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal removed
        script = match.group(0).lower()
        if any(marker.lower() in script for marker in BRIDGE_MARKERS):
            removed += 1
            return ""
        return match.group(0)

    return SCRIPT_TAG.sub(replace, html), removed


def clean_zip(path: Path) -> bool:
    with zipfile.ZipFile(path, "r") as source:
        html = source.read(CHAT_ENTRY).decode("utf-8")
        cleaned, count = remove_old_bridge_scripts(html)
        if any(marker.lower() in cleaned.lower() for marker in BRIDGE_MARKERS):
            raise RuntimeError(f"old bridge marker survived in {path}")
        if count == 0:
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
                        data = cleaned.encode("utf-8")
                    target.writestr(item, data)
            source.close()
            backup = path.with_suffix(path.suffix + ".before-standalone-clean")
            if not backup.exists():
                shutil.copy2(path, backup)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    args = parser.parse_args()
    changed = 0
    for path in sorted(args.runtime.rglob("webui.zip")):
        changed += int(clean_zip(path))
    print(f"cleaned_webui_zips={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
