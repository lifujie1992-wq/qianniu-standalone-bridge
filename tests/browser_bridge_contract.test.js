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

function loadBridge() {
  const handlers = new Map();
  const registrations = new Map();
  const sent = [];
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
  const originalInvoke = function () { invokeCalls += 1; return Promise.resolve({}); };
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
    values: new Map(),
    getItem(key) { return this.values.get(key) || null; },
    setItem(key, value) { this.values.set(key, value); },
  };
  const window = {
    imsdk,
    _db: {msgDataMap: new Map()},
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
    setTimeout(callback) { callback(); return 1; },
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
