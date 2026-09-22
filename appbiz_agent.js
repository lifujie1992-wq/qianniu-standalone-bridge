'use strict';

const appBiz = Process.getModuleByName('AppBiz.dll');
const HOOK_PROFILES = {
  0x1f05000: {
    version: '9.97.59N',
    serviceVtable: 0x18af478,
    secondaryVtable1: 0x18af5e0,
    secondaryVtable2: 0x18af5f0,
    serviceGetNewMsg: 0xa6c080,
    serviceOnMessageArrive: 0xa6f9a0,
    serviceSendText: 0xa77ff0,
  },
  0x1f17000: {
    version: '9.97.74N',
    serviceVtable: 0x18bdee8,
    secondaryVtable1: 0x18be050,
    secondaryVtable2: 0x18be060,
    serviceGetNewMsg: 0xa77b50,
    serviceOnMessageArrive: 0xa7b470,
    serviceSendText: 0xa83af0,
  },
  0x1f20000: {
    version: '9.97.81N',
    serviceVtable: 0x18c4c78,
    secondaryVtable1: 0x18c4de0,
    secondaryVtable2: 0x18c4df0,
    serviceGetNewMsg: 0xa798a0,
    serviceOnMessageArrive: 0xa7d1c0,
    serviceSendText: 0xa85840,
  },
};
const hookProfile = HOOK_PROFILES[appBiz.size];
if (!hookProfile) {
  throw new Error('unsupported AppBiz.dll image size 0x' + appBiz.size.toString(16));
}
const serviceVtable = appBiz.base.add(hookProfile.serviceVtable);
const secondaryVtable1 = appBiz.base.add(hookProfile.secondaryVtable1);
const secondaryVtable2 = appBiz.base.add(hookProfile.secondaryVtable2);
const serviceGetNewMsg = appBiz.base.add(hookProfile.serviceGetNewMsg);
const serviceOnMessageArrive = appBiz.base.add(hookProfile.serviceOnMessageArrive);
const serviceSendText = appBiz.base.add(hookProfile.serviceSendText);

function requireExecutable(address, name) {
  const range = Process.findRangeByAddress(address);
  if (!range || range.protection.indexOf('x') < 0) {
    throw new Error(name + ' does not point to executable memory: ' + address);
  }
}

requireExecutable(serviceGetNewMsg, 'serviceGetNewMsg');
requireExecutable(serviceOnMessageArrive, 'serviceOnMessageArrive');
requireExecutable(serviceSendText, 'serviceSendText');
if (!serviceVtable.add(0x18).readPointer().equals(serviceGetNewMsg)) {
  throw new Error('service vtable/GetNewMsg profile mismatch for ' + hookProfile.version);
}
if (!serviceVtable.add(0x90).readPointer().equals(serviceSendText)) {
  throw new Error('service vtable/SendText profile mismatch for ' + hookProfile.version);
}
const helperPath = __APPBIZ_ADAPTER_PATH__;
const selectedServiceHint = __SELECTED_SERVICE__;
const SERVICE_CCACHE_TTL_MS = 120000;
const SERVICE_CCACHE_MAX = 256;
const MESSAGE_SIZE = 584;
const MESSAGE_CCODE_OFFSET = 8;
const MESSAGE_ID_OFFSET = 48;
const MESSAGE_BUYER_ID_OFFSET = 120;
const MESSAGE_SELLER_ID_OFFSET = 192;
const MESSAGE_CONTENT_OFFSET = 352;
const candidates = [];
const serviceByCcode = new Map();
let selectedService = null;
let selectionReason = '';
let selectedAtMs = 0;
let observedCalls = 0;
let receivedCalls = 0;
let receivedMessages = 0;
let lastReceiveAtMs = 0;
let helperModule = null;
let helperSendText = null;
let helperPollSend = null;
let helperCancelSend = null;
let lastCandidateScanMs = 0;

function readMsvcString(address) {
  try {
    const size = Number(address.add(0x10).readU64().toString());
    const capacity = Number(address.add(0x18).readU64().toString());
    if (!Number.isSafeInteger(size) || size < 0 || size > 1024 * 1024) return null;
    if (!Number.isSafeInteger(capacity) || capacity < size || capacity > 1024 * 1024) return null;
    const data = capacity >= 16 ? address.readPointer() : address;
    return size ? data.readUtf8String(size) : '';
  } catch (_) {
    return null;
  }
}

function readHex(address, length) {
  try {
    if (address.isNull()) return null;
    const bytes = new Uint8Array(address.readByteArray(length));
    return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  } catch (_) {
    return null;
  }
}

function pruneServiceCcache(now) {
  const expiresAt = now - SERVICE_CCACHE_TTL_MS;
  for (const [ccode, item] of serviceByCcode) {
    if (item.seen_at_ms < expiresAt) serviceByCcode.delete(ccode);
  }
  while (serviceByCcode.size >= SERVICE_CCACHE_MAX) {
    let oldestKey = null;
    let oldestAt = Infinity;
    for (const [ccode, item] of serviceByCcode) {
      if (item.seen_at_ms < oldestAt) {
        oldestAt = item.seen_at_ms;
        oldestKey = ccode;
      }
    }
    if (oldestKey === null) break;
    serviceByCcode.delete(oldestKey);
  }
}

