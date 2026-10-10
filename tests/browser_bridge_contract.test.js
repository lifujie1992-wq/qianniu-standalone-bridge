'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function message(ccode, buyer, id, text) {
  return {
    cid: {ccode},
    fromid: {nick: buyer},
    toid: {nick: 'seller'},
    summary: text,
    mcode: {messageId: id},
    sendTime: Date.now(),
  };
}

function loadBridge(options = {}) {
  const handlers = new Map();
  const registrations = new Map();
  const sent = [];
  const calls = [];
  let invokeCalls = 0;
  let offCalls = 0;

  class FakeWebSocket {
    static OPEN = 1;
    static CONNECTING = 0;
    constructor() {
      this.readyState = FakeWebSocket.CONNECTING;
      FakeWebSocket.instance = this;
    }
    send(raw) { sent.push(JSON.parse(raw)); }
    close() { this.readyState = 3; }
  }

  class FakeMutationObserver {
    constructor(callback) { this.callback = callback; }
    observe() {}
    disconnect() {}
  }

  const element = {
    nodeType: 1,
    getAttribute() { return null; },
    querySelectorAll() { return []; },
  };
  const document = {
    readyState: 'complete',
    documentElement: element,
    hidden: false,
    addEventListener() {},
  };
  const originalInvoke = function (api, params) {
    invokeCalls += 1;
    calls.push({api, params});
    return Promise.resolve(options.invoke ? options.invoke(api, params) : {});
  };
  const imsdk = {
    invoke: originalInvoke,
    on(names, handler) {
      for (const name of names) {
        handlers.set(name, handler);
        registrations.set(name, (registrations.get(name) || 0) + 1);
      }
    },
    off() { offCalls += 1; throw new Error('imsdk.off must not be called'); },
  };
  const localStorage = {
    values: options.storage || new Map(),
    getItem(key) { return this.values.get(key) || null; },
    setItem(key, value) { this.values.set(key, value); },
  };
  const window = {
    imsdk,
    _db: {msgDataMap: new Map(), ...(options.db || {})},
    _conversationId: {ccode: 'foreground#1@cntaobao'},
    localStorage,
    addEventListener() {},
  };
  const context = {
    window,
    document,
    WebSocket: FakeWebSocket,
    MutationObserver: FakeMutationObserver,
    console: {log() {}, warn() {}, error() {}},
    setTimeout(callback, delay) {
      if (options.asyncRpc && delay === 800) queueMicrotask(callback);
      else if (!options.asyncRpc || delay < 1000) callback();
      return 1;
    },
    clearTimeout() {},
    setInterval() { return 1; },
    clearInterval() {},
    Promise,
    Date,
    JSON,
    Map,
    Object,
    Array,
    String,
    Number,
    Math,
    RegExp,
    Error,
    URL,
  };
  context.globalThis = context;
  vm.createContext(context);
  const source = fs.readFileSync(path.join(__dirname, '..', 'browser_bridge.js'), 'utf8');
  vm.runInContext(source, context, {filename: 'browser_bridge.js'});
  const socket = FakeWebSocket.instance;
  socket.readyState = FakeWebSocket.OPEN;
  socket.onopen();
  sent.length = 0;
  return {
    window,
    handlers,
    registrations,
    sent,
    calls,
    originalInvoke,
    get invokeCalls() { return invokeCalls; },
    get offCalls() { return offCalls; },
  };
}

