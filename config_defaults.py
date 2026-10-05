"""One-time operational defaults for unattended customer installations."""

from __future__ import annotations

from typing import Any

from brain_endpoint import enforce as enforce_brain_endpoint


CONFIG_DEFAULTS_REVISION = 9
CONFIG_DEFAULTS_REVISION_KEY = "config_defaults_revision"
LEGACY_WORKBENCH_PORT = 18767
DEFAULT_WORKBENCH_PORT = 18776
LEGACY_EVENT_DELAY_SECONDS = 2.5
DEFAULT_EVENT_DELAY_SECONDS = 0.2
LEGACY_DOCK_WIDTH = 286
DEFAULT_DOCK_WIDTH = 300
DEFAULT_DOCK_HEIGHT = 720

# These are customer-facing capabilities that are expected to work out of the box.
OPERATIONAL_DEFAULTS: dict[str, bool] = {
    "brain_enabled": True,
    "brain_ai_reply_enabled": True,
    "brain_remote_open_chat_enabled": True,
    "brain_draft_sync_enabled": True,
    "context_enrich_enabled": True,
    "delivery_enabled": True,
    "send_enabled": True,
    "appbiz_send_adapter_enabled": True,
    "appbiz_send_abi_validated": True,
    "disable_updater_task": True,
    "auto_launch_qianniu": True,
    "dock_enabled": True,
}

# These are mutually exclusive implementation and conflict-safety switches, not
# customer features. Enabling them alongside the passive/AppBiz path is unsafe.
INTERNAL_SAFETY_DEFAULTS: dict[str, bool] = {
    "native_adapter_enabled": False,
    "cdp_injection_enabled": False,
    "allow_existing_foreign_plugin": False,
}


def apply_operational_defaults(config: dict[str, Any]) -> bool:
    # The brain address is pinned on every load, so a hand-edited value is
    # corrected as soon as the bridge, the launcher or the dialog reads it.
    changed = enforce_brain_endpoint(config)
    try:
        revision = int(config.get(CONFIG_DEFAULTS_REVISION_KEY) or 0)
    except (TypeError, ValueError):
        revision = 0
    if revision >= CONFIG_DEFAULTS_REVISION:
        return changed

    if revision < 1:
        config.update(OPERATIONAL_DEFAULTS)
        for key, value in INTERNAL_SAFETY_DEFAULTS.items():
            config.setdefault(key, value)

    # Revision 2 moves only the former default endpoint. Explicit customer
    # ports remain untouched.
    if revision < 2:
        try:
            old_default_port = int(config.get("workbench_port")) == LEGACY_WORKBENCH_PORT
        except (TypeError, ValueError):
            old_default_port = False
        if old_default_port:
            config["workbench_port"] = DEFAULT_WORKBENCH_PORT
        if str(config.get("gateway_url") or "").rstrip("/") == (
            f"http://127.0.0.1:{LEGACY_WORKBENCH_PORT}"
        ):
            config["gateway_url"] = f"http://127.0.0.1:{DEFAULT_WORKBENCH_PORT}"

    # Revision 3 shortens the legacy 2.5s brain event delay. Customers who
    # tuned the value themselves keep their own number.
    if revision < 3:
        try:
            legacy_delay = float(config.get("brain_event_delay_seconds")) == (
                LEGACY_EVENT_DELAY_SECONDS
            )
        except (TypeError, ValueError):
            legacy_delay = False
        if legacy_delay:
            config["brain_event_delay_seconds"] = DEFAULT_EVENT_DELAY_SECONDS

    # Revision 4 turns on the concurrent brain-event upload pool. A burst used
    # to be drained one HTTP round-trip at a time (tail latency 10-40s); the
    # pool claims batches atomically so several requests are in flight at once.
    # setdefault keeps any value a customer tuned on purpose.
    if revision < 4:
        config.setdefault("event_upload_concurrency", 4)

    # Revision 5 exposes the passive scan cadence. The in-page bridge defaults to
    # DOM 5s / local cache 10s (was a hard-coded 10s/30s) plus a miss-triggered
    # recovery scan, cutting inbound capture latency for sessions that the SDK
    # event does not cover.
    if revision < 5:
        config.setdefault("bridge_passive_dom_ms", 5000)
        config.setdefault("bridge_passive_cache_ms", 10000)

    # Revision 6 adds the read-only MTop context enrichment ported from 福客's
    # qn-hh-4.3.js: shop info (淘宝/天猫 判定) is on by default; history orders
    # cost an extra MTop call per inbound message, so they stay opt-in.
    if revision < 6:
        config.setdefault("context_enrich_shop_info", True)
        config.setdefault("context_enrich_history_orders", False)

    # Revision 7 turns on the persistent WS event channel to the brain. Events
    # used to leave only through the HTTP batch endpoint (a fresh round-trip per
    # batch); the WS path pushes each frame as soon as it is claimed, with the
    # HTTP path kept as an automatic fallback when the link is down.
    if revision < 7:
        config.setdefault("brain_ws_enabled", True)

    # Revision 8 turns the dock from a Qianniu-snapping寄生窗 into a normal
    # floating app window: it no longer follows/resizes with Qianniu, no longer
    # hides itself when another app is focused, and no longer forces topmost.
    # Re-enable it for installs that an earlier build switched off, and default
    # the behaviour to the new floating mode.
    if revision < 8:
        config["dock_enabled"] = True
        config.setdefault("dock_mode", "floating")

    # Revision 9 makes the dock look like 福客's app component: a frameless
    # 300-wide panel that follows Qianniu (position + height), stays out of the
    # taskbar and is not forced topmost. The stock Edge `--app` title bar is
    # stripped at runtime, so the panel reads as a native window instead of a
    # browser popup. Floating stays available via dock_mode="floating".
    if revision < 9:
        config["dock_enabled"] = True
        if str(config.get("dock_mode") or "").strip().lower() in ("", "floating"):
            config["dock_mode"] = "snap"
        try:
            if int(config.get("dock_width") or 0) == LEGACY_DOCK_WIDTH:
                config["dock_width"] = DEFAULT_DOCK_WIDTH
        except (TypeError, ValueError):
            pass
        config.setdefault("dock_frameless", True)
        config.setdefault("dock_skip_taskbar", True)
        config.setdefault("dock_follow_height", True)
        config.setdefault("dock_topmost", False)

    config[CONFIG_DEFAULTS_REVISION_KEY] = CONFIG_DEFAULTS_REVISION
    return True
