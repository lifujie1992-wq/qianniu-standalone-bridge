"""Rejected uploads must not create nonexistent-session draft polling."""
import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from standalone_bridge import BrainConnector,StateDB
class EventAckTests(unittest.TestCase):
    def brain(self,directory,status,committed,retryable):
        brain=BrainConnector(SimpleNamespace(config={},db=StateDB(Path(directory)/'state.db')))
        brain.account_allowed=lambda account:True
        brain.event_channel=lambda:None
        brain.request=Mock(return_value={'ok':True,'event_acks':[{'event_id':'e1','status':status,
            'committed':committed,'retryable':retryable,'error':{'code':'invalid_message'}}]})
        brain.watch_draft=Mock()
        rows=[{'event_id':'e1','payload':{'event_id':'e1','msg_id':'p1','original_msg_id':'p1',
            'account':'sbpgklso','buyer_id':'buyer#1@cntaobao','role':'user','content':'hi'}}]
        return brain,rows
    def test_terminal_rejection_stops_retry_without_polling_missing_session(self):
        with tempfile.TemporaryDirectory() as d:
            brain,rows=self.brain(d,'rejected',False,False)
            self.assertEqual(brain.upload_events(rows),{'e1'})
            brain.watch_draft.assert_not_called()
            self.assertEqual(brain.last_event_response['business_rejected'],1)
            self.assertTrue(any(item['stage']=='event_rejected' for item in brain._activity))
    def test_persisted_event_keeps_immediate_draft_watch(self):
        with tempfile.TemporaryDirectory() as d:
            brain,rows=self.brain(d,'persisted',True,False)
            self.assertEqual(brain.upload_events(rows),{'e1'})
            brain.watch_draft.assert_called_once()
    def test_retryable_failure_is_not_acknowledged_or_polled(self):
        with tempfile.TemporaryDirectory() as d:
            brain,rows=self.brain(d,'error',False,True)
            self.assertEqual(brain.upload_events(rows),set())
            brain.watch_draft.assert_not_called()