test('passive bridge handles mixed multi-argument callbacks per ccode', () => {
  const bridge = loadBridge();
  const background = 'background#2@cntaobao';
  bridge.window._db.msgDataMap.set(background, [{
    messageId: 'msg-b',
    clientId: 'client-b',
    sendTime: Date.now(),
    originData: {
      originBanamaMessage: {
        fromid: {nick: 'buyer-b'},
        toid: {nick: 'seller'},
        summary: 'from local map',
        messageId: 'inner-id-must-not-win',
        mcode: {messageId: 'inner-mcode-id-must-not-win', clientId: 'inner-client-must-not-win'},
        sendTime: 1,
      },
    },
  }]);
  bridge.window._db.msgDataMap.set(
    'foreground#1@cntaobao',
    message('foreground#1@cntaobao', 'buyer-front', 'msg-front', 'must not be sampled'),
  );

  const receive = bridge.handlers.get('im.singlemsg.onReceiveNewMsg');
  assert.equal(typeof receive, 'function');
  receive(
    'im.singlemsg.onReceiveNewMsg',
    {
      data: {
        messages: [message('direct#1@cntaobao', 'buyer-a', 'msg-a', 'direct payload')],
        notification: {conversation: {ccode: background}},
      },
    },
  );

  const events = bridge.sent.filter(item => item.type === 'chat_event').map(item => item.payload);
  assert.equal(events.length, 2);
  assert.deepEqual(events.map(item => item.msg_id).sort(), ['msg-a', 'msg-b']);
  assert.equal(events.filter(item => item.msg_id === 'msg-a').length, 1);
  assert.equal(events.filter(item => item.msg_id === 'msg-b').length, 1);
  const cached = events.find(item => item.msg_id === 'msg-b');
  assert.equal(cached.content, 'from local map');
  assert.equal(cached.role, 'user');
  assert.equal(cached.buyer_nick, 'buyer-b');
  assert.equal(cached.original_msg_id, 'msg-b');
  assert.ok(cached.original_timestamp > 0);
  assert.equal(events.some(item => item.msg_id === 'msg-front'), false);
  assert.equal(bridge.window.imsdk.invoke.__qn_standalone_wrapped, true);
  assert.equal(bridge.invokeCalls, 0);
  assert.equal(bridge.offCalls, 0);
});

test('self-heal neither rebinds the same SDK nor calls invoke/off', () => {
  const bridge = loadBridge();
  bridge.window.__qn_standalone_self_heal('contract-test');
  for (const count of bridge.registrations.values()) assert.equal(count, 1);
  assert.equal(bridge.window.imsdk.invoke.__qn_standalone_wrapped, true);
  assert.equal(bridge.invokeCalls, 0);
  assert.equal(bridge.offCalls, 0);
});

test('passive refresh discovers changed local cache without an SDK callback', () => {
  const bridge = loadBridge();
  const ccode = 'background#3@cntaobao';
  bridge.window._db.msgDataMap.set(
    ccode,
    message(ccode, 'buyer-cache', 'msg-cache', 'cache-only payload'),
  );

  bridge.window.__qn_standalone_self_heal('passive-cache-test');

  const events = bridge.sent.filter(item => item.type === 'chat_event').map(item => item.payload);
  assert.equal(events.length, 1);
  assert.equal(events[0].msg_id, 'msg-cache');
  assert.equal(events[0].content, 'cache-only payload');
  assert.equal(events[0].buyer_id, ccode);
  assert.equal(bridge.invokeCalls, 0);
  assert.equal(bridge.offCalls, 0);
});

test('login identity keeps the buyer stable for inbound and outbound messages', () => {
  const bridge = loadBridge();
  const ccode = '4054500565.1-11789284.1#11001@cntaobao';
  const seller = 'sbpgklso';
  const buyer = 'tb136202715';
  const receive = bridge.handlers.get('im.singlemsg.onReceiveNewMsg');

  receive({data: {messages: [
    {
      cid: {ccode},
      fromid: {nick: seller},
      toid: {nick: buyer},
      loginid: {nick: seller},
      summary: 'seller outbound',
      mcode: {messageId: 'seller-outbound-1'},
      sendTime: Date.now(),
    },
    {
      cid: {ccode},
      fromid: {nick: buyer},
      toid: {nick: seller},
      loginId: {nick: seller},
      summary: 'buyer inbound',
      mcode: {messageId: 'buyer-inbound-1'},
      sendTime: Date.now(),
    },
  ]}});

  const events = bridge.sent.filter(item => item.type === 'chat_event').map(item => item.payload);
  assert.equal(events.length, 2);
  const outbound = events.find(item => item.msg_id === 'seller-outbound-1');
  const inbound = events.find(item => item.msg_id === 'buyer-inbound-1');
  for (const event of events) {
    assert.equal(event.account, seller);
    assert.equal(event.buyer_nick, buyer);
    assert.equal(event.buyer_id, ccode);
    assert.equal(event.seller_identity_source, 'loginid');
  }
  assert.equal(outbound.role, 'mall_cs');
  assert.equal(inbound.role, 'user');
});

