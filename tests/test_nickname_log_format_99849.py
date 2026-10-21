"""Qianniu 9.98.49N log-shape regressions.

The client stopped repeating the account segment inside the `[CHAT ...]`
bracket and started emitting a second event namespace (`im.imbamsg.*`) for
some seats. Both shapes silently disabled conversation-switch confirmation,
nickname learning and local-send receipts, so pin them here.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nickname_resolver as nr  # noqa: E402

# Real millisecond stamp captured from 9.98.49N; log prefixes and `now` must
# agree with it because notification_payload cross-checks the line timestamp.
SEND_TIME_MS = 1792605766480
SEND_TIME = SEND_TIME_MS / 1000.0
NOW = SEND_TIME + 1.0


def prefix(at: float = NOW) -> str:
    return f"[{time.strftime('%m-%d %H:%M:%S', time.localtime(at))} 14111406 19752 14744 INFO] app "


def chat(account: str, seat: str, repeated: bool, trailing_space: bool, at: float = NOW) -> str:
    body = f"{account}#3#{seat}"
    if repeated:
        body += f" {body}"
    if trailing_space:
        body += " "
    return f"{prefix(at)}[CHAT {body}]"


CONVERSATION_CHANGE = (
    "[onEventNotify][ strFullEvent=im.uiutil.onConversationChange,w_json_str="
    '{"cid":{"ccode":"2201250451713.1-126446588.1#11001@cntaobao",'
    '"targetId":"2201250451713"},"nick":"tb110649232"}]'
)

# Captured verbatim from app.log: a seat's own outbound message notification.
LOCAL_SEND_BODY = (
    '[{"cid":{"ccode":"2217298756354.1-11789284.1#11001@cntaobao","nick":"tb1238562800",'
    '"domain":"cntaobao","targetId":"2217298756354"},"bizType":"11001","newmsgs":'
    '[{"mcode":{"messageId":"","clientId":"7518733536770129962"},'
    '"sendTime":1792605766480,"fromId":{"nick":"sbpgklso","targetId":"11789284",'
    '"domain":"cntaobao"},"toId":{"nick":"tb1238562800","targetId":"2217298756354",'
    '"domain":"cntaobao"}}]}]'
)
LOCAL_SEND = f"[onEventNotify][ strEvent=im.singlemsg.onShopRobotLocalSendMsgs,jsonStr={LOCAL_SEND_BODY}]"


class ContextShapeTest(unittest.TestCase):
    def test_new_single_account_with_trailing_space(self):
        line = chat("联想官方旗舰店:花花", "2214931408880", False, True)
        match = nr.CONTEXT.search(line) or nr.CONTEXT_SINGLE.search(line)
        self.assertIsNotNone(match, "9.98.49N single-account shape must parse")
        self.assertEqual(match.group("account"), "联想官方旗舰店:花花")
        self.assertEqual(match.group("seat"), "2214931408880")

    def test_legacy_repeated_account_still_parses(self):
        line = chat("sbpgklso", "11789284", True, False)
        match = nr.CONTEXT.search(line)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("account"), "sbpgklso")

    def test_single_account_without_space_still_parses(self):
        line = chat("sbpgklso", "11789284", False, False)
        self.assertIsNotNone(nr.CONTEXT_SINGLE.search(line))

    def test_account_is_not_swallowed_by_optional_group(self):
        line = chat("联想官方旗舰店:花花", "2214931408880", False, True)
        match = nr.CONTEXT.search(line) or nr.CONTEXT_SINGLE.search(line)
        self.assertNotIn("#3#", match.group("account"))


class EventNamespaceTest(unittest.TestCase):
    def test_singlemsg_namespace(self):
        self.assertEqual(nr.EVENT.search(LOCAL_SEND).group(1), "im.singlemsg.onShopRobotLocalSendMsgs")

    def test_imbamsg_namespace_is_accepted(self):
        line = LOCAL_SEND.replace("im.singlemsg.", "im.imbamsg.")
        self.assertEqual(nr.EVENT.search(line).group(1), "im.imbamsg.onShopRobotLocalSendMsgs")

    def test_local_reply_records_accepts_both_namespaces(self):
        expected = [("sbpgklso", "2217298756354.1-11789284.1#11001@cntaobao", SEND_TIME)]
        for namespace in ("im.singlemsg.", "im.imbamsg."):
            line = (
                chat("sbpgklso", "11789284", False, True)
                + LOCAL_SEND.replace("im.singlemsg.", namespace)
            )
            with self.subTest(namespace=namespace):
                self.assertEqual(nr.local_reply_records(line, NOW), expected)

    def test_local_reply_records_stale_timestamp_is_dropped(self):
        line = chat("sbpgklso", "11789284", False, True) + LOCAL_SEND
        self.assertEqual(nr.local_reply_records(line, NOW + 86400 * 2), [])

    def test_local_reply_records_ignores_other_seat(self):
        line = chat("联想官方旗舰店:花花", "2214931408880", False, True) + LOCAL_SEND
        self.assertEqual(nr.local_reply_records(line, NOW), [])


class SelectionConfirmationTest(unittest.TestCase):
    """9.98.49N often omits `im.uiutil.onConversationChange`, so the
    `[APPCHAT ...][OpenConversation][...GetcCode()=...]` line must confirm too."""

    LINE = (
        "[10-22 03:18:13 18664796 17444 17488 WARNING] app "
        "[APPCHAT 联想官方旗舰店:花花#3#2214931408880 ][OpenConversation][ "
        "appConversationInfo.GetcCode()=2201250451713.1-126446588.1#11001@cntaobao,"
        "appConversationInfo.GetSnick()=cntaobaowangchenyu2660,"
        "GetCurrentConversation().GetcCode()=2201250451713.1-126446588.1#11001@cntaobao"
        " ][ChatService.cpp(162) OpenConversationBefore]\r"
    )

    def test_regex_extracts_account_and_ccode(self):
        match = nr.OPEN_CONVERSATION.search(self.LINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("account"), "联想官方旗舰店:花花")
        self.assertEqual(match.group("seat"), "2214931408880")
        self.assertEqual(match.group("ccode"), "2201250451713.1-126446588.1#11001@cntaobao")
        self.assertEqual(match.group("pid"), "17444")

    def test_watch_confirms_from_open_conversation_line(self):
        import tempfile

        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "app.log"
            log.write_bytes(chat("联想官方旗舰店:花花", "2214931408880", False, True).encode("utf-8")
                            + b"\r\n" + self.LINE.encode("utf-8") + b"\r\n")
            watch = nr.ConversationSelectionWatch([log], process_id=17444, recent_bytes=log.stat().st_size)
            watch.poll()
            self.assertEqual(watch.revisions.get("联想官方旗舰店:花花"), 1)
            self.assertEqual(watch.selected.get("联想官方旗舰店:花花"), "2201250451713.1-126446588.1#11001@cntaobao")

    def test_watch_ignores_other_process(self):
        import tempfile

        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "app.log"
            log.write_bytes(self.LINE.encode("utf-8") + b"\r\n")
            watch = nr.ConversationSelectionWatch([log], process_id=999, recent_bytes=log.stat().st_size)
            watch.poll()
            self.assertEqual(watch.selected, {})


class PayloadTest(unittest.TestCase):
    def test_conversation_change_payload_on_new_shape(self):
        line = chat("联想官方旗舰店:花花", "2214931408880", False, True) + CONVERSATION_CHANGE
        decoded = nr.notification_payload(line, NOW)
        self.assertIsNotNone(decoded)
        _context, payload = decoded
        self.assertEqual(payload["cid"]["ccode"], "2201250451713.1-126446588.1#11001@cntaobao")

    def test_register_lines_are_ignored(self):
        line = chat("sbpgklso", "11789284", False, True) + "[register event.][ browserID=id:1-2 ]"
        self.assertIsNone(nr.notification_payload(line, NOW))

    def test_ccode_regex_unchanged(self):
        self.assertIsNotNone(nr.CCODE.fullmatch("2201250451713.1-126446588.1#11001@cntaobao"))


if __name__ == "__main__":
    unittest.main()
