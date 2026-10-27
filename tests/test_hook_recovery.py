"""任务 B（1.6.29）单测：阶段流转、提醒限频、夜间静默、恢复关闭、授权二次确认。"""

from __future__ import annotations

import unittest

import hook_recovery as hr


class FakeConfig:
    def __init__(self, values=None):
        self.values = {
            "imsdk_force_hook_enabled": True,
            "imsdk_hook_grace_seconds": 60,
            "imsdk_hook_remind_interval_minutes": 15,
            "imsdk_hook_quiet_when_no_buyer_minutes": 30,
        }
        if values:
            self.values.update(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeBrowser:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.expressions = []
        self.probe_result = {"frames": [], "count": 0}
        self.rehook_result = True

    def diagnostics_by_connection(self):
        return list(self.rows)

    def execute(self, expression, timeout=3.0, **kwargs):
        self.expressions.append(expression)
        if "window.__qn_standalone_rehook" in expression:
            return self.rehook_result
        return self.probe_result


class FakeDB:
    def __init__(self, active=True):
        self.active = active

    def has_recent_buyer_activity(self, minutes):
        return self.active


class FakeApp:
    def __init__(self, rows=None, config=None, db_active=True, cdp=None):
        self.browser = FakeBrowser(rows)
        self.config = FakeConfig(config)
        self.db = FakeDB(db_active)
        self.stop_event = None
        self.cdp = cdp


def chat_row(hooked=False, kind="chat_recent"):
    return {"page_kind": kind, "imsdk_hooked": hooked, "last_hook_error": "" if hooked else "imsdk_undefined"}


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make(app, clock, prompt=None, notify=None):
    # 单测里绝不开真窗口：默认用桩函数
    prompt = prompt or (lambda payload: hr.ANSWER_IGNORED)
    notify = notify or (lambda *args: None)
    return hr.HookRecovery(app, now_fn=clock, prompt_fn=prompt, notify_fn=notify, logger=None)


class StageFlowTests(unittest.TestCase):
    def test_grace_period_observes_only(self):
        clock = Clock()
        app = FakeApp([chat_row(False)])
        recovery = make(app, clock)
        clock.advance(30)
        recovery.tick()
        self.assertEqual(recovery.stage, hr.STAGE_OBSERVE)
        self.assertEqual(app.browser.expressions, [])

    def test_retry_probes_frames_and_rehooks(self):
        clock = Clock()
        app = FakeApp([chat_row(False)])
        app.browser.probe_result = {"frames": [
            {"frame": 0, "is_top": True, "url": "https://qn/web_chat-packer/recent.html?x=1",
             "imsdk_typeof": "undefined", "imsdk_invoke_typeof": "n/a"},
            {"frame": 1, "is_top": False, "url": "https://qn/web_chat-packer/child.html",
             "imsdk_typeof": "object", "imsdk_invoke_typeof": "function"},
        ]}
        recovery = make(app, clock)
        recovery.snooze_until = clock.now + 10**6  # 只验证阶段 1，先抑制提醒
        clock.advance(61)
        recovery.tick()
        self.assertEqual(recovery.stage, hr.STAGE_RETRY)
        self.assertTrue(recovery.child_frame_with_imsdk)
        self.assertEqual(len(app.browser.expressions), 2)
        self.assertEqual(recovery.last_action, "rehook")
        # URL 已脱敏
        self.assertEqual(recovery.frames_seen[0]["page_url"], "https://qn/web_chat-packer/recent.html")

    def test_retry_is_rate_limited_to_30s(self):
        clock = Clock()
        app = FakeApp([chat_row(False)])
        recovery = make(app, clock)
        clock.advance(61)
        recovery.tick()
        first = len(app.browser.expressions)
        clock.advance(5)
        recovery.tick()
        self.assertEqual(len(app.browser.expressions), first)
        clock.advance(30)
        recovery.tick()
        self.assertGreater(len(app.browser.expressions), first)

    def test_recovered_stops_everything(self):
        clock = Clock()
        app = FakeApp([chat_row(True)])
        recovery = make(app, clock)
        recovery.prompts_shown = 2
        recovery.needs_human = True
        clock.advance(120)
        recovery.tick()
        self.assertTrue(recovery.recovered)
        self.assertEqual(recovery.stage, hr.STAGE_OBSERVE)
        self.assertFalse(recovery.needs_human)
        self.assertEqual(recovery.last_action, "recovered")

    def test_stage_two_marks_no_safe_open_method(self):
        clock = Clock()
        app = FakeApp([chat_row(False, kind="other")])
        recovery = make(app, clock)
        recovery.snooze_until = clock.now + 10**6  # 先压住提醒，验证阶段 2 的结果
        clock.advance(61 + 121)
        recovery.tick()
        self.assertTrue(recovery.stage2_attempted)
        self.assertEqual(recovery.last_action_result, "no_safe_open_method")

    def test_disabled_does_nothing(self):
        clock = Clock()
        app = FakeApp([chat_row(False)], config={"imsdk_force_hook_enabled": False})
        recovery = make(app, clock)
        clock.advance(600)
        recovery.tick()
        self.assertEqual(recovery.stage, hr.STAGE_OBSERVE)
        self.assertFalse(recovery.enabled)
        self.assertEqual(app.browser.expressions, [])


class PromptTests(unittest.TestCase):
    def test_prompt_throttled_15min_and_snooze(self):
        clock = Clock()
        answers = []

        def prompt(payload):
            answers.append(payload)
            return hr.ANSWER_IGNORED

        app = FakeApp([chat_row(False)])
        recovery = make(app, clock, prompt=prompt, notify=lambda *a: None)
        clock.advance(61)
        recovery.tick()
        recovery._prompt_thread.join(timeout=5)
        self.assertEqual(recovery.prompts_shown, 1)

        clock.advance(60)
        recovery.tick()
        recovery._prompt_thread.join(timeout=5)
        self.assertEqual(recovery.prompts_shown, 1, "15 分钟内不应重复提醒")

        clock.advance(15 * 60)
        recovery.tick()
        recovery._prompt_thread.join(timeout=5)
        self.assertEqual(recovery.prompts_shown, 2)

        recovery.apply_answer(hr.ANSWER_SNOOZE, confirmed=True)
        self.assertGreater(recovery.snooze_until, clock.now)
        clock.advance(60)
        recovery.tick()
        recovery._prompt_thread.join(timeout=5)
        self.assertEqual(recovery.prompts_shown, 2, "snooze 期间不弹")

    def test_quiet_mode_suppresses_prompt_but_sets_needs_human(self):
        clock = Clock()
        app = FakeApp([chat_row(False)], db_active=False)
        recovery = make(app, clock, prompt=lambda payload: hr.ANSWER_IGNORED,
                        notify=lambda *a: None)
        clock.advance(61)
        recovery.tick()
        self.assertEqual(recovery.prompts_shown, 0)
        self.assertTrue(recovery.needs_human)
        self.assertEqual(recovery.last_action_result, "quiet: no buyer activity")

    def test_downgrade_to_hourly_after_three_prompts(self):
        clock = Clock()
        app = FakeApp([chat_row(False)])
        recovery = make(app, clock, prompt=lambda payload: hr.ANSWER_IGNORED,
                        notify=lambda *a: None)
        recovery.prompts_shown = 3
        recovery.last_prompt_at = clock.now
        clock.advance(16 * 60)
        self.assertFalse(recovery.should_prompt(), "3 次后应降频到 1 小时")
        clock.advance(45 * 60)
        self.assertTrue(recovery.should_prompt())

    def test_authorize_requires_confirmation(self):
        clock = Clock()
        app = FakeApp([chat_row(False)])
        recovery = make(app, clock)
        recovery.apply_answer(hr.ANSWER_AUTHORIZE, confirmed=False)
        self.assertEqual(recovery.last_action, "authorize_rejected")
        self.assertEqual(recovery.human_action, "none")
        self.assertEqual(recovery.stage, hr.STAGE_PROMPT if recovery.prompts_shown else hr.STAGE_OBSERVE)

    def test_authorize_attempts_once_and_reports_unsupported_restart(self):
        clock = Clock()

        class Cdp:
            def __init__(self):
                self.calls = 0

            def inject_once(self):
                self.calls += 1
                return 0

        cdp = Cdp()
        app = FakeApp([chat_row(False)], cdp=cdp)
        recovery = make(app, clock)
        result = recovery.authorize_repair()
        self.assertFalse(result["ok"])
        self.assertIn("cdp_unavailable", recovery.last_action_result)
        self.assertEqual(cdp.calls, 1)
        # 第二次不再自动重试
        again = recovery.authorize_repair()
        self.assertEqual(again["error"], "already_attempted")
        self.assertEqual(cdp.calls, 1)


class StatusTests(unittest.TestCase):
    def test_status_fields_match_task_contract(self):
        clock = Clock()
        app = FakeApp([chat_row(False)])
        recovery = make(app, clock)
        clock.advance(61)
        recovery.tick()
        status = recovery.status()
        for key in (
            "enabled", "stage", "recovered", "started_at_ms", "last_action",
            "last_action_result", "needs_human", "human_action", "prompts_shown",
            "last_prompt_at_ms", "last_prompt_answer", "cdp_authorized_by_user_at_ms",
        ):
            self.assertIn(key, status)
        self.assertLessEqual(len(status["last_action_result"]), 200)

    def test_hint_only_when_needs_human(self):
        clock = Clock()
        app = FakeApp([chat_row(False)])
        recovery = make(app, clock)
        self.assertEqual(recovery.hint(), "")
        clock.advance(61)
        recovery.tick()
        self.assertIn("接待台", recovery.hint())


if __name__ == "__main__":
    unittest.main()