function scanCandidates() {
  const found = [];
  for (const range of Process.enumerateRanges({protection: 'rw-', coalesce: true})) {
    let matches = [];
    try {
      matches = Memory.scanSync(range.base, range.size, serviceVtable.toMatchPattern());
    } catch (_) {
      continue;
    }
    for (const match of matches) {
      if (found.length >= 64) break;
      const object = match.address;
      try {
        if (!object.add(872).readPointer().equals(secondaryVtable1)) continue;
        if (!object.add(888).readPointer().equals(secondaryVtable2)) continue;
        found.push(object);
      } catch (_) {}
    }
    if (found.length >= 64) break;
  }

  candidates.splice(0, candidates.length, ...found);
  lastCandidateScanMs = Date.now();
  if (selectedService !== null && !candidates.some(item => item.equals(selectedService))) {
    selectedService = null;
    selectionReason = '';
    selectedAtMs = 0;
  }
  return candidates.length;
}

scanCandidates();

// Login and shift changes can create the service objects after this script is
// attached. Keep retrying only while no service object is available.
setInterval(function () {
  if (candidates.length === 0) scanCandidates();
}, 15000);

if (typeof selectedServiceHint === 'string') {
  selectedService = candidates.find(item => item.toString() === selectedServiceHint) || null;
  if (selectedService !== null) {
    selectionReason = 'same_process_persisted';
    selectedAtMs = Date.now();
  }
}

function candidateDetails(service) {
  return {
    service: service.toString(),
    seller_id: readMsvcString(service.add(0x3f8)) || '',
    account: readMsvcString(service.add(0x438)) || '',
    selected: selectedService !== null && service.equals(selectedService),
  };
}

function selectService(service, reason) {
  let candidateIndex = candidates.findIndex(item => item.equals(service));
  if (candidateIndex < 0 && Date.now() - lastCandidateScanMs >= 10000) {
    scanCandidates();
    candidateIndex = candidates.findIndex(item => item.equals(service));
  }
  if (candidateIndex < 0) return false;
  const changed = selectedService === null || !selectedService.equals(service) || selectionReason !== reason;
  selectedService = service;
  selectionReason = reason;
  selectedAtMs = Date.now();
  if (changed) {
    send({
      event: 'appbiz_service_selected',
      service: selectedService.toString(),
      reason: selectionReason,
      candidate_index: candidateIndex,
      candidate_count: candidates.length,
      selected_at_ms: selectedAtMs,
    });
  }
  return true;
}

Interceptor.attach(serviceGetNewMsg, {
  onEnter(args) {
    const ccode = readMsvcString(args[1]);
    const operation = readMsvcString(args[6]);
    if (
      !ccode || ccode.indexOf('#') < 0 || ccode.indexOf('@') < 0 ||
      !operation || operation.indexOf('GetNewMsg|') !== 0
    ) return;
    if (!selectService(args[0], 'singlemsg_getnewmsg')) return;
    const now = Date.now();
    pruneServiceCcache(now);
    serviceByCcode.set(ccode, {service: args[0], seen_at_ms: now});
  },
});

Interceptor.attach(serviceOnMessageArrive, {
  onEnter(args) {
    const service = args[0];
    selectService(service, 'message_arrive');
    try {
      const vector = args[1];
      const begin = vector.readPointer();
      const end = vector.add(Process.pointerSize).readPointer();
      const span = end.sub(begin).toInt32();
      if (span <= 0 || span % MESSAGE_SIZE !== 0) return;
      const count = Math.min(span / MESSAGE_SIZE, 100);
      receivedCalls += 1;
      const now = Date.now();
      lastReceiveAtMs = now;
      const account = readMsvcString(service.add(0x438)) || '';
      for (let index = 0; index < count; index++) {
        const message = begin.add(index * MESSAGE_SIZE);
        const ccode = readMsvcString(message.add(MESSAGE_CCODE_OFFSET));
        const messageId = readMsvcString(message.add(MESSAGE_ID_OFFSET));
        const buyerId = readMsvcString(message.add(MESSAGE_BUYER_ID_OFFSET));
        const sellerId = readMsvcString(message.add(MESSAGE_SELLER_ID_OFFSET));
        const content = readMsvcString(message.add(MESSAGE_CONTENT_OFFSET));
        if (
          !ccode || ccode.indexOf('#') < 0 || ccode.indexOf('@') < 0 ||
          !messageId || !content
        ) continue;
        pruneServiceCcache(now);
        serviceByCcode.set(ccode, {service: service, seen_at_ms: now});
        receivedMessages += 1;
        send({
          event: 'appbiz_receive',
          message: {
            platform: 'taobao',
            role: 'user',
            content: content,
            account: account || sellerId,
            buyer_id: ccode,
            buyer_cid: ccode,
            buyer_nick: buyerId,
            msg_id: messageId,
            original_msg_id: messageId,
            ts: now / 1000,
            source: 'qianniu_appbiz_on_message_arrive',
            capture_mode: 'appbiz_native_callback',
            raw_type: 'text',
          },
        });
      }
    } catch (error) {
      send({event: 'appbiz_receive_error', error: String(error)});
    }
  },
});