const NUMERIC_BUYER = '4007146934.1-126446588.1#11001@cntaobao';
function uidMessage(id, extra = {}) {
  return {...message(NUMERIC_BUYER, '', id, 'hello'),
    loginid: {nick: 'seller'}, fromid: {uid: '4007146934.1'}, ...extra};
}
function captured(bridge, type = 'message') {
  return bridge.sent.filter(e => e.type === 'chat_event' && (e.payload.type || 'message') === type).map(e => e.payload);
}
async function settle() { for (let i = 0; i < 30; i++) await Promise.resolve(); }

for (const field of ['senderNick', 'nick', 'senderName']) {
  test(`inbound capture reads ${field} and preserves identifiers`, () => {
    const b = loadBridge();
    const raw = uidMessage(`alias-${field}`, {[field]: '真实买家昵称'});
    b.handlers.get('im.singlemsg.onReceiveNewMsg')({data: {messages: [raw]}});
    const row = captured(b)[0];
    assert.equal(row.nickname, '真实买家昵称');
    assert.equal(row.buyer_id, NUMERIC_BUYER);
    assert.equal(row.msg_id, raw.mcode.messageId);
    assert.equal(row.ts, Math.floor(raw.sendTime / 1000));
    assert.equal(row.buyer_nick, '4007146934.1');
    assert.equal(row.account, 'seller');
    assert.equal(b.invokeCalls, 0);
  });
}

test('outgoing senderNick cannot replace the buyer nickname', () => {
  const b = loadBridge();
  const raw = uidMessage('outgoing', {fromid: {nick: 'seller'}, toid: {nick: '真实买家'}, senderNick: 'seller'});
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(raw);
  assert.equal(captured(b)[0].role, 'mall_cs');
  assert.equal(captured(b)[0].nickname, '真实买家');
});

test('local session map provides a name without RPC and caches it', () => {
  const sessions = new Map([[NUMERIC_BUYER, {nick: '会话昵称'}]]);
  const b = loadBridge({db: {sessionList: sessions}});
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(uidMessage('session-1'));
  assert.equal(captured(b)[0].nickname, '会话昵称');
  sessions.clear();
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(uidMessage('session-2'));
  assert.equal(captured(b)[1].nickname, '会话昵称');
  assert.equal(b.invokeCalls, 0);
});

test('UID lookup is coalesced, messages wait for nick, and later messages use cache', async () => {
  let release;
  const b = loadBridge({asyncRpc: true, invoke(api, params) {
    if (api === 'util.GetUserNick') {
      assert.equal(params.uid, '4007146934.1');
      return new Promise(resolve => { release = resolve; });
    }
    return {ok: true, result: {list: []}};
  }});
  const receive = b.handlers.get('im.singlemsg.onReceiveNewMsg');
  receive(uidMessage('resolve-1'));
  receive(uidMessage('resolve-2'));
  assert.equal(captured(b).length, 0);
  await settle();
  const pending = JSON.parse(b.window.localStorage.getItem('qn_standalone_v1_pending_events'));
  assert.equal(pending.filter(e => e.nickname_pending).length, 2);
  await settle();
  assert.equal(b.calls.filter(c => c.api === 'util.GetUserNick').length, 1);
  release({ok: true, result: {nick: '补查昵称'}});
  await settle();
  assert.equal(captured(b).length, 2);
  for (const row of captured(b)) assert.equal(row.nickname, '补查昵称');
  const updates = captured(b, 'nickname_update');
  assert.equal(updates.length, 1);
  assert.equal(updates[0].buyer_id, NUMERIC_BUYER);
  assert.equal(updates[0].nickname, '补查昵称');
  assert.equal('msg_id' in updates[0], false);
  receive(uidMessage('resolve-3'));
  assert.equal(captured(b).at(-1).nickname, '补查昵称');
  assert.equal(b.calls.filter(c => c.api === 'util.GetUserNick').length, 1);
});

