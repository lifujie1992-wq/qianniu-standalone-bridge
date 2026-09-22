from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import launcher  # noqa: E402
import tray_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the source tray against an installed root")
    parser.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args()
    install_root = args.install_root.resolve()
    if not (install_root / "config.json").is_file():
        parser.error(f"config.json is missing from {install_root}")

    launcher.app_root = lambda: install_root
    return tray_app.run()


if __name__ == "__main__":
    raise SystemExit(main())