Interceptor.attach(serviceSendText, {
  onEnter(args) {
    if (!selectService(args[0], 'official_send')) return;
    observedCalls += 1;
    const ccode = readMsvcString(args[1]);
    const content = readMsvcString(args[2]);
    const source = readMsvcString(args[3]);
    send({
      event: 'appbiz_send_observed',
      service: selectedService.toString(),
      candidate_index: candidates.findIndex(item => item.equals(selectedService)),
      candidate_count: candidates.length,
      args: [ccode, content, source],
      metadata_first_96_bytes: readHex(args[4], 96),
      callback_first_64_bytes: readHex(args[5], 64),
    });
    if (
      ccode && ccode.indexOf('#') >= 0 && ccode.indexOf('@') >= 0 &&
      content && (!source || source.indexOf('QianniuStandaloneBridge/') !== 0)
    ) {
      const now = Date.now();
      send({
        event: 'appbiz_outgoing',
        message: {
          platform: 'taobao',
          role: 'mall_cs',
          content: content,
          account: readMsvcString(args[0].add(0x438)) || '',
          buyer_id: ccode,
          buyer_cid: ccode,
          msg_id: 'appbiz-out-' + now + '-' + observedCalls,
          original_msg_id: 'appbiz-out-' + now + '-' + observedCalls,
          ts: now / 1000,
          source: 'qianniu_appbiz_official_send',
          capture_mode: 'appbiz_native_callback',
          raw_type: 'text',
        },
      });
    }
  },
});

function ensureHelper() {
  if (helperSendText !== null) return;
  helperModule = Module.load(helperPath);
  helperSendText = new NativeFunction(
    helperModule.getExportByName('appbiz_send_text_v2'),
    'int',
    ['pointer', 'pointer', 'pointer', 'pointer', 'pointer', 'uint64']
  );
  helperPollSend = new NativeFunction(
    helperModule.getExportByName('appbiz_poll_send_result_v1'),
    'int',
    ['uint64', 'pointer']
  );
  helperCancelSend = new NativeFunction(
    helperModule.getExportByName('appbiz_cancel_send_result_v1'),
    'int',
    ['uint64']
  );
}

rpc.exports = {
  status() {
    if ((selectedService === null || candidates.length === 0) && Date.now() - lastCandidateScanMs >= 30000) {
      scanCandidates();
    }
    const details = candidates.map(candidateDetails);
    return {
      module_base: appBiz.base.toString(),
      module_size: appBiz.size,
      hook_profile: hookProfile.version,
      hook_validated: true,
      candidate_count: candidates.length,
      candidates: candidates.map(item => item.toString()),
      candidate_details: details,
      routable_candidate_count: selectedService === null ? 0 : 1,
      observed_ccode_count: serviceByCcode.size,
      selected_service: selectedService === null ? null : selectedService.toString(),
      selection_reason: selectionReason,
      selected_at_ms: selectedAtMs,
      last_candidate_scan_ms: lastCandidateScanMs,
      observed_calls: observedCalls,
      received_calls: receivedCalls,
      received_messages: receivedMessages,
      last_receive_at_ms: lastReceiveAtMs,
      helper_loaded: helperModule !== null,
    };
  },
  sendtext(arg1, arg2, arg3, receiptToken) {
    const ccode = String(arg1);
    const route = serviceByCcode.get(ccode);
    if (!route || Date.now() - route.seen_at_ms > 120000) {
      throw new Error('Qianniu has no recent GetNewMsg context for the target ccode');
    }
    if (!selectService(route.service, 'singlemsg_getnewmsg')) {
      throw new Error('the GetNewMsg service for the target ccode is no longer valid');
    }
    ensureHelper();
    const first = Memory.allocUtf8String(String(arg1));
    const second = Memory.allocUtf8String(String(arg2));
    const third = Memory.allocUtf8String(String(arg3));
    const token = new UInt64(String(receiptToken));
    return helperSendText(selectedService, appBiz.base, first, second, third, token);
  },
  pollsend(receiptToken) {
    ensureHelper();
    const token = new UInt64(String(receiptToken));
    const resultCode = Memory.alloc(4);
    resultCode.writeS32(-2147483648);
    const state = helperPollSend(token, resultCode);
    return {state: state, result_code: resultCode.readS32()};
  },
  cancelsend(receiptToken) {
    ensureHelper();
    return helperCancelSend(new UInt64(String(receiptToken)));
  },
};

send({
  event: 'appbiz_ready',
  module_base: appBiz.base.toString(),
  module_size: appBiz.size,
  hook_profile: hookProfile.version,
  hook_validated: true,
  candidate_count: candidates.length,
  routable_candidate_count: selectedService === null ? 0 : 1,
});