test('failed or wrong-UID lookup retains an explicit empty field and throttles misses', async () => {
  const b = loadBridge({asyncRpc: true, invoke(api) {
    return api === 'util.GetUserNick' ? {result: {uid: 'another-buyer', nick: '错误昵称'}} : {result: {list: []}};
  }});
  const receive = b.handlers.get('im.singlemsg.onReceiveNewMsg');
  receive(uidMessage('missing-1'));
  await settle();
  assert.equal(captured(b)[0].nickname, '');
  assert.equal(captured(b, 'nickname_update').length, 0);
  receive(uidMessage('missing-2'));
  await settle();
  assert.equal(captured(b)[1].nickname, '');
  assert.equal(b.calls.filter(c => c.api === 'util.GetUserNick').length, 1);
});

test('backfill resolves loaded matching-shop sessions only and emits no messages', async () => {
  const b = loadBridge({asyncRpc: true, invoke(api) {
    return api === 'util.GetUserNick' ? {result: '在线买家昵称'} : {result: {list: []}};
  }});
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(message('seed#1@cntaobao', 'seed', 'seed', 'seed'));
  b.window._db.msgDataMap.set(NUMERIC_BUYER, []);
  b.sent.length = 0;
  const result = await b.window.__qn_standalone_backfill_nicknames([
    {account: 'seller', buyer_id: NUMERIC_BUYER},
    {account: 'other-shop', buyer_id: NUMERIC_BUYER},
    {account: 'seller', buyer_id: 'offline#1@cntaobao'},
  ]);
  assert.equal(result.length, 1);
  assert.equal(captured(b).length, 0);
  assert.equal(captured(b, 'nickname_update').length, 1);
  await b.window.__qn_standalone_backfill_nicknames([{account: 'seller', buyer_id: NUMERIC_BUYER}]);
  assert.equal(captured(b, 'nickname_update').length, 1);
  assert.equal(b.calls.filter(c => c.api === 'util.GetUserNick').length, 1);
});

test('nickname cache survives page reload and is scoped to account', () => {
  const storage = new Map();
  let b = loadBridge({storage});
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(uidMessage('persist-1', {senderNick: '缓存昵称'}));
  b = loadBridge({storage});
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(uidMessage('persist-2'));
  assert.equal(captured(b)[0].nickname, '缓存昵称');
  assert.equal(b.invokeCalls, 0);
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(uidMessage('persist-other', {loginid: {nick: 'other-shop'}, toid: {nick: 'other-shop'}}));
  assert.equal(captured(b).some(row => row.msg_id === 'persist-other' && row.nickname === '缓存昵称'), false);
});

test('one session-list batch supplies multiple missing names without UID RPC', async () => {
  const second = '2217298756354.1-126446588.1#11001@cntaobao';
  const b = loadBridge({asyncRpc: true, invoke(api) {
    if (api === 'util.GetUserNick') throw new Error('session list already contains the nick');
    return {result: {list: [{cid: {ccode: NUMERIC_BUYER}, nick: '买家甲'}, {cid: {ccode: second}, nick: '买家乙'}]}};
  }});
  const receive = b.handlers.get('im.singlemsg.onReceiveNewMsg');
  receive(uidMessage('batch-1'));
  receive(uidMessage('batch-2', {cid: {ccode: second}, fromid: {uid: '2217298756354.1'}}));
  await settle();
  assert.deepEqual(captured(b).map(row => row.nickname).sort(), ['买家乙', '买家甲'].sort());
  assert.equal(b.calls.length, 1);
});

