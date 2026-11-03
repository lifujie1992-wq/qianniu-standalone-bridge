"""大脑工位令牌被拒时的诊断（1.6.84）。

现场（2026-11-04，DESKTOP-FHQHG6C）：面板只有一句 `invalid agent token`，
分不清是令牌被重置、设备绑定不匹配、还是网络/代理，运维只能整包重装。
这组用例锁住三件事：

* 服务端 403 + `{"error":"invalid agent token"}` 必须被识别为"令牌问题"，
  并且给出可核对的令牌指纹（不是明文令牌）；
* 鉴权失败要进 /api/v1/status 的结构化字段，面板能看到"令牌被拒 / 处理建议"；
* 这类失败重试无意义，退避要拉长，不能每 2 秒刷一次同样的错误。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import brain_auth
import standalone_bridge as bridge


REAL_BODY = '{"error":"invalid agent token","ok":false}'
TOKEN = "8KOixvDwJhGMxRRDzerMlAAofTx7sg8sBn5jB8By8Pw"


def make_connector(**overrides):
    config_values = {
        "brain_enabled": True,
        "brain_server_url": "http://brain.test",
        "brain_agent_token": TOKEN,
        "brain_agent_id": "agent-1",
        "gateway_agent_id": "gateway-1",
        "gateway_agent_token": "local-gateway-token",
        "device_id": "device-1",
        "brain_ws_enabled": False,
    }
    config_values.update(overrides)
    config = bridge.Config(Path(tempfile.gettempdir()) / "brain-auth-test.json", config_values)
    db = SimpleNamespace(
        max_event_rowid=lambda: 0,
        enqueue_untracked_brain_events_after_rowid=lambda rowid: 0,
        brain_event_counts=lambda: {"pending": 0, "delivered": 0},
        brain_command_counts=lambda: {"in_flight": 0, "result_pending": 0, "acknowledged": 0},
    )
    app = SimpleNamespace(config=config, db=db, stop_event=threading.Event())
    return bridge.BrainConnector(app), app


def http_error(status: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://brain.test/api/bridge/v1/register", status, "err", {}, io.BytesIO(body.encode())
    )


class TokenFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_short_and_not_the_token(self):
        fingerprint = brain_auth.token_fingerprint(TOKEN)
        self.assertTrue(fingerprint.startswith("sha256:"))
        self.assertEqual(len(fingerprint), len("sha256:") + 10)
        self.assertNotIn(TOKEN[:8], fingerprint)

    def test_fingerprint_is_stable_and_blank_safe(self):
        self.assertEqual(
            brain_auth.token_fingerprint(TOKEN), brain_auth.token_fingerprint(TOKEN)
        )
        self.assertEqual(brain_auth.token_fingerprint(""), "")
        self.assertEqual(brain_auth.token_fingerprint(None), "")


class InvalidAgentTokenTests(unittest.TestCase):
    def test_403_invalid_agent_token_is_an_auth_failure(self):
        described = brain_auth.describe_brain_error(403, REAL_BODY, token=TOKEN)
        self.assertEqual(described["code"], brain_auth.INVALID_AGENT_TOKEN)
        self.assertTrue(described["auth"])
        self.assertFalse(described["retryable"])
        self.assertIn("工位令牌无效", described["message"])
        self.assertIn("重新生成", described["hint"])
        self.assertEqual(described["fingerprint"], brain_auth.token_fingerprint(TOKEN))

    def test_message_and_hint_are_joined_for_dialogs(self):
        text = brain_auth.format_brain_error(
            brain_auth.describe_brain_error(403, REAL_BODY, token=TOKEN)
        )
        self.assertIn("工位令牌无效", text)
        self.assertIn(brain_auth.token_fingerprint(TOKEN), text)

    def test_missing_token_is_called_out_explicitly(self):
        described = brain_auth.describe_brain_error(403, REAL_BODY, token="")
        self.assertIn("没有令牌", described["message"])
        self.assertIn("brain_agent_token", described["hint"])

    def test_nested_detail_shape_is_understood(self):
        body = '{"detail":{"error":"invalid agent token"}}'
        described = brain_auth.describe_brain_error(403, body, token=TOKEN)
        self.assertEqual(described["code"], brain_auth.INVALID_AGENT_TOKEN)

    def test_token_bound_elsewhere_is_distinct_from_unknown_token(self):
        # 现场 2026-11-04：大脑对"令牌不在库"回 43 字节 invalid agent token，
        # 对"令牌有效但绑在别的机器"回 219 字节中文 + token_bound_to_another_device。
        body = '{"error":"该令牌已在另一台电脑使用","code":"token_bound_to_another_device"}'
        described = brain_auth.describe_brain_error(403, body, token=TOKEN)
        self.assertEqual(described["code"], brain_auth.TOKEN_BOUND_ELSEWHERE)
        self.assertTrue(described["auth"])
        self.assertFalse(described["retryable"])
        self.assertIn("另一台电脑", described["message"])
        self.assertIn("解绑", described["hint"])

    def test_unknown_token_hint_points_at_the_other_environment(self):
        described = brain_auth.describe_brain_error(403, REAL_BODY, token=TOKEN)
        self.assertIn("不在这台大脑的库里", described["hint"])
        self.assertIn("令牌管理", described["hint"])

    def test_device_binding_stays_separate_from_token(self):
        described = brain_auth.describe_brain_error(
            403, '{"error":"device binding mismatch"}', token=TOKEN
        )
        self.assertEqual(described["code"], brain_auth.DEVICE_BINDING_MISMATCH)
        self.assertTrue(described["auth"])
        self.assertIn("设备绑定", described["message"])

    def test_plain_403_without_auth_words_is_not_auth(self):
        described = brain_auth.describe_brain_error(403, '{"error":"shop not allowed"}')
        self.assertEqual(described["code"], brain_auth.FORBIDDEN)
        self.assertFalse(described["auth"])

    def test_401_and_5xx_and_network(self):
        self.assertTrue(brain_auth.describe_brain_error(401, "")["auth"])
        server = brain_auth.describe_brain_error(502, "bad gateway")
        self.assertEqual(server["code"], brain_auth.SERVER_ERROR)
        self.assertTrue(server["retryable"])
        network = brain_auth.describe_brain_error(0, "connection refused")
        self.assertEqual(network["code"], brain_auth.NETWORK_ERROR)
        self.assertIn("无法连接大脑", network["message"])
        self.assertFalse(network["auth"])

    def test_ok_false_body_is_still_recognised(self):
        described = brain_auth.describe_brain_error(200, REAL_BODY, token=TOKEN)
        self.assertEqual(described["code"], brain_auth.INVALID_AGENT_TOKEN)
        self.assertTrue(described["auth"])


class ConnectorRequestErrorTests(unittest.TestCase):
    def test_http_403_becomes_structured_brain_request_error(self):
        connector, _app = make_connector()
        with patch.object(bridge.urllib.request, "urlopen", side_effect=http_error(403, REAL_BODY)):
            with self.assertRaises(bridge.BrainRequestError) as caught:
                connector.request("POST", "/api/bridge/v1/register", {})
        error = caught.exception
        self.assertEqual(error.status_code, 403)
        self.assertEqual(error.error_code, brain_auth.INVALID_AGENT_TOKEN)
        self.assertTrue(error.auth_rejected)
        self.assertEqual(error.fingerprint, brain_auth.token_fingerprint(TOKEN))
        self.assertIn("工位令牌无效", str(error))

    def test_note_failure_publishes_structured_status(self):
        connector, _app = make_connector()
        with patch.object(bridge.urllib.request, "urlopen", side_effect=http_error(403, REAL_BODY)):
            with self.assertRaises(bridge.BrainRequestError) as caught:
                connector.request("POST", "/api/bridge/v1/register", {})
        connector.note_failure("brain", caught.exception)
        status = connector.status()
        self.assertEqual(status["state"], "degraded")
        self.assertEqual(status["last_error_code"], brain_auth.INVALID_AGENT_TOKEN)
        self.assertEqual(status["last_error_status"], 403)
        self.assertTrue(status["auth_rejected"])
        self.assertTrue(status["needs_operator"])
        self.assertIn("重新生成", status["last_error_hint"])
        self.assertEqual(status["last_error_fingerprint"], brain_auth.token_fingerprint(TOKEN))

    def test_local_errors_are_not_reported_as_network_failures(self):
        connector, _app = make_connector()
        connector.note_failure("events", RuntimeError("sqlite3.OperationalError: disk I/O error"))
        status = connector.status()
        self.assertEqual(status["last_error_code"], "internal")
        self.assertIn("disk I/O error", status["last_error"])
        self.assertNotIn("无法连接大脑", status["last_error"])

    def test_network_errors_still_map_to_the_network_hint(self):
        connector, _app = make_connector()
        connector.note_failure("brain", OSError("connection refused"))
        status = connector.status()
        self.assertEqual(status["last_error_code"], brain_auth.NETWORK_ERROR)
        self.assertIn("无法连接大脑", status["last_error"])

    def test_auth_failures_are_recorded_once_a_minute(self):
        connector, _app = make_connector()
        error = bridge.BrainRequestError(403, REAL_BODY, token=TOKEN)
        connector.note_failure("brain", error)
        connector.note_failure("brain", error)
        connector.note_failure("brain", error)
        errors = [row for row in connector.activity() if row["stage"] == "brain"]
        self.assertEqual(len(errors), 1)
        # 状态字段仍然是最新的，只是不重复刷时间线。
        self.assertEqual(connector.status()["last_error_code"], brain_auth.INVALID_AGENT_TOKEN)

    def test_success_clears_the_failure_record(self):
        connector, _app = make_connector()
        connector.note_failure("brain", bridge.BrainRequestError(403, REAL_BODY, token=TOKEN))
        connector.clear_failure()
        status = connector.status()
        self.assertEqual(status["last_error"], "")
        self.assertEqual(status["last_error_code"], "")
        self.assertFalse(status["auth_rejected"])

    def test_register_success_clears_previous_rejection(self):
        connector, _app = make_connector()
        connector.note_failure("brain", bridge.BrainRequestError(403, REAL_BODY, token=TOKEN))
        with patch.object(connector, "request", return_value={"ok": True}):
            connector.register()
        status = connector.status()
        self.assertTrue(status["registered"])
        self.assertEqual(status["state"], "online")
        self.assertFalse(status["auth_rejected"])
        self.assertEqual(status["last_error"], "")

    def test_auth_rejection_backs_off_harder_than_other_errors(self):
        connector, _app = make_connector()
        connector.auth_rejected = True
        self.assertGreaterEqual(connector.failure_backoff(15.0), 30.0)
        self.assertLessEqual(connector.failure_backoff(1000.0), 60.0)
        connector.auth_rejected = False
        self.assertLessEqual(connector.failure_backoff(1000.0), 15.0)


class ConnectorConfigurationTests(unittest.TestCase):
    def test_agent_id_source_prefers_the_brain_agent_id(self):
        connector, _app = make_connector()
        self.assertEqual(connector.agent_id_source(), "brain_agent_id")
        self.assertEqual(connector.status()["agent_id_source"], "brain_agent_id")

    def test_fallback_agent_id_is_reported_and_warned(self):
        connector, _app = make_connector(brain_agent_id="")
        self.assertEqual(connector.agent_id_source(), "gateway_agent_id")
        warnings = connector.config_warnings()
        self.assertTrue(any("gateway_agent_id" in item for item in warnings))
        self.assertIn("config_warnings", connector.status())

    def test_gateway_token_pasted_as_brain_token_is_flagged(self):
        connector, _app = make_connector(brain_agent_token="local-gateway-token")
        self.assertEqual(connector.token_source(), "gateway_agent_token")
        warnings = connector.config_warnings()
        self.assertTrue(any("gateway_agent_token" in item for item in warnings))

    def test_status_exposes_token_fingerprint_without_the_token(self):
        connector, _app = make_connector()
        status = connector.status()
        self.assertEqual(status["token_fingerprint"], brain_auth.token_fingerprint(TOKEN))
        self.assertNotIn(TOKEN, json.dumps(status, ensure_ascii=False))
        self.assertEqual(status["token_source"], "brain_agent_token")


class WorkbenchMarkupTests(unittest.TestCase):
    """面板必须把指纹与处理建议渲染出来，否则结构化字段白存。"""

    def setUp(self) -> None:
        self.html = (ROOT / "workbench.html").read_text(encoding="utf-8")

    def test_brain_panel_has_fingerprint_and_hint_rows(self):
        self.assertIn('id="brainToken"', self.html)
        self.assertIn('id="brainAgentSource"', self.html)
        self.assertIn('id="brainFix"', self.html)

    def test_brain_panel_renders_structured_fields(self):
        self.assertIn("brain.token_fingerprint", self.html)
        self.assertIn("brain.auth_rejected", self.html)
        self.assertIn("brain.last_error_hint", self.html)
        self.assertIn("令牌被拒", self.html)


class ConfigDialogMessageTests(unittest.TestCase):
    def test_test_connection_names_the_rejected_token(self):
        import config_dialog

        opener = Mock()
        opener.open.side_effect = http_error(403, REAL_BODY)
        config = {
            "brain_server_url": "http://brain.test",
            "brain_agent_token": TOKEN,
            "brain_agent_id": "agent-1",
            "device_id": "device-1",
        }
        with patch.object(config_dialog, "direct_opener", return_value=opener):
            with self.assertRaises(RuntimeError) as caught:
                config_dialog.test_brain_connection(config)
        message = str(caught.exception)
        self.assertIn("工位令牌无效", message)
        self.assertIn(brain_auth.token_fingerprint(TOKEN), message)


if __name__ == "__main__":
    unittest.main()
