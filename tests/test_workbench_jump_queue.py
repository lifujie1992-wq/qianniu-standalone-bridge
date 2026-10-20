"""Execute the real workbench functions with controlled async native replies."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')

HARNESS = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const page = fs.readFileSync('workbench.html', 'utf8');
const functions = page.slice(page.indexOf('    async function loadMessages('),
                             page.indexOf('    function renderSessionMode('));
function setup(dock = true) {
  const requests = [], nodes = {}, notices = [];
  const state = {openingKey:'', openSequence:0, messageLoadSequence:0,
    openingPromise:null, pendingOpen:null, messages:[], current:null};
  const c = {state, dockMode:dock, innerWidth:1200, URLSearchParams,
    keyOf:s=>s.account+'\0'+s.buyer_id,
    el:id=>nodes[id] ||= {},
    api:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
    dockToast:(message,error)=>notices.push({message,error}),
    renderSessions(){}, updateSendButton(){}, renderMessages(){}, renderSessionMode(){}, renderEvidence(){}};
  vm.createContext(c); vm.runInContext(functions,c);
  return {...c,requests,nodes,notices};
}
const A={account:'shop',buyer_id:'uid-a',buyer_nick:'Alice'},
      B={account:'shop',buyer_id:'uid-b',buyer_nick:'Bob'},
      C={account:'shop',buyer_id:'uid-c',buyer_nick:'Carol'};
const flush = async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
const target = r=>JSON.parse(r.options.body).buyer_id;
async function latestClick() {
  const c=setup();
  const a=c.selectSession(A,true),b=c.selectSession(B,true),d=c.selectSession(C,true);
  assert.equal(c.requests.length,1); assert.equal(target(c.requests[0]),A.buyer_id);
  assert.equal(c.state.current,C); assert.equal(c.state.openingKey,c.keyOf(C));
  c.requests[0].resolve({verified:true}); await flush();
  assert.equal(c.requests.length,2); assert.equal(target(c.requests[1]),C.buyer_id);
  assert(!c.nodes.openState.textContent.includes('已在千牛打开 Alice'));
  c.requests[1].resolve({verified:true}); await Promise.all([a,b,d]);
  assert.equal(c.nodes.openState.textContent,'已在千牛打开 Carol');
  assert.equal(c.state.openingKey,''); assert.equal(c.state.openingPromise,null);
}
async function failedOlderRequest() {
  const c=setup(); const a=c.selectSession(A,true), b=c.selectSession(B,true);
  c.requests[0].reject(Error('old request failed')); await flush();
  assert.equal(c.requests.length,2); assert.equal(target(c.requests[1]),B.buyer_id);
  assert(!c.nodes.openState.textContent.includes('old request failed'));
  c.requests[1].resolve({verified:false}); await Promise.all([a,b]);
  assert(c.nodes.openState.textContent.includes('已请求打开 Bob'));
  assert(c.nodes.openState.textContent.includes('尚未确认'));
}
async function newestFailure() {
  const c=setup(); const a=c.openInQianniu(A), b=c.openInQianniu(B);
  c.requests[0].resolve({verified:true}); await flush();
  c.requests[1].reject(Error('new request failed')); await Promise.all([a,b]);
  assert.equal(c.nodes.openState.textContent,'new request failed');
  assert.equal(c.nodes.openState.className,'open-state error');
  assert.equal(c.state.openingKey,'');
}
async function staleMessages() {
  const c=setup(false);
  c.state.current=A; const a=c.loadMessages();
  c.state.current=B; const b=c.loadMessages();
  const bRows=[{event_id:'b-message'}],aRows=[{event_id:'a-message'}];
  c.requests[1].resolve({messages:bRows}); await b;
  c.requests[0].resolve({messages:aRows}); await a;
  assert.equal(c.state.messages,bRows);
  // A->B->A still needs a generation guard, not only identity equality.
  c.state.current=A; const old=c.loadMessages(), recent=c.loadMessages();
  const newest=[{event_id:'newest-a'}];
  c.requests[3].resolve({messages:newest}); await recent;
  c.requests[2].resolve({messages:aRows}); await old;
  assert.equal(c.state.messages,newest);
}
async function messageLatencyCannotReorderJumps() {
  const c=setup(false); const a=c.selectSession(A,true), b=c.selectSession(B,true);
  assert.equal(target(c.requests[0]),A.buyer_id);
  assert.equal(c.requests[1].url.startsWith('/api/v1/session?'),true);
  assert.equal(c.requests[2].url.startsWith('/api/v1/session?'),true);
  c.requests[0].resolve({verified:true}); await flush();
  assert.equal(target(c.requests[3]),B.buyer_id);
  c.requests[3].resolve({verified:true});
  c.requests[2].resolve({messages:[{event_id:'b'}]}); await flush();
  c.requests[1].resolve({messages:[{event_id:'a'}]}); await Promise.all([a,b]);
  assert.equal(c.state.current,B); assert.equal(c.state.messages[0].event_id,'b');
  assert.equal(c.nodes.openState.textContent,'已在千牛打开 Bob');
}
const tests={latestClick,failedOlderRequest,newestFailure,staleMessages,messageLatencyCannotReorderJumps};
(async()=>{await tests[process.argv[2]](); console.log('PASS '+process.argv[2]);})()
  .catch(error=>{console.error(error);process.exitCode=1;});
'''

@unittest.skipUnless(NODE, 'Node is required to execute workbench JavaScript')
class WorkbenchJumpQueueTests(unittest.TestCase):
    def run_case(self, name):
        result = subprocess.run([NODE, '-', name], input=HARNESS, cwd=ROOT,
                                text=True, encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_latest_click_runs_after_inflight_request(self): self.run_case('latestClick')
    def test_old_failure_does_not_hide_latest_target(self): self.run_case('failedOlderRequest')
    def test_latest_failure_remains_visible(self): self.run_case('newestFailure')
    def test_stale_messages_cannot_replace_selected_chat(self): self.run_case('staleMessages')
    def test_message_latency_cannot_reorder_jumps(self): self.run_case('messageLatencyCannotReorderJumps')

if __name__ == '__main__': unittest.main()