test('pending nickname capture survives reload and uses the same message id', async () => {
  const storage = new Map();
  let first = loadBridge({storage, asyncRpc: true, invoke(api) {
    return api === 'util.GetUserNick' ? new Promise(() => {}) : {result: {list: []}};
  }});
  first.handlers.get('im.singlemsg.onReceiveNewMsg')(uidMessage('reload-pending'));
  await settle();
  assert.equal(captured(first).length, 0);
  const second = loadBridge({storage, asyncRpc: true, invoke(api) {
    return api === 'util.GetUserNick' ? {data: {nickname: '重载补查昵称'}} : {result: {list: []}};
  }});
  await settle();
  const rows = captured(second);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].nickname, '重载补查昵称');
  assert.equal(rows[0].msg_id, 'reload-pending');
  assert.equal(rows[0].event_id, 'qn-msg-v1|taobao|reload-pending');
});

test('Tmall UID-only own outbound remains staff', () => {
  const b = loadBridge();
  b.handlers.get('im.singlemsg.onReceiveNewMsg')({data: {messages: [{
    cid: {ccode: 'buyer.1-seller.1#11001@cntaobao'},
    fromid: {uid: 'seller.1'}, toid: {uid: 'buyer.1'},
    loginid: {nick: '联想官方旗舰店:燕燕', uid: 'seller.1'},
    summary: '客服已经处理', mcode: {messageId: 'review-own-uid'}, sendTime: Date.now(),
    receiverNick: '买家昵称', senderNick: '联想官方旗舰店:燕燕',
  }]}});
  const row = captured(b)[0];
  assert.equal(row.role, 'mall_cs');
  assert.notEqual(row.buyer_nick, 'seller.1');
});

test('Tmall other staff seat preserves buyer nickname', () => {
  const b = loadBridge();
  b.handlers.get('im.singlemsg.onReceiveNewMsg')({data: {messages: [{
    cid: {ccode: 'buyer.1-seller.1#11001@cntaobao'},
    fromid: {nick: '联想官方旗舰店:雪晴'}, toid: {nick: '真实买家'},
    loginid: {nick: '联想官方旗舰店:燕燕'},
    summary: '已处理', mcode: {messageId: 'review-other-seat'}, sendTime: Date.now(),
  }]}});
  const row = captured(b)[0];
  assert.equal(row.role, 'mall_cs');
  assert.equal(row.nickname, '真实买家');
});

test('Tmall missing original timestamp is context only', () => {
  const b=loadBridge();
  const raw=message('buyer#1@cntaobao','买家昵称','review-missing-ts','由 雪晴 转交给 燕燕');
  raw.loginid={nick:'联想官方旗舰店:燕燕'}; raw.toid={nick:'联想官方旗舰店:燕燕'};
  delete raw.sendTime;
  b.handlers.get('im.singlemsg.onReceiveNewMsg')({data:{messages:[raw]}});
  const row=captured(b)[0];
  assert.equal(row.incomplete,true);
  assert.equal(row.original_timestamp,0);
  assert.equal(row.capture_mode, "history_snapshot");
});


test('Tmall unresolved nickname cannot hold a live transfer notification', async () => {
  const b=loadBridge({asyncRpc:true,invoke(){return new Promise(()=>{});}});
  const raw=uidMessage('tmall-live-notice',{loginid:{nick:'联想官方旗舰店:燕燕',uid:'126446588.1'},toid:{uid:'126446588.1'},summary:'由雪晴转交给燕燕'});
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(raw);
  assert.equal(captured(b)[0].msg_id,'tmall-live-notice');
  assert.equal(captured(b)[0].nickname,'');
  assert.equal(captured(b)[0].capture_mode,'event:im.singlemsg.onReceiveNewMsg');
});

