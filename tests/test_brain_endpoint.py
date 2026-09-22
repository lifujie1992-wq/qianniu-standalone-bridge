"""The brain address ships fixed: customer seats cannot point it elsewhere.

The value is pinned on every config load rather than once per defaults
revision, so a hand-edited ``config.json`` is corrected the next time the
bridge, the launcher or the config dialog reads it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import brain_endpoint  # noqa: E402
from brain_endpoint import BRAIN_SERVER_URL, CONFIG_KEY, brain_server_url, enforce  # noqa: E402
from config_defaults import (  # noqa: E402
    CONFIG_DEFAULTS_REVISION,
    CONFIG_DEFAULTS_REVISION_KEY,
    apply_operational_defaults,
)
from standalone_bridge import Config  # noqa: E402


REQUIRED_KEYS = {
    "ws_host": "127.0.0.1",
    "api_host": "127.0.0.1",
    "api_token": "api-token",
    "browser_token": "browser-token",
    "gateway_url": "http://127.0.0.1:18776",
    "workbench_host": "127.0.0.1",
    "workbench_port": 18776,
    "workbench_token": "workbench-token",
}


class BrainEndpointTests(unittest.TestCase):
    def setUp(self):
        # Exercise the shipped default whatever the ambient environment says;
        # tests that need an override patch it again on top of this.
        patcher = patch.dict(os.environ, {brain_endpoint.ENV_OVERRIDE: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_address_is_the_customer_deployment(self):
        self.assertEqual(BRAIN_SERVER_URL, "http://47.107.138.228:18765")
        self.assertEqual(brain_server_url(), BRAIN_SERVER_URL)

    def test_enforce_replaces_a_hand_edited_address(self):
        config = {CONFIG_KEY: "http://10.0.0.5:1234"}
        self.assertTrue(enforce(config))
        self.assertEqual(config[CONFIG_KEY], BRAIN_SERVER_URL)

    def test_enforce_is_idempotent(self):
        config = {CONFIG_KEY: BRAIN_SERVER_URL}
        self.assertFalse(enforce(config))
        self.assertEqual(config[CONFIG_KEY], BRAIN_SERVER_URL)

    def test_enforce_normalises_a_trailing_slash(self):
        config = {CONFIG_KEY: BRAIN_SERVER_URL + "/"}
        self.assertTrue(enforce(config))
        self.assertEqual(config[CONFIG_KEY], BRAIN_SERVER_URL)

    def test_enforce_drops_a_blank_address(self):
        config = {CONFIG_KEY: "   "}
        self.assertTrue(enforce(config))
        self.assertEqual(config[CONFIG_KEY], BRAIN_SERVER_URL)

    def test_environment_override_wins(self):
        with patch.dict(os.environ, {brain_endpoint.ENV_OVERRIDE: "http://127.0.0.1:9/"}):
            self.assertEqual(brain_server_url(), "http://127.0.0.1:9")
            config = {CONFIG_KEY: BRAIN_SERVER_URL}
            self.assertTrue(enforce(config))
            self.assertEqual(config[CONFIG_KEY], "http://127.0.0.1:9")

    def test_operational_defaults_pin_the_address_after_revision_catchup(self):
        # A customer config already at the current revision must still be
        # corrected, so the pin cannot live behind the early return.
        config = {CONFIG_DEFAULTS_REVISION_KEY: CONFIG_DEFAULTS_REVISION}
        config[CONFIG_KEY] = "http://example.invalid"
        self.assertTrue(apply_operational_defaults(config))
        self.assertEqual(config[CONFIG_KEY], BRAIN_SERVER_URL)

    def test_operational_defaults_reports_no_change_when_only_pinned(self):
        config = {CONFIG_DEFAULTS_REVISION_KEY: CONFIG_DEFAULTS_REVISION}
        config[CONFIG_KEY] = BRAIN_SERVER_URL
        self.assertFalse(apply_operational_defaults(config))

    def test_config_load_persists_the_pinned_address(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = dict(REQUIRED_KEYS)
            raw[CONFIG_KEY] = "http://example.invalid"
            path.write_text(json.dumps(raw), encoding="utf-8")

            loaded = Config.load(path)
            self.assertEqual(loaded.get(CONFIG_KEY), BRAIN_SERVER_URL)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))[CONFIG_KEY],
                BRAIN_SERVER_URL,
            )

    def test_example_config_ships_the_pinned_address(self):
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(example[CONFIG_KEY], BRAIN_SERVER_URL)

    def test_dialog_shows_the_address_read_only(self):
        source = (ROOT / "config_dialog.py").read_text(encoding="utf-8")
        self.assertIn("from brain_endpoint import brain_server_url", source)
        self.assertIn('self.brain_url_entry = entry', source)
        self.assertIn('entry.configure(state="readonly")', source)
        self.assertIn("value=brain_server_url()", source)


if __name__ == "__main__":
    unittest.main()
