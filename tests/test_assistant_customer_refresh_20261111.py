import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class AssistantRefreshTests(unittest.TestCase):
    def test_context_and_independent_session_refresh(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const page=fs.readFileSync('workbench.html','utf8');
const source=page.slice(page.indexOf('    async function loadMessages('),page.indexOf('    async function openInQianniu('))+
 page.slice(page.indexOf('    async function refresh()'),page.indexOf('    function updateSendButton()'));
const requests=[],state={current:{account:'shop',buyer_id:'buyer'},messageLoadSequence:0,messages:[],status:{},busy:false};
let renders=0,lists=0;
const c={state,URLSearchParams,dockMode:false,keyOf:s=>s.account+'\0'+s.buyer_id,
 api:url=>new Promise(resolve=>requests.push({url,resolve})),renderMessages(){renders++},
 renderSessionMode(){},renderBridgeStatus(){},renderSessions(){lists++},renderEvidence(){},
 el:()=>({}),dotState(){},selectSession:async()=>{}};
vm.createContext(c);vm.runInContext(source,c);
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
(async()=>{
 let p=c.loadMessages();requests.shift().resolve({messages:[{event_id:'same',content:'图片',context:{}}]});await p;
 p=c.loadMessages();requests.shift().resolve({messages:[{event_id:'same',content:'图片',context:{order_id:'new-order'}}]});await p;
 assert.equal(renders,2,'updated customer facts must render without waiting for another message');
 p=c.refresh();await flush();
 requests.find(r=>r.url==='/api/v1/sessions').resolve({sessions:[{account:'shop',buyer_id:'buyer',buyer_nick:'真实客户'}]});await flush();
 assert.equal(state.sessions[0].buyer_nick,'真实客户');assert.equal(lists,1,'status/diagnostics must not block customer rows');
 requests.find(r=>r.url.startsWith('/api/v1/session?')).resolve({messages:[]});
 requests.find(r=>r.url==='/api/v1/status').resolve({});requests.find(r=>r.url==='/api/v1/diagnostics').resolve({});
 await p;
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run(['node','-e',script],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
