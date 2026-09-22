"""Guard against Qianniu upgrading itself out of the bridge's support.

Qianniu upgrades in place: it stages replacement executables under runtime\\new,
unpacks the new client build next to the old one, and ships both an untouched
webui.zip and an AppBiz.dll whose image size needs its own Frida profile. Each
of those breaks something silently - a bare webui.zip means no inbound events,
a missing AppBiz profile means text send dies - so the bridge inspects the
runtime at startup and on a slow timer, re-injects any webui.zip that lost the
bridge, and surfaces the result in its status payload.

Support is read from appbiz_agent.js, so this can never drift from the profiles
the agent actually uses.
"""

from __future__ import annotations

import logging
import re
import struct
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

LOG = logging.getLogger("qianniu_standalone.client_support")

AGENT_PROFILE_RE = re.compile(
    r"(0x[0-9a-fA-F]+)\s*:\s*\{[^}]*?version\s*:\s*'([^']+)'[^}]*?serviceVtable\s*:\s*(0x[0-9a-fA-F]+)",
    re.S,
)
INJECTION_MARKER = "data-qn-standalone-bridge"
INJECTION_INSTALLED = "__qn_standalone_bridge_v1_installed"
DEFAULT_INTERVAL_SECONDS = 600.0


def size_of_image(path: Path) -> int:
    with path.open("rb") as handle:
        header = handle.read(0x40)
        if len(header) < 0x40 or header[:2] != b"MZ":
            raise ValueError("not a PE image")
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        handle.seek(pe_offset + 0x18 + 0x38)
        return struct.unpack("<I", handle.read(4))[0]


def agent_profiles(root: Path) -> dict[int, str]:
    profile_source = Path(root) / "appbiz_agent.js"
    if not profile_source.is_file():
        return {}
    source = profile_source.read_text(encoding="utf-8", errors="replace")
    return {
        int(size, 16): version
        for size, version, _vtable in AGENT_PROFILE_RE.findall(source)
    }


def launcher_version(runtime: Path) -> str:
    ini = runtime / "AliWorkbench.ini"
    if not ini.is_file():
        return ""
    for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.lower().startswith("version="):
            return line.split("=", 1)[1].strip()
    return ""


