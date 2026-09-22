"""One-time operational defaults for unattended customer installations."""

from __future__ import annotations

from typing import Any


CONFIG_DEFAULTS_REVISION = 3
CONFIG_DEFAULTS_REVISION_KEY = "config_defaults_revision"
LEGACY_WORKBENCH_PORT = 18767
DEFAULT_WORKBENCH_PORT = 18776
LEGACY_EVENT_DELAY_SECONDS = 2.5
DEFAULT_EVENT_DELAY_SECONDS = 0.2

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
    try:
        revision = int(config.get(CONFIG_DEFAULTS_REVISION_KEY) or 0)
    except (TypeError, ValueError):
        revision = 0
    if revision >= CONFIG_DEFAULTS_REVISION:
        return False

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

    config[CONFIG_DEFAULTS_REVISION_KEY] = CONFIG_DEFAULTS_REVISION
    return True
