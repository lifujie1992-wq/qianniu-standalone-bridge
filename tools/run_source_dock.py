"""Run the docked reception panel from source against an installed root."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the source dock against an installed root")
    parser.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args()

    source_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source_root))
    import docked_workbench

    install_root = args.install_root.resolve()
    docked_workbench.ROOT = install_root
    sys.argv = [
        "docked_workbench.py",
        "--config",
        str(install_root / "config.json"),
    ]
    return docked_workbench.main()


if __name__ == "__main__":
    raise SystemExit(main())
