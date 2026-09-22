from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bridge source against an installed data root")
    parser.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args()

    source_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source_root))
    import standalone_bridge

    install_root = args.install_root.resolve()
    standalone_bridge.ROOT = install_root
    sys.argv = [
        "standalone_bridge.py",
        "--config",
        str(install_root / "config.json"),
    ]
    return standalone_bridge.main()


if __name__ == "__main__":
    raise SystemExit(main())
