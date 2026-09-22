"""PyInstaller entry point for the one-click Qianniu bridge package.

The same executable serves three roles:
  --role launcher   (default) start/stop/status orchestration
  --role bridge     run standalone_bridge.py
  --role dock       run docked_workbench.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def clear_role_pid(role: str) -> None:
    name = {"bridge": "standalone_bridge.pid", "dock": "docked_workbench.pid"}.get(role)
    if not name:
        return
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    pid_file = root / "state" / name
    try:
        if int(pid_file.read_text(encoding="ascii").strip()) == os.getpid():
            pid_file.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def main() -> int:
    argv = sys.argv[1:]
    role = "launcher"
    if argv and argv[0] == "--role" and len(argv) >= 2:
        role = argv[1]
        argv = argv[2:]
    sys.argv = [sys.argv[0]] + argv

    if role == "bridge":
        try:
            import standalone_bridge

            return standalone_bridge.main()
        finally:
            clear_role_pid(role)
    if role == "dock":
        try:
            import docked_workbench

            return docked_workbench.main()
        finally:
            clear_role_pid(role)
    if role == "launcher":
        if not argv:
            import tray_app

            return tray_app.run()
        import launcher

        return launcher.main()
    raise SystemExit(f"unknown role: {role}")


if __name__ == "__main__":
    raise SystemExit(main())