test('Tmall unresolved ordinary sender is context only without staff nickname', () => {
  const b=loadBridge();
  const raw=message('buyer.1-seller.1#11001@cntaobao','unresolved sender','tmall-unknown','收到');
  raw.loginid={nick:'联想官方旗舰店:燕燕'}; raw.toid={uid:'unresolved'};
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(raw);
  const row=captured(b)[0];
  assert.equal(row.role,'unknown');
  assert.equal(row.identity_uncertain,true);
  assert.equal(row.nickname,'');
});


test('Tmall transfer emitted by a staff seat keeps a buyer-eligible parent without staff nickname', () => {
  const b=loadBridge();
  const raw=message('buyer.1-seller.1#11001@cntaobao','联想官方旗舰店:雪晴','tmall-staff-transfer','由雪晴转交给燕燕');
  raw.loginid={nick:'联想官方旗舰店:燕燕'}; raw.toid={nick:'真实买家'};
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(raw);
  const row=captured(b)[0];
  assert.equal(row.role,'user');
  assert.notEqual(row.nickname,'联想官方旗舰店:雪晴');
  assert.equal(row.original_msg_id,'tmall-staff-transfer');
});

test('native-only nickname backfill reads explicit cached login identity without replaying chat', async () => {
  const buyer = '2212089943099.1-126446588.1#11001@cntaobao';
  const account = '联想官方旗舰店:小山';
  const cached = {...message(buyer, '2212089943099', '4337282500251.PNM', '你好，我买的随身WiFi用不了'), loginid: {nick: account}};
  const b = loadBridge({asyncRpc: true, invoke(api) {
    return api === 'util.GetUserNick' ? {result: {uid: '2212089943099.1', nick: 'dysileib'}} : {result: {list: []}};
  }});
  b.window._db.msgDataMap.set(buyer, [cached]);
  const result = await b.window.__qn_standalone_backfill_nicknames([{account, buyer_id: buyer}]);
  assert.equal(result.length, 1);
  assert.equal(result[0].nickname, 'dysileib');
  assert.equal(captured(b).length, 0);
  assert.equal(captured(b, 'nickname_update')[0].account, account);
  assert.equal(captured(b, 'nickname_update')[0].buyer_id, buyer);
});

test('native-only backfill cannot infer login account from target or buyer nickname', async () => {
  const b = loadBridge({asyncRpc: true});
  b.window._db.msgDataMap.set(NUMERIC_BUYER, [uidMessage('cached-no-login', {loginid: undefined})]);
  const result = await b.window.__qn_standalone_backfill_nicknames([{account: 'seller', buyer_id: NUMERIC_BUYER}]);
  assert.equal(result.length, 0);
  assert.equal(captured(b, 'nickname_update').length, 0);
});

test('native-only backfill rejects a different or conflicting cached seller', async () => {
  for (const accounts of [['other-shop'], ['seller', 'other-shop']]) {
    const b = loadBridge({asyncRpc: true});
    b.window._db.msgDataMap.set(NUMERIC_BUYER, accounts.map((account, i) => uidMessage('cached-' + i, {loginid: {nick: account}})));
    const result = await b.window.__qn_standalone_backfill_nicknames([{account: 'seller', buyer_id: NUMERIC_BUYER}]);
    assert.equal(result.length, 0);
    assert.equal(captured(b, 'nickname_update').length, 0);
    assert.equal(b.calls.some(call => call.api === 'util.GetUserNick'), false);
  }
});

