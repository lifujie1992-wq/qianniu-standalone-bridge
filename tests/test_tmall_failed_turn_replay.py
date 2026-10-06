import json
import unittest
from pathlib import Path
try:
    from pdd_brain.taobao_message_contract import select_parent, verified_batch_parent, message_id
except ModuleNotFoundError:
    from taobao_message_contract import select_parent, verified_batch_parent, message_id

CASES=json.loads((Path(__file__).parent/'fixtures/tmall_failed_turns_sanitized.json').read_text())

class FailedTurnReplayTests(unittest.TestCase):
    pass

def replay(case):
    def test(self):
        parent,cohort=verified_batch_parent(case['events'],case['parent'],case['questions'])
        self.assertEqual(message_id(parent),case['expected_parent'])
        selected=select_parent(case['events'],parent,cohort)
        self.assertEqual(message_id(selected['msg_id']),case['expected_selected'])
        self.assertEqual(bool(selected.get('_parent_guard_superseded')),case['expected_superseded'])
    return test

for case in CASES:
    setattr(FailedTurnReplayTests,'test_'+case['name'].replace('-','_'),replay(case))
