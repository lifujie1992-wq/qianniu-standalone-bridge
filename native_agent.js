'use strict';

const PLUGIN_PATH = __QN_PLUGIN_PATH__;
const ALLOW_EXISTING_PLUGIN = __QN_ALLOW_EXISTING_PLUGIN__;
const PLUGIN_NAME = '9.77.01_qnmsgplugin_x64.dll';
const FOREIGN_PLUGIN_NAME = 'InsidePlugin_taobao_x64.dll';

let plugin = null;
let receiveCallback = null;
let setReceiveCallback = null;
let sendText = null;
let ready = false;
let initResult = false;
let lastError = '';

function emit(payload) {
  send(payload);
}

function moduleSummary(module) {
  if (!module) return null;
  return { name: module.name, path: module.path, base: module.base.toString(), size: module.size };
}

function findExport(name) {
  const address = plugin.findExportByName(name);
  if (address === null) throw new Error('missing export: ' + name);
  return address;
}

function install() {
  try {
    const foreign = Process.findModuleByName(FOREIGN_PLUGIN_NAME);
    if (foreign !== null && !ALLOW_EXISTING_PLUGIN) {
      lastError = 'foreign plugin already owns the qnmsg callback';
      emit({ event: 'native_conflict', foreign_module: moduleSummary(foreign) });
      return;
    }

    plugin = Process.findModuleByName(PLUGIN_NAME);
    if (plugin === null) plugin = Module.load(PLUGIN_PATH);

    const init = new NativeFunction(findExport('qnhelper_init_s'), 'bool', ['pointer', 'bool']);
    setReceiveCallback = new NativeFunction(
      findExport('qnhelper_seller_conversation_content_callback_s'),
      'void',
      ['pointer']
    );
    sendText = new NativeFunction(
      findExport('qnhelper_send_msg_text_s'),
      'bool',
      ['pointer', 'pointer', 'pointer', 'bool']
    );

    initResult = !!init(ptr(0), 0);
    if (!initResult) throw new Error('qnhelper_init_s returned false');

    receiveCallback = new NativeCallback(function (rawPointer) {
      try {
        if (rawPointer.isNull()) return;
        const raw = rawPointer.readUtf8String();
        if (!raw) return;
        emit({ event: 'native_receive', raw: raw });
      } catch (error) {
        emit({ event: 'native_receive_error', error: String(error) });
      }
    }, 'void', ['pointer']);
    setReceiveCallback(receiveCallback);
    ready = true;
    emit({ event: 'native_ready', plugin: moduleSummary(plugin), init_result: initResult });
  } catch (error) {
    lastError = String(error && error.stack ? error.stack : error);
    emit({ event: 'native_error', error: lastError });
  }
}

rpc.exports = {
  status() {
    return {
      ready: ready,
      init_result: initResult,
      last_error: lastError,
      plugin: moduleSummary(plugin),
      pid: Process.id
    };
  },

  sendtext(sellerNick, buyerCid, context, isSetTime) {
    if (!ready || sendText === null) throw new Error(lastError || 'native adapter is not ready');
    sellerNick = String(sellerNick || '');
    buyerCid = String(buyerCid || '');
    context = String(context || '');
    if (!sellerNick || !buyerCid || !context) throw new Error('seller_nick, buyer_cid and context are required');
    const seller = Memory.allocUtf8String(sellerNick);
    const buyer = Memory.allocUtf8String(buyerCid);
    const text = Memory.allocUtf8String(context);
    return !!sendText(seller, buyer, text, isSetTime ? 1 : 0);
  },

  shutdown() {
    try {
      if (setReceiveCallback !== null) setReceiveCallback(ptr(0));
    } finally {
      ready = false;
      receiveCallback = null;
    }
    return true;
  }
};

setImmediate(install);