test('transfer cache preserves an older buyer question as context before the live notice', () => {
  const account = '联想官方旗舰店:燕燕';
  const bridge = loadBridge();
  const common = {loginid: {nick: account}, toid: {nick: account}, senderNick: '真实买家'};
  const question = uidMessage('old-pre-transfer', {...common, summary: '设备怎么打开', sendTime: Date.now()-6*60*1000});
  const notice = uidMessage('live-transfer', {...common, summary: '由 服务助手 转交给 燕燕', sendTime: Date.now()});
  bridge.window._db.msgDataMap.set(NUMERIC_BUYER, [question, notice]);
  bridge.window.__qn_standalone_self_heal('transfer-history-test');
  const events = captured(bridge);
  const old = events.find(event => event.msg_id === 'old-pre-transfer');
  assert.ok(old, 'buyer question visible in Qianniu must not be dropped');
  assert.equal(old.capture_mode, 'history_snapshot');
  assert.equal(old.content, '设备怎么打开');
  assert.equal(old.buyer_id, NUMERIC_BUYER);
  assert.equal(old.account, account);
  assert.ok(events.findIndex(event => event.msg_id === 'old-pre-transfer') < events.findIndex(event => event.msg_id === 'live-transfer'));
  assert.equal(bridge.invokeCalls, 0);
});

test('transfer context marking preserves a newer buyer question and does not enable other shops', () => {
  for (const account of ['联想官方旗舰店:燕燕', '普通淘宝店:客服']) {
    const bridge = loadBridge();
    const common = {loginid: {nick: account}, toid: {nick: account}, senderNick: '真实买家'};
    const now = Date.now();
    bridge.window._db.msgDataMap.set(NUMERIC_BUYER, [
      uidMessage('older', {...common, summary: '物联卡是什么', sendTime: now-6*60*1000}),
      uidMessage('transfer', {...common, summary: '由 服务助手 转交给 燕燕', sendTime: now}),
      uidMessage('newer', {...common, summary: '怎么续费', sendTime: now+1000}),
    ]);
    bridge.window.__qn_standalone_self_heal('transfer-context-boundaries');
    const events = captured(bridge);
    const newer = events.find(event => event.msg_id === 'newer');
    assert.ok(newer);
    assert.equal(newer.capture_mode, 'event-local-db:im.singlemsg.onReceiveNewMsg');
    if (account.startsWith('联想')) assert.equal(events.find(event => event.msg_id === 'older').capture_mode, 'history_snapshot');
    else assert.equal(events.some(event => event.msg_id === 'older'), false);
  }
});

test('old Tmall cache stays available as context without triggering live replay', () => {
  const bridge = loadBridge();
  const account = '联想官方旗舰店:燕燕';
  const common = {loginid: {nick: account}, toid: {nick: account}, senderNick: '真实买家'};
  bridge.window._db.msgDataMap.set(NUMERIC_BUYER, [
    uidMessage('old-q', {...common, summary: '设备怎么打开', sendTime: Date.now()-7*60*1000}),
    uidMessage('old-notice', {...common, summary: '由 服务助手 转交给 燕燕', sendTime: Date.now()-6*60*1000}),
  ]);
  bridge.window.__qn_standalone_self_heal('stale-transfer-context');
  const rows = captured(bridge);
  assert.equal(rows.length, 2);
  assert.ok(rows.every(row => row.capture_mode === 'history_snapshot'));
});


test('Tmall uploads observed sender evidence even when role cannot be resolved', () => {
  const b=loadBridge();
  const raw=uidMessage('raw-identity',{loginid:{nick:'联想官方旗舰店:燕燕',uid:'126446588.1'},fromid:{uid:'4007146934.1',nick:'真实买家'},toid:{nick:'联想官方旗舰店:燕燕',uid:'126446588.1'}});
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(raw);
  const row=captured(b)[0];
  assert.equal(row.sender_uid,'4007146934.1');
  assert.equal(row.login_uid,'126446588.1');
  assert.equal(row.sender_nick,'真实买家');
  assert.equal(row.recipient_nick,'联想官方旗舰店:燕燕');
});

test('other shops do not get the Tmall raw-evidence contract', async () => {
  const b=loadBridge();
  b.handlers.get('im.singlemsg.onReceiveNewMsg')(uidMessage('other-raw'));
  await settle();
  assert.equal(captured(b)[0].sender_uid,undefined);
});