def webui_state(archive: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(archive) as package:
            try:
                html = package.read("web_chat-packer/recent.html").decode("utf-8", "replace")
            except KeyError:
                return {"path": str(archive), "ok": False, "reason": "recent.html missing"}
    except (OSError, zipfile.BadZipFile) as error:
        return {"path": str(archive), "ok": False, "reason": f"unreadable: {error}"}
    return {
        "path": str(archive),
        "ok": INJECTION_MARKER in html,
        "installed_marker": INJECTION_INSTALLED in html,
    }


def inspect(root: Path) -> dict[str, Any]:
    """Report the support state of every client build present under runtime\\."""
    runtime = Path(root) / "runtime"
    if not runtime.is_dir():
        raise FileNotFoundError(f"runtime directory not found: {runtime}")

    profiles = agent_profiles(root)
    report: dict[str, Any] = {
        "launcher_version": launcher_version(runtime),
        "profiles": {f"0x{size:x}": version for size, version in sorted(profiles.items())},
        "builds": [],
        "staged_upgrade": [],
        "problems": [],
    }

    for entry in sorted(runtime.iterdir()):
        if not entry.is_dir():
            continue
        appbiz = entry / "AppBiz.dll"
        if not appbiz.is_file():
            continue
        try:
            size = size_of_image(appbiz)
        except (OSError, ValueError) as error:
            report["problems"].append(f"{entry.name}: cannot read AppBiz.dll ({error})")
            continue
        version = profiles.get(size)
        states = [webui_state(archive) for archive in sorted(entry.rglob("webui.zip"))]
        report["builds"].append({
            "name": entry.name,
            "size_of_image": f"0x{size:x}",
            "profile_version": version,
            "send_supported": version is not None,
            "webui": states,
        })
        if version is None:
            report["problems"].append(
                f"{entry.name}: no AppBiz profile for image size 0x{size:x} (text send would fail)"
            )
        for state in states:
            if not state["ok"]:
                report["problems"].append(f"{entry.name}: webui.zip not injected ({state['path']})")

    staged = runtime / "new"
    if staged.is_dir():
        staged_names = sorted(
            item.name for item in staged.iterdir()
            if item.is_file() and item.name.lower().startswith("new_")
        )
        if staged_names:
            report["staged_upgrade"] = staged_names
            report["problems"].append(
                "runtime\\new holds staged client executables: Qianniu replaces the client on next launch"
            )

    active = report["launcher_version"]
    if active:
        matching = [build for build in report["builds"] if build["name"] == active]
        if matching and not matching[0]["send_supported"]:
            report["problems"].append(
                f"active build {active} has no AppBiz profile: text send would fail"
            )
        elif not matching:
            report["problems"].append(
                f"AliWorkbench.ini points at {active}, which is not present under runtime"
            )
    return report


def gpu_rendering_state() -> dict[str, Any]:
    """Report whether the client's GPU process fell back to software rendering.

    Qianniu runs CEF, and when no usable GPU is present (virtual display,
    missing driver, virtualised desktop) the GPU process silently switches to
    swiftshader: the whole UI is then drawn on the CPU, which customers feel as
    "Qianniu is slow" long before any bridge metric moves.
    """
    state: dict[str, Any] = {
        "checked": False,
        "software_rendering": False,
        "gpu_processes": 0,
        "cpu_seconds": 0.0,
        "angle_backend": "",
        "error": "",
    }
    try:
        import psutil  # local import: the file-level checker must stay dependency-free
    except ImportError:
        state["error"] = "psutil unavailable"
        return state
    try:
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if str(process.info.get("name") or "").lower() != "alirender.exe":
                    continue
                command = " ".join(process.info.get("cmdline") or [])
                if "--type=gpu-process" not in command:
                    continue
                state["checked"] = True
                state["gpu_processes"] += 1
                if "swiftshader" in command.lower():
                    state["software_rendering"] = True
                for token in command.split():
                    if token.startswith("--use-angle="):
                        state["angle_backend"] = token.split("=", 1)[1]
                try:
                    times = process.cpu_times()
                    state["cpu_seconds"] += float(times.user) + float(times.system)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as error:  # noqa: BLE001 - diagnostics must never raise
        state["error"] = str(error)[:200]
    state["cpu_seconds"] = round(state["cpu_seconds"], 1)
    return state


class ClientSupportWatcher(threading.Thread):
    """Periodically inspect the runtime and re-inject any lost webui bridge."""

    def __init__(
        self,
        root: Path,
        config: Any,
        stop_event: threading.Event,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        allow_injection: bool = True,
    ) -> None:
        super().__init__(name="client-support", daemon=True)
        self.root = Path(root)
        self.config = config
        self.stop_event = stop_event
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.allow_injection = allow_injection
        self.report: dict[str, Any] = {}
        self.reinjections = 0
        self.last_checked_at = 0.0
        self.last_error = ""
        self._gpu_warned = False

    def refresh(self) -> dict[str, Any]:
        try:
            report = inspect(self.root)
        except (OSError, ValueError) as error:
            self.last_error = str(error)
            LOG.warning("client support inspection failed: %s", error)
            return self.report

        missing = [
            state["path"]
            for build in report["builds"]
            for state in build["webui"]
            if not state["ok"]
        ]
        if missing and self.allow_injection:
            self.reinject(missing)
            try:
                report = inspect(self.root)
            except (OSError, ValueError) as error:
                self.last_error = str(error)
                LOG.warning("client support re-inspection failed: %s", error)

        self.report = report
        self.last_checked_at = time.time()
        self.last_error = ""
        for problem in report["problems"]:
            LOG.warning("client support: %s", problem)
        if not report["problems"]:
            LOG.info("client support: %d build(s) supported and injected", len(report["builds"]))
        return self.report

    def reinject(self, missing: list[str]) -> None:
        try:
            import launcher

            raw = getattr(self.config, "raw", None)
            if raw is None and isinstance(self.config, dict):
                raw = self.config
            if not raw:
                raise RuntimeError("config payload unavailable for re-injection")
            launcher.inject_all_webui(raw)
            self.reinjections += 1
            LOG.warning("client support: re-injected bridge into %d webui.zip", len(missing))
        except Exception as error:  # noqa: BLE001 - a guard must never kill the bridge
            self.last_error = str(error)
            LOG.error("client support: re-injection failed: %s", error)

    def status(self) -> dict[str, Any]:
        # Read the launcher ini live: a client upgrade can flip the active build
        # between two watcher passes, and a stale active build would delay the
        # very warning this guard exists to raise.
        active = launcher_version(self.root / "runtime") or self.report.get("launcher_version", "")
        supported = {
            build["name"]: bool(build["send_supported"])
            for build in self.report.get("builds", [])
        }
        gpu = gpu_rendering_state()
        if gpu["software_rendering"] and not self._gpu_warned:
            self._gpu_warned = True
            LOG.warning(
                "client GPU process is rendering in software (angle=%s, %s gpu process(es), %.1fs cpu): "
                "UI drawing runs on the CPU, which reads as 'Qianniu is slow'",
                gpu["angle_backend"] or "unknown", gpu["gpu_processes"], gpu["cpu_seconds"],
            )
        return {
            "last_checked_at": self.last_checked_at,
            "reinjections": self.reinjections,
            "last_error": self.last_error,
            "active_build": active,
            "active_build_supported": supported.get(active, None),
            "gpu": gpu,
            "profiles": self.report.get("profiles", {}),
            "builds": [
                {
                    "name": build["name"],
                    "size_of_image": build["size_of_image"],
                    "profile_version": build["profile_version"],
                    "send_supported": build["send_supported"],
                    "webui_injected": all(state["ok"] for state in build["webui"]),
                }
                for build in self.report.get("builds", [])
            ],
            "staged_upgrade": self.report.get("staged_upgrade", []),
            "problems": self.report.get("problems", []),
        }

    def run(self) -> None:
        self.refresh()
        while not self.stop_event.wait(self.interval_seconds):
            self.refresh()
