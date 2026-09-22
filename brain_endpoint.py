"""Brain endpoint fixed at build time for customer installations.

A customer seat only ever talks to the one deployment this build ships for, so
the address is compiled in instead of being typed in during setup. The
``QN_BRAIN_SERVER_URL`` environment variable overrides it for local runs and
tests only.
"""

from __future__ import annotations

import os
from typing import Any

BRAIN_SERVER_URL = "http://47.107.138.228:18765"
ENV_OVERRIDE = "QN_BRAIN_SERVER_URL"
CONFIG_KEY = "brain_server_url"


def brain_server_url() -> str:
    """Return the fixed brain base URL without a trailing slash."""
    override = str(os.environ.get(ENV_OVERRIDE) or "").strip()
    return (override or BRAIN_SERVER_URL).rstrip("/")


def enforce(config: dict[str, Any]) -> bool:
    """Pin ``brain_server_url``; report whether the config actually changed."""
    expected = brain_server_url()
    if str(config.get(CONFIG_KEY) or "").strip() == expected:
        return False
    config[CONFIG_KEY] = expected
    return True
