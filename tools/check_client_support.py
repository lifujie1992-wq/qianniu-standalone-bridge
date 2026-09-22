"""Check whether the packaged Qianniu runtime is still fully supported.

Thin CLI wrapper around client_support.inspect(), so the report a technician
sees here is byte-for-byte the same one the running bridge exposes in its
status payload.

Support is read from appbiz_agent.js itself, so this cannot drift from the
profiles the Frida agent actually uses.

Usage:
    py -3.10 tools/check_client_support.py [--root <bridge root>] [--fix-inject] [--json out.json]

Exit codes: 0 = every present build is supported and injected, 1 = a problem
was found, 2 = the runtime layout could not be inspected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client_support import inspect  # noqa: E402


def fix_injection(root: Path) -> int:
    """Re-run the injector when any webui.zip is missing the bridge."""
    import launcher  # noqa: PLC0415 - only needed for the repair path

    config_path = root / "config.json"
    if not config_path.is_file():
        print(f"config missing: {config_path}", file=sys.stderr)
        return 2
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    try:
        launcher.inject_all_webui(config)
    except Exception as error:  # noqa: BLE001 - report, do not traceback
        print(f"injection failed: {error}", file=sys.stderr)
        return 2
    print("injection re-applied to every webui.zip under runtime\\")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Qianniu runtime support for the bridge")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--fix-inject", action="store_true", help="re-inject any webui.zip that lost the bridge")
    parser.add_argument("--json", type=Path, help="also write the raw report as JSON")
    args = parser.parse_args()

    root = args.root.resolve()
    try:
        report = inspect(root)
    except (FileNotFoundError, OSError) as error:
        print(f"inspection failed: {error}", file=sys.stderr)
        return 2

    print(f"runtime        : {root / 'runtime'}")
    print(f"active build   : {report['launcher_version'] or '(unknown)'}")
    print("agent profiles : " + ", ".join(f"{k}={v}" for k, v in report["profiles"].items()))
    print("\nbuilds")
    for build in report["builds"]:
        flag = "OK  " if build["send_supported"] else "FAIL"
        injected = all(state["ok"] for state in build["webui"])
        print(
            f"  {flag} {build['name']:<12} size={build['size_of_image']:<10} "
            f"profile={str(build['profile_version'] or '-'):<10} webui_injected={injected}"
        )
    if report["staged_upgrade"]:
        print("\nstaged upgrade : " + ", ".join(report["staged_upgrade"]))

    if report["problems"]:
        print("\nproblems")
        for problem in report["problems"]:
            print(f"  - {problem}")

    if args.fix_inject:
        code = fix_injection(root)
        if code == 0:
            report = inspect(root)
            print("\nafter re-injection: " + ("clean" if not report["problems"] else "still has problems"))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwritten: {args.json}")

    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
