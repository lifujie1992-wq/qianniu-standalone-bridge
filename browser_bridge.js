/**
 * Qianniu standalone IMSDK receive bridge v1.
 *
 * Receives IM SDK events in the Qianniu chat page and forwards normalized
 * messages to the standalone service on loopback. It only observes SDK
 * events and local memory; it never calls an API that can advance UI cursors.
 */
(function () {
  if (window.__qn_standalone_bridge_v1_installed) {
    if (typeof window.__qn_standalone_reconnect === "function") window.__qn_standalone_reconnect();
    return;
  }
  window.__qn_standalone_bridge_v1_installed = true;

  var BRIDGE_VERSION = "qn-standalone-browser-v6-stable-identity";
  var WS_URL = String(window.__qn_standalone_ws_url || "ws://127.0.0.1:42110/");
  var STARTED_AT_MS = Date.now();
  var RECOVERY_WINDOW_MS = 5 * 60 * 1000;
  var MAX_KNOWN_CONVERSATIONS = 80;
  var MAX_SEEN = 1200;
  var MAX_OUTBOX = 500;
  var OUTBOX_PERSIST_DELAY_MS = 800;
  var outboxPersistTimer = null;
  var socket = null;
  var reconnectTimer = null;
  var heartbeatTimer = null;
  var watchdogTimer = null;
  var domObserver = null;
  var domObserverRoot = null;
  var outbox = {};
  var outboxOrder = [];
  var OUTBOX_KEY = "qn_standalone_v1_pending_events";
  var seenMsgIds = {};
  var seenOrder = [];
  var knownConversations = {};
  var conversationOrder = [];
  var passiveCacheCursor = 0;
  var passiveCacheFingerprints = {};
  var lastSellerNick = "";
  var lastDomScanAt = 0;
  var localRetryTimers = {};
  var LOCAL_RETRY_DELAYS_MS = [50, 200, 600, 1500, 3000];
  var invokeNotifyTimer = null;
  var hookedImSdk = null;
  var everConnected = false;

  var diagnostics = {
    version: BRIDGE_VERSION,
    started_at_ms: Date.now(),
    websocket_connected: false,
    imsdk_hooked: false,
    poll_timer_running: false,
    dom_observer_running: false,
    subscriptions: {},
    event_callbacks: 0,
    known_conversations: 0,
    last_event_at_ms: 0,
    last_capture_at_ms: 0,
    last_capture_mode: "",
    last_poll_at_ms: 0,
    last_poll_success_at_ms: 0,
    last_poll_duration_ms: 0,
    poll_error_count: 0,
    reconnect_count: 0,
    self_heal_count: 0,
    watchdog_last_at_ms: 0,
    pending_events: 0,
    queued_while_disconnected: 0,
    incomplete_history_count: 0,
    stale_history_skipped: 0,
    local_db_hits: 0,
    sent_events: 0,
    dropped_while_disconnected: 0,
    outbox_overflow_dropped: 0,
    background_notifications: 0,
    remote_fetch_calls: 0,
    remote_fetch_success: 0,
    remote_fetch_failures: 0,
    last_background_ccode: "",
    last_event_raw: "",
    last_event_arg_count: 0,
    last_event_arg_types: [],
    last_event_shape: "",
    last_event_ccode_count: 0,
    local_lookup_attempts: 0,
    local_lookup_hits: 0,
    local_lookup_misses: 0,
    passive_cache_scans: 0,
    passive_cache_changes: 0,
    passive_cache_last_at_ms: 0,
    invoke_notify_hooked: false,
    has_onInvokeNotify: false,
    has_task_cache: false,
    has_conversationId: false,
    has_vs: false,
    current_ccode_hint: "",
    has_db: false,
    has_msgDataMap: false,
    msgDataMap_size: -1,
    msgDataMap_keys: [],
    msgDataMap_sample: "",
    msgDataMap_error: "",
    msgdb_last_arrlen: -1,
    msgdb_last_msgid: "",
    msgdb_last_sendtime: "",
    msgdb_last_normalized: false,
    msgdb_last_reason: "",
    msgdb_last_rawtext: "",
    msgdb_last_has_summary: false,
    msgdb_last_has_originalData: false,
    msgdb_last_has_content: false,
    msgdb_last_fromNick: "",
    msgdb_last_toNick: "",
    msgdb_dump: "",
  };
  window.__qn_standalone_diag = diagnostics;

  function eventIdOf(row) {
    var messageId = String(row.original_msg_id || row.msg_id || "").trim();
    if (messageId && !(row.incomplete && !row.original_msg_id)) {
      var platform = String(row.platform || "taobao").trim().toLowerCase();
      if (platform === "cntaobao" || platform === "qn" || platform === "qianniu") platform = "taobao";
      if (platform === "taobao") return ["qn-msg-v1", platform, messageId].join("|");
    }
    var stableTimestamp = row.original_timestamp || (row.incomplete ? "" : (row.ts || ""));
    return [
      "qn-v6", row.platform || "taobao", row.account || "", row.buyer_id || "",
      row.role || "", row.msg_id || "", stableTimestamp,
    ].join("|");
  }

  function loadOutbox() {
    try {
      var parsed = JSON.parse(window.localStorage && window.localStorage.getItem(OUTBOX_KEY) || "[]");
      if (!Array.isArray(parsed)) return;
      parsed.forEach(function (envelope) {
        var id = envelope && envelope.payload && eventIdOf(envelope.payload);
        if (!id || outbox[id]) return;
        envelope.event_id = id;
        envelope.payload.event_id = id;
        envelope.payload.idempotency_key = id;
        outbox[id] = envelope;
        outboxOrder.push(id);
        rememberSeen(envelope.payload.msg_id);
      });
    } catch (e) {
      console.error("[qn-bridge] outbox load fail", e);
    }
    diagnostics.pending_events = outboxOrder.length;
  }

  function writeOutboxNow() {
    try {
      if (window.localStorage) {
        var batch = outboxOrder.slice(-MAX_OUTBOX).map(function (id) { return outbox[id]; });
        window.localStorage.setItem(OUTBOX_KEY, JSON.stringify(batch));
      }
      return true;
    } catch (e) {
      diagnostics.dropped_while_disconnected += 1;
      console.error("[qn-bridge] CRITICAL outbox persist fail; event retained in memory", e);
      return false;
    }
  }

  function persistOutbox() {
    diagnostics.pending_events = outboxOrder.length;
    if (outboxPersistTimer) return true;
    outboxPersistTimer = setTimeout(function () {
      outboxPersistTimer = null;
      writeOutboxNow();
    }, OUTBOX_PERSIST_DELAY_MS);
    return true;
  }

  function queueEnvelope(row) {
    var id = eventIdOf(row) || row.event_id;
    row.event_id = id;
    row.idempotency_key = id;
    if (!outbox[id]) {
      outbox[id] = { type: "chat_event", event_id: id, payload: row };
      outboxOrder.push(id);
      while (outboxOrder.length > MAX_OUTBOX) {
        var old = outboxOrder.shift();
        delete outbox[old];
        diagnostics.outbox_overflow_dropped += 1;
      }
      persistOutbox();
    }
    return id;
  }

  function acknowledgeEvent(id) {
    id = String(id || "");
    if (!id || !outbox[id]) return;
    delete outbox[id];
    outboxOrder = outboxOrder.filter(function (value) { return value !== id; });
    persistOutbox();
  }

  function flushOutbox(force) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return 0;
    var sent = 0;
    var now = Date.now();
    outboxOrder.slice(0, 200).forEach(function (id) {
      var envelope = outbox[id];
      if (!envelope) return;
      if (!force && envelope.__last_sent_at_ms && now - envelope.__last_sent_at_ms < 2000) return;
      try {
        socket.send(JSON.stringify(envelope));
        envelope.__last_sent_at_ms = now;
        sent += 1;
      } catch (e) {}
    });
    return sent;
  }

  function safeSend(obj) {
    try {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(obj));
        return true;
      }
    } catch (e) {
      console.error("[qn-bridge] send fail", e);
    }
    return false;
  }

  function startHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(function () {
      var snapshot = {
        version: BRIDGE_VERSION,
        started_at_ms: diagnostics.started_at_ms,
        websocket_connected: diagnostics.websocket_connected,
        imsdk_hooked: diagnostics.imsdk_hooked,
        poll_timer_running: diagnostics.poll_timer_running,
        dom_observer_running: diagnostics.dom_observer_running,
        last_poll_at_ms: diagnostics.last_poll_at_ms,
        last_poll_success_at_ms: diagnostics.last_poll_success_at_ms,
        last_event_at_ms: diagnostics.last_event_at_ms,
        last_capture_at_ms: diagnostics.last_capture_at_ms,
        pending_events: diagnostics.pending_events,
        reconnect_count: diagnostics.reconnect_count,
        poll_error_count: diagnostics.poll_error_count,
        dropped_while_disconnected: diagnostics.dropped_while_disconnected,
        outbox_overflow_dropped: diagnostics.outbox_overflow_dropped,
        self_heal_count: diagnostics.self_heal_count,
        incomplete_history_count: diagnostics.incomplete_history_count,
        stale_history_skipped: diagnostics.stale_history_skipped,
        local_db_hits: diagnostics.local_db_hits,
        known_conversations: conversationOrder.length,
        event_callbacks: diagnostics.event_callbacks,
        last_event_raw: diagnostics.last_event_raw,
        last_event_arg_count: diagnostics.last_event_arg_count,
        last_event_arg_types: diagnostics.last_event_arg_types,
        last_event_shape: diagnostics.last_event_shape,
        last_event_ccode_count: diagnostics.last_event_ccode_count,
        local_lookup_attempts: diagnostics.local_lookup_attempts,
        local_lookup_hits: diagnostics.local_lookup_hits,
        local_lookup_misses: diagnostics.local_lookup_misses,
        passive_cache_scans: diagnostics.passive_cache_scans,
        passive_cache_changes: diagnostics.passive_cache_changes,
        passive_cache_last_at_ms: diagnostics.passive_cache_last_at_ms,
        background_notifications: diagnostics.background_notifications,
        remote_fetch_calls: diagnostics.remote_fetch_calls,
        remote_fetch_success: diagnostics.remote_fetch_success,
        remote_fetch_failures: diagnostics.remote_fetch_failures,
        last_background_ccode: diagnostics.last_background_ccode,
        invoke_notify_hooked: diagnostics.invoke_notify_hooked,
        has_onInvokeNotify: diagnostics.has_onInvokeNotify,
        has_task_cache: diagnostics.has_task_cache,
        has_conversationId: diagnostics.has_conversationId,
        has_vs: diagnostics.has_vs,
        current_ccode_hint: diagnostics.current_ccode_hint,
        has_db: diagnostics.has_db,
        has_msgDataMap: diagnostics.has_msgDataMap,
        msgDataMap_size: diagnostics.msgDataMap_size,
        msgDataMap_keys: diagnostics.msgDataMap_keys,
        msgDataMap_sample: diagnostics.msgDataMap_sample,
        msgDataMap_error: diagnostics.msgDataMap_error,
        msgdb_last_arrlen: diagnostics.msgdb_last_arrlen,
        msgdb_last_msgid: diagnostics.msgdb_last_msgid,
        msgdb_last_sendtime: diagnostics.msgdb_last_sendtime,
        msgdb_last_normalized: diagnostics.msgdb_last_normalized,
        msgdb_last_reason: diagnostics.msgdb_last_reason,
        msgdb_last_rawtext: diagnostics.msgdb_last_rawtext,
        msgdb_last_has_summary: diagnostics.msgdb_last_has_summary,
        msgdb_last_has_originalData: diagnostics.msgdb_last_has_originalData,
        msgdb_last_has_content: diagnostics.msgdb_last_has_content,
        msgdb_last_fromNick: diagnostics.msgdb_last_fromNick,
        msgdb_last_toNick: diagnostics.msgdb_last_toNick,
        msgdb_dump: diagnostics.msgdb_dump,
      };
      safeSend({
        type: "hi",
        response: BRIDGE_VERSION,
        diagnostics: snapshot,
      });
      safeSend({ type: "heartbeat", diagnostics: snapshot });
      flushOutbox(true);
    }, 10000);
  }

  function wasSeen(id) {
    id = String(id || "");
    return !!(id && seenMsgIds[id]);
  }

  function rememberSeen(id) {
    id = String(id || "");
    if (!id || seenMsgIds[id]) return;
    seenMsgIds[id] = 1;
    seenOrder.push(id);
    if (seenOrder.length > MAX_SEEN) {
      var old = seenOrder.shift();
      delete seenMsgIds[old];
    }
  }

  function textOf(v) {
    if (v == null) return "";
    if (typeof v === "string") return v.replace(/\s+/g, " ").trim();
    if (typeof v === "number" || typeof v === "boolean") return String(v);
    return "";
  }

  function nickOf(node) {
    if (!node) return "";
    if (typeof node === "string") return textOf(node);
    if (typeof node !== "object") return "";
    return textOf(node.nick || node.display || node.targetId || node.uid || node.userId || "");
  }

  function firstText() {
    for (var i = 0; i < arguments.length; i++) {
      var t = textOf(arguments[i]);
      if (t) return t;
    }
    return "";
  }

  function timestampSeconds(raw) {
    var value = Number(raw) || Date.now();
    // Qianniu fields vary between seconds, milliseconds, microseconds, and
    // nanoseconds. Normalize all of them before upload.
    if (value > 1e17) return Math.floor(value / 1e9);
    if (value > 1e14) return Math.floor(value / 1e6);
    if (value > 1e11) return Math.floor(value / 1e3);
    return Math.floor(value);
  }

  function conversationIdOf(node) {
    if (node == null) return "";
    if (typeof node === "string" || typeof node === "number") {
      var raw = textOf(node);
      if (!raw || raw.length > 300 || raw === "[object Object]") return "";
      if (raw.charAt(0) === "{" || raw.charAt(0) === "[") {
        try { return conversationIdOf(JSON.parse(raw)); } catch (e) { return ""; }
      }
      return raw;
    }
    if (typeof node !== "object") return "";
    return conversationIdOf(
      node.ccode || node.conversationId || node.conversationID ||
      node.conversation_id || node.conversationCode || node.cid || ""
    );
  }

  function registerConversation(raw, source, urgent) {
    var ccode = conversationIdOf(raw);
    if (!ccode) return "";
    var now = Date.now();
    var entry = knownConversations[ccode];
    if (!entry) {
      entry = knownConversations[ccode] = { first_seen_at_ms: now, last_seen_at_ms: now, source: source || "unknown", history_fetched_at_ms: 0 };
      conversationOrder.push(ccode);
      if (conversationOrder.length > MAX_KNOWN_CONVERSATIONS) {
        var removed = conversationOrder.shift();
        delete knownConversations[removed];
        delete passiveCacheFingerprints[removed];
      }
    } else {
      entry.last_seen_at_ms = now;
      if (source) entry.source = source;
    }
    diagnostics.known_conversations = conversationOrder.length;
    return ccode;
  }

  function walkConversationIds(obj, source, depth, found) {
    depth = depth || 0;
    found = found || [];
    if (obj == null || depth > 7) return found;
    if (typeof obj === "string" || typeof obj === "number") {
      var direct = conversationIdOf(obj);
      if (direct && direct.indexOf("#") >= 0 && direct.indexOf("@") >= 0) {
        direct = registerConversation(direct, source, true);
        if (direct && found.indexOf(direct) < 0) found.push(direct);
      }
      return found;
    }
    if (Array.isArray(obj)) {
      for (var i = 0; i < Math.min(obj.length, 120); i++) walkConversationIds(obj[i], source, depth + 1, found);
      return found;
    }
    if (typeof obj !== "object") return found;
    Object.keys(obj).slice(0, 120).forEach(function (key) {
      var value = obj[key];
      var lower = String(key).toLowerCase();
      if (lower === "ccode" || lower === "conversationid" || lower === "conversation_id" || lower === "conversationcode") {
        var ccode = registerConversation(value, source, true);
        if (ccode && found.indexOf(ccode) < 0) found.push(ccode);
      } else if (lower === "cid") {
        var cid = registerConversation(value, source, true);
        if (cid && found.indexOf(cid) < 0) found.push(cid);
        walkConversationIds(value, source, depth + 1, found);
      } else if (value && typeof value === "object") {
        walkConversationIds(value, source, depth + 1, found);
      } else if (typeof value === "string" && (value.charAt(0) === "{" || value.charAt(0) === "[")) {
        try { walkConversationIds(JSON.parse(value), source, depth + 1, found); } catch (e) {}
      }
    });
    return found;
  }

  function extractProduct(detail) {
    var buckets = [];
    function push(x) {
      if (x && typeof x === "object") buckets.push(x);
    }
    push(detail);
    var original = detail && detail.originalData;
    if (typeof original === "string") {
      try { original = JSON.parse(original); } catch (e) { original = null; }
    }
    push(original);
    if (original) {
      ["item", "itemInfo", "goods", "product", "card", "ext", "extra", "bizData", "content"].forEach(function (k) {
        push(original[k]);
      });
      if (Array.isArray(original.jsview)) {
        original.jsview.forEach(function (it) {
          push(it);
          if (it && typeof it === "object") push(it.value);
        });
      }
    }
    var goods = { goods_id: "", goods_name: "", goods_url: "", goods_thumb_url: "", goods_price: "", goods_spec: "" };
    var idKeys = ["goods_id", "product_id", "itemId", "item_id", "itemid", "num_iid", "numIid", "auctionId", "id"];
    var nameKeys = ["goods_name", "product_name", "itemTitle", "item_title", "title", "name", "auctionTitle"];
    var urlKeys = ["goods_url", "product_url", "itemUrl", "item_url", "url", "actionUrl", "pcUrl", "h5Url"];
    var thumbKeys = ["goods_thumb_url", "pic", "picUrl", "pictUrl", "img", "image", "mainPic", "imgUrl"];
    var priceKeys = ["goods_price", "price", "salePrice", "zkFinalPrice", "discountPrice"];
    var specKeys = ["goods_spec", "sku", "skuText", "skuName", "props", "spec"];
    buckets.forEach(function (b) {
      if (!goods.goods_id) {
        for (var i = 0; i < idKeys.length; i++) {
          var cand = b[idKeys[i]];
          if (cand != null && String(cand).match(/^\d{5,20}$/)) {
            if (idKeys[i] === "id" && !(b.title || b.itemTitle || b.pic || b.price || b.itemUrl)) continue;
            goods.goods_id = String(cand);
            break;
          }
        }
      }
      if (!goods.goods_name) goods.goods_name = firstText.apply(null, nameKeys.map(function (k) { return b[k]; }));
      if (!goods.goods_url) goods.goods_url = firstText.apply(null, urlKeys.map(function (k) { return b[k]; }));
      if (!goods.goods_thumb_url) goods.goods_thumb_url = firstText.apply(null, thumbKeys.map(function (k) { return b[k]; }));
      if (!goods.goods_price) goods.goods_price = firstText.apply(null, priceKeys.map(function (k) { return b[k]; }));
      if (!goods.goods_spec) goods.goods_spec = firstText.apply(null, specKeys.map(function (k) { return b[k]; }));
    });
    if (goods.goods_id && !goods.goods_url) goods.goods_url = "https://item.taobao.com/item.htm?id=" + goods.goods_id;
    var out = {};
    Object.keys(goods).forEach(function (k) { if (goods[k]) out[k] = goods[k]; });
    return out;
  }

  function isProductImageUrl(url) {
    // 商品主图走 /bao/uploaded/（宝贝主图），聊天图片走 /imgextra/。两者都是
    // alicdn，但含义完全不同：前者是商品卡携带的咨询商品图，不应被当成买家
    // 发出的图片消息。
    if (!url) return false;
    return /bao\/uploaded/i.test(url);
  }

  function normalizeImageUrl(value) {
    var raw = textOf(value).replace(/&amp;/g, "&");
    if (!raw) return "";
    var match = raw.match(/(?:https?:)?\/\/[^\s\"'<>\\]+/i);
    if (!match) return "";
    var url = match[0].replace(/[),.;]+$/, "");
    if (url.indexOf("//") === 0) url = "https:" + url;
    if (!/^https:\/\//i.test(url) && !/^http:\/\//i.test(url)) return "";
    return url;
  }

  function extractImage(detail) {
    var candidates = [];
    var visited = 0;
    var imageKeys = {
      imageurl: 100, image_url: 100, originurl: 100, originalurl: 100,
      picurl: 95, pic_url: 95, pictureurl: 95, picture_url: 95,
      image: 85, img: 85, pic: 85, url: 70, src: 65,
      thumbnailurl: 50, thumburl: 50,
    };
    function visit(value, key, depth) {
      if (value == null || depth > 9 || visited > 500) return;
      visited += 1;
      if (typeof value === "string") {
        var trimmed = value.trim();
        if ((trimmed.charAt(0) === "{" || trimmed.charAt(0) === "[") && trimmed.length < 100000) {
          try { visit(JSON.parse(trimmed), key, depth + 1); } catch (e) {}
        }
        var url = normalizeImageUrl(value);
        if (!url) return;
        var lowerKey = String(key || "").toLowerCase();
        if (/avatar|head|icon|logo|seller|buyer/.test(lowerKey)) return;
        var hasImageExtension = /\.(?:jpe?g|png|gif|webp|bmp|heic|avif)(?:[?#]|$)/i.test(url);
        var score = imageKeys[lowerKey] || 0;
        var imageHost = /alicdn|taobaocdn|tbcdn|chat-img|im-image/i.test(url);
        if (!hasImageExtension && score < 85 && !imageHost) return;
        if (hasImageExtension) score += 25;
        if (imageHost) score += 10;
        candidates.push({ url: url, score: score });
        return;
      }
      if (Array.isArray(value)) {
        for (var i = 0; i < Math.min(value.length, 100); i++) visit(value[i], key, depth + 1);
        return;
      }
      if (typeof value !== "object") return;
      Object.keys(value).slice(0, 150).forEach(function (childKey) {
        visit(value[childKey], childKey, depth + 1);
      });
    }
    visit(detail, "", 0);
    candidates.sort(function (a, b) { return b.score - a.score; });
    return candidates.length ? candidates[0].url : "";
  }

  function extractText(detail) {
    if (!detail || typeof detail !== "object") return "";
    var t = firstText(detail.summary, detail.text, detail.msg);
    if (t) return t;
    // content 可能是 { text, jsview } 对象（msgdb 顶层 / 千牛新结构），
    // 不能靠 textOf（只认字符串）提取，需递归取 text。
    var contentObj = detail.content;
    if (contentObj && typeof contentObj === "object") {
      t = firstText(contentObj.text, contentObj.content, contentObj.msg, contentObj.summary);
      if (t) return t;
      if (Array.isArray(contentObj.jsview)) {
        for (var ci = 0; ci < contentObj.jsview.length; ci++) {
          var cit = contentObj.jsview[ci] || {};
          var cval = cit.value;
          if (cval && typeof cval === "object") t = firstText(cval.text, cval.content);
          else t = textOf(cval);
          if (t) return t;
        }
      }
    }
    var original = detail.originalData;
    if (typeof original === "string") {
      try { original = JSON.parse(original); } catch (e) { original = null; }
    }
    if (original && typeof original === "object") {
      t = firstText(original.text, original.content, original.msg, original.summary);
      if (t) return t;
      if (Array.isArray(original.jsview)) {
        for (var i = 0; i < original.jsview.length; i++) {
          var it = original.jsview[i] || {};
          var val = it.value;
          if (val && typeof val === "object") t = firstText(val.text, val.content);
          else t = textOf(val);
          if (t) return t;
        }
      }
    }
    var product = extractProduct(detail);
    if (product.goods_name || product.goods_id) {
      var name = product.goods_name || product.goods_id;
      return product.goods_spec ? ("咨询商品：" + name + "（" + product.goods_spec + "）") : ("咨询商品：" + name);
    }
    return "";
  }

  function extractOrder(detail) {
    var buckets = [];
    function push(x) {
      if (x && typeof x === "object") buckets.push(x);
    }
    push(detail);
    var original = detail.originalData;
    if (typeof original === "string") {
      try { original = decodeNestedJson(original); } catch (e) { original = null; }
    }
    push(original);
    push(detail.mcode);
    var content = detail.content;
    if (typeof content === "string") {
      try { content = decodeNestedJson(content); } catch (e) { content = null; }
    }
    push(content);
    ["orderInfo", "order", "trade", "bizData", "data", "ext", "extra"].forEach(function (k) {
      push(detail[k]);
      if (original) push(original[k]);
      if (content && typeof content === "object") push(content[k]);
    });
    var idKeys = ["orderId", "bizOrderId", "biz_order_id", "order_id", "oid", "tid", "tradeId", "bizOrderID", "subOrderId"];
    var priceKeys = ["orderPrice", "payPrice", "payFee", "price", "totalFee"];
    var createKeys = ["createTime", "gmtCreate", "created", "orderCreateTime"];
    var statusKeys = ["orderStatus", "status", "payStatus", "refundStatus", "logisticsStatus"];
    var expressCompanyKeys = ["expressCompany", "logisticsCompany", "companyName"];
    var expressNoKeys = ["expressOrderNumber", "invoiceNo", "mailNo", "logisticsNo"];
    var afterSaleKeys = ["afterSaleText", "afterSaleReason", "refundText"];
    var out = {};
    buckets.forEach(function (b) {
      if (!out.order_id) {
        for (var i = 0; i < idKeys.length; i++) {
          var cand = b[idKeys[i]];
          if (cand != null && String(cand).trim()) {
            out.order_id = String(cand).trim();
            break;
          }
        }
      }
      if (!out.order_price) out.order_price = firstText.apply(null, priceKeys.map(function (k) { return b[k]; }));
      if (!out.create_time) out.create_time = firstText.apply(null, createKeys.map(function (k) { return b[k]; }));
      if (!out.status) out.status = firstText.apply(null, statusKeys.map(function (k) { return b[k]; }));
      if (!out.express_company) out.express_company = firstText.apply(null, expressCompanyKeys.map(function (k) { return b[k]; }));
      if (!out.express_order_number) out.express_order_number = firstText.apply(null, expressNoKeys.map(function (k) { return b[k]; }));
      if (!out.after_sale_text) out.after_sale_text = firstText.apply(null, afterSaleKeys.map(function (k) { return b[k]; }));
      var items = b.itemList || b.items || b.orders;
      if (!out.items && Array.isArray(items)) {
        out.items = items.slice(0, 12).map(function (item) {
          if (item && typeof item === "object") {
            return {
              goods_id: firstText(item.auctionId, item.itemId, item.goods_id),
              goods_name: firstText(item.auctionTitle, item.title, item.itemTitle, item.goods_name),
              goods_url: firstText(item.auctionUrl, item.itemUrl, item.url, item.goods_url),
              goods_price: firstText(item.price, item.auctionPrice, item.goods_price),
            };
          }
          return null;
        }).filter(function (item) { return item && (item.goods_id || item.goods_name); });
      }
    });
    Object.keys(out).forEach(function (k) { if (!out[k]) delete out[k]; });
    return out;
  }

  function normalizeDetail(detail, sellerHint, captureMode) {
    if (!detail || typeof detail !== "object") return null;
    var product = extractProduct(detail);
    var contextOrder = extractOrder(detail);
    var imageUrl = extractImage(detail);
    var productImage = isProductImageUrl(imageUrl);
    var isProduct = !!(product.goods_id || product.goods_name) || productImage;
    var content = extractText(detail);
    if (imageUrl && !isProduct) content = imageUrl;
    if (!content && productImage) content = "咨询商品";
    if (!content) return null;
    var fromid = detail.fromid || detail.fromId || {};
    var toid = detail.toid || detail.toId || {};
    var fromNick = nickOf(fromid);
    var toNick = nickOf(toid);
    var loginid = detail.loginid || detail.loginId || {};
    var loginNick = nickOf(loginid);
    var ccode = conversationIdOf(detail.cid || detail.ccode || "");
    if (ccode) registerConversation(ccode, captureMode, true);
    // loginid is the authoritative current account. Without it, an outbound
    // message can make fromid look like the buyer and reverse the session.
    var seller = textOf(loginNick || sellerHint || lastSellerNick || "");
    var role = "user";
    var buyer = "";
    if (seller && fromNick && fromNick === seller) {
      role = "mall_cs";
      buyer = toNick || ccode;
    } else if (seller && toNick && toNick === seller) {
      role = "user";
      buyer = fromNick || ccode;
    } else {
      buyer = fromNick || ccode || toNick;
      if (!seller) seller = toNick || fromNick;
    }
    if (!buyer) return null;
    var buyerId = ccode || buyer;
    var mcode = detail.mcode && typeof detail.mcode === "object" ? detail.mcode : {};
    var msgId = textOf(mcode.messageId || mcode.clientId || detail.messageId || detail.msg_id || detail.clientId || "");
    var originalMsgId = msgId;
    var tsRaw = detail.sendTime || detail.sortTimeMicrosecond || detail.ts || 0;
    var hasOriginalTimestamp = Number(tsRaw) > 0;
    var ts = timestampSeconds(tsRaw);
    var historyCapture = /GetRemoteHisMsg|GetLocalHisMsg|history_snapshot|event-remote:|event-local-db:|poll:/.test(String(captureMode || ""));
    if (historyCapture && hasOriginalTimestamp && (ts * 1000) < STARTED_AT_MS - RECOVERY_WINDOW_MS) {
      diagnostics.stale_history_skipped += 1;
      return null;
    }
    var incomplete = !msgId || !hasOriginalTimestamp;
    if (!msgId) {
      msgId = "tb-history-incomplete-" + String(seller) + "|" + String(buyerId) + "|" + content.slice(0, 80);
      captureMode = "history_snapshot";
    }
    if (wasSeen(msgId)) return null;
    var row = {
      platform: "taobao",
      role: role,
      content: content,
      account: seller || lastSellerNick || "",
      buyer_id: buyerId,
      buyer_nick: buyer,
      msg_id: msgId,
      ts: ts,
      source: "qianniu_standalone_browser",
      capture_mode: captureMode || "unknown",
      incomplete: incomplete,
      original_msg_id: originalMsgId,
      original_timestamp: hasOriginalTimestamp ? timestampSeconds(tsRaw) : 0,
      qn_bridge_js_version: BRIDGE_VERSION,
      captured_at_ms: Date.now(),
      raw_type: textOf(detail.templateId || detail.templateid || detail.msgType || detail.type || ""),
    };
    if (loginNick) row.seller_identity_source = "loginid";
    if (contextOrder.order_id) {
      row.order_id = contextOrder.order_id;
      row.biz_order_id = contextOrder.order_id;
      row.order_info = Object.assign({ source: "bridge_order_context" }, contextOrder);
      row.order_context = Object.assign({ source: "bridge_order_context" }, contextOrder);
    }
    if (seller) lastSellerNick = seller;
    if (incomplete) {
      diagnostics.incomplete_history_count += 1;
      console.error("[qn-bridge] incomplete history snapshot; not treated as realtime", msgId);
    }
    if (imageUrl && !isProduct) {
      row.image_url = imageUrl;
      row.media_url = imageUrl;
      row.message_type = "image";
      row.template_name = "taobao_image";
      if (!row.raw_type) row.raw_type = "image";
    }
    if (isProduct) Object.keys(product).forEach(function (k) { row[k] = product[k]; });
    if (isProduct) {
      if (productImage && !product.goods_thumb_url) row.goods_thumb_url = imageUrl;
      var productOrderInfo = Object.assign({ source: "bridge_product_context" }, product);
      if (row.order_info && row.order_info.order_id) {
        row.order_info = Object.assign({}, productOrderInfo, row.order_info);
        row.order_context = Object.assign({}, row.order_context || {}, productOrderInfo, row.order_info);
      } else {
        row.order_info = productOrderInfo;
        row.order_context = productOrderInfo;
      }
      if (!row.raw_type) row.raw_type = "taobao_product_card";
    }
    return row;
  }

  function walkDetails(obj, out, depth) {
    depth = depth || 0;
    if (!obj || depth > 8) return;
    if (Array.isArray(obj)) {
      for (var i = 0; i < Math.min(obj.length, 100); i++) walkDetails(obj[i], out, depth + 1);
      return;
    }
    if (typeof obj !== "object") return;
    var hasParty = obj.fromid || obj.toid || obj.fromId || obj.toId || obj.cid;
    var hasBody = obj.summary || obj.originalData || obj.mcode || obj.templateId || obj.text || obj.content;
    var childCollections = [obj.msgs, obj.messages, obj.list, obj.items];
    var hasMessageChildren = childCollections.some(function (value) {
      if (Array.isArray(value)) return value.length > 0;
      if (typeof value === "string" && value.trim().charAt(0) === "[") {
        try {
          var parsed = JSON.parse(value);
          return Array.isArray(parsed) && parsed.length > 0;
        } catch (e) {}
      }
      return false;
    });
    // A conversation container can have a summary plus a list of individual
    // messages. Upload only its children so a history window never becomes one
    // synthetic real-time message.
    if (hasParty && hasBody && !hasMessageChildren) out.push(obj);
    ["data", "result", "bizData", "msgDetail", "message", "originData", "msgs", "messages", "list", "items"].forEach(function (k) {
      var v = obj[k];
      if (typeof v === "string" && (v.charAt(0) === "{" || v.charAt(0) === "[")) {
        try { walkDetails(JSON.parse(v), out, depth + 1); } catch (e) {}
      } else {
        walkDetails(v, out, depth + 1);
      }
    });
  }

  function emitChatPayload(payload, sellerHint, captureMode) {
    var result = { details: 0, captured: 0, sent: 0, ccodes: [] };
    try {
      // A duplicate callback may be the first activity after sleep/network
      // recovery.  Flush its already-durable envelope before deduplication.
      flushOutbox();
      walkConversationIds(payload, captureMode || "payload", 0);
      var details = [];
      walkDetails(payload, details, 0);
      // Never turn an aggregate/window snapshot into one synthetic message.
      // walkDetails only returns individual message-shaped records.
      result.details = details.length;
      details.forEach(function (detail) {
        var row = normalizeDetail(detail, sellerHint, captureMode);
        if (!row) return;
        var eventId = queueEnvelope(row);
        rememberSeen(row.msg_id);
        result.captured += 1;
        var capturedCcode = String(row.buyer_id || "");
        if (capturedCcode.indexOf("#") >= 0 && capturedCcode.indexOf("@") >= 0 && result.ccodes.indexOf(capturedCcode) < 0) {
          result.ccodes.push(capturedCcode);
        }
        if (socket && socket.readyState === WebSocket.OPEN) {
          flushOutbox();
          result.sent += 1;
          diagnostics.sent_events += 1;
          diagnostics.last_capture_at_ms = Date.now();
          diagnostics.last_capture_mode = captureMode || "unknown";
        } else {
          diagnostics.queued_while_disconnected += 1;
          console.warn("[qn-bridge] queued while disconnected", eventId);
        }
      });
    } catch (e) {
      console.error("[qn-bridge] emit fail", e);
    }
    return result;
  }

  function decodeNestedJson(value) {
    var current = value;
    for (var i = 0; i < 3; i++) {
      if (typeof current !== "string") break;
      var trimmed = current.trim();
      if (!trimmed || (trimmed.charAt(0) !== "{" && trimmed.charAt(0) !== "[")) break;
      current = JSON.parse(trimmed);
    }
    return current;
  }

  function eventHandler(eventName) {
    return function (data) {
      diagnostics.event_callbacks += 1;
      diagnostics.last_event_at_ms = Date.now();
      var mode = "event:" + eventName;
      try {
        walkConversationIds(data, mode, 0);
        emitChatPayload({ event: eventName, data: data }, lastSellerNick, mode);
      } catch (e) {
        console.error("[qn-bridge] event handle fail", eventName, e);
      }
    };
  }

  function localMessageValue(ccode) {
    diagnostics.local_lookup_attempts += 1;
    try {
      var mdm = window._db && window._db.msgDataMap;
      if (!mdm || !ccode) {
        diagnostics.local_lookup_misses += 1;
        return null;
      }
      var value = mdm instanceof Map ? (mdm.get(ccode) || null)
        : (typeof mdm === "object" ? (mdm[ccode] || null) : null);
      if (value == null) diagnostics.local_lookup_misses += 1;
      else diagnostics.local_lookup_hits += 1;
      return decodeNestedJson(value);
    } catch (e) {
      diagnostics.local_lookup_misses += 1;
    }
    return null;
  }

  function passiveConversationKeys(limit) {
    try {
      var mdm = window._db && window._db.msgDataMap;
      if (!mdm) return [];
      var allKeys = mdm instanceof Map ? Array.from(mdm.keys())
        : (typeof mdm === "object" ? Object.keys(mdm) : []);
      allKeys = allKeys.map(function (key) { return String(key || ""); }).filter(function (key) {
        return key.length <= 300 && key.indexOf("#") >= 0 && key.indexOf("@") >= 0;
      });
      if (!allKeys.length) {
        passiveCacheCursor = 0;
        return [];
      }
      if (passiveCacheCursor >= allKeys.length) passiveCacheCursor = 0;
      var count = Math.min(Math.max(Number(limit) || 1, 1), allKeys.length);
      var selected = [];
      for (var i = 0; i < count; i++) {
        selected.push(allKeys[(passiveCacheCursor + i) % allKeys.length]);
      }
      passiveCacheCursor = (passiveCacheCursor + count) % allKeys.length;
      return selected;
    } catch (e) {
      return [];
    }
  }

  function passiveCacheFingerprint(value) {
    try {
      var rows = Array.isArray(value) ? value : [value];
      var tail = rows.length ? rows[rows.length - 1] : null;
      if (tail && typeof tail === "object") {
        return JSON.stringify([
          rows.length,
          tail.messageId || tail.msgId || tail.clientId || "",
          tail.sendTime || tail.sortTimeMicrosecond || tail.timestamp || "",
        ]);
      }
      return JSON.stringify([rows.length, tail]);
    } catch (e) {
      return String(value);
    }
  }

  function scanPassiveLocalCache() {
    diagnostics.passive_cache_scans += 1;
    diagnostics.passive_cache_last_at_ms = Date.now();
    passiveConversationKeys(20).forEach(function (rawCcode) {
      var ccode = registerConversation(rawCcode, "passive:msgDataMap", false);
      if (!ccode) return;
      var value = localMessageValue(ccode);
      if (!value) return;
      var fingerprint = passiveCacheFingerprint(value);
      if (passiveCacheFingerprints[ccode] === fingerprint) return;
      passiveCacheFingerprints[ccode] = fingerprint;
      diagnostics.passive_cache_changes += 1;
      emitLocalConversation(ccode);
    });
  }

  function adaptLocalMessage(ccode, record) {
    if (!record || typeof record !== "object") return record;
    var merged = {};
    var layers = [];
    function addLayer(value) {
      try { value = decodeNestedJson(value); } catch (e) { value = null; }
      if (!value || typeof value !== "object" || Array.isArray(value)) return;
      [value.originalData, value.originData, value.originBanamaMessage, value.msgDetail].forEach(addLayer);
      layers.push(value);
    }
    addLayer(record);
    layers.forEach(function (layer) { Object.assign(merged, layer); });
    // The outer cache row owns transport identity and ordering metadata.
    Object.assign(merged, {
      messageId: record.messageId || merged.messageId || "",
      clientId: record.clientId || merged.clientId || "",
      sendTime: record.sendTime || record.sortTimeMicrosecond || merged.sendTime || merged.sortTimeMicrosecond || 0,
    });
    // Nested source objects were merged above. Remove them from the adapted
    // copy so walkDetails cannot emit the same cache row a second time.
    delete merged.originalData;
    delete merged.originData;
    delete merged.originBanamaMessage;
    delete merged.msgDetail;
    merged.cid = merged.cid || { ccode: ccode };
    if (!merged.mcode || typeof merged.mcode !== "object") merged.mcode = {};
    merged.mcode = Object.assign({}, merged.mcode, {
      messageId: record.messageId || merged.mcode.messageId || "",
      clientId: record.clientId || merged.mcode.clientId || "",
    });
    merged.sendTime = merged.sendTime || record.sendTime || record.sortTimeMicrosecond || 0;
    return merged;
  }

  function emitLocalConversation(ccode) {
    var value = localMessageValue(ccode);
    if (!value) return { details: 0, captured: 0, sent: 0, ccodes: [] };
    var rows = Array.isArray(value) ? value : [value];
    var adapted = rows.slice(-100).map(function (record) { return adaptLocalMessage(ccode, record); });
    diagnostics.msgdb_last_arrlen = rows.length;
    diagnostics.msgdb_last_reason = adapted.length ? "adapted_local_cache" : "empty_local_cache";
    diagnostics.msgdb_dump = JSON.stringify(diagnosticShape(adapted.slice(-1), 0)).slice(0, 3000);
    var result = emitChatPayload(
      { source: "msgDataMap", ccode: ccode, data: adapted },
      lastSellerNick,
      "event-local-db:im.singlemsg.onReceiveNewMsg"
    );
    diagnostics.msgdb_last_normalized = !!(result && result.captured);
    if (result && result.captured) diagnostics.local_db_hits += result.captured;
    return result;
  }

  function retryLocalConversation(ccode, attempt) {
    ccode = registerConversation(ccode, "event:im.singlemsg.onReceiveNewMsg", true);
    attempt = attempt || 0;
    if (!ccode || localRetryTimers[ccode]) return;
    diagnostics.last_background_ccode = ccode;
    localRetryTimers[ccode] = setTimeout(function () {
      delete localRetryTimers[ccode];
      var local = emitLocalConversation(ccode);
      if ((!local || !local.sent) && attempt + 1 < LOCAL_RETRY_DELAYS_MS.length) {
        retryLocalConversation(ccode, attempt + 1);
      }
    }, LOCAL_RETRY_DELAYS_MS[attempt]);
  }

  function receiveNewMessageHandler() {
    diagnostics.event_callbacks += 1;
    diagnostics.last_event_at_ms = Date.now();
    var args = Array.prototype.slice.call(arguments);
    diagnostics.last_event_arg_count = args.length;
    diagnostics.last_event_arg_types = args.map(function (value) {
      return Array.isArray(value) ? "array" : (value === null ? "null" : typeof value);
    });
    diagnostics.last_event_shape = JSON.stringify(diagnosticShape(args, 0)).slice(0, 4000);
    diagnostics.last_event_raw = "[redacted; see last_event_shape]";

    var mode = "event:im.singlemsg.onReceiveNewMsg";
    var ccodes = [];
    var capturedCcodes = {};
    var sent = 0;
    args.forEach(function (value) {
      var argCcodes = walkConversationIds(value, mode, 0, []);
      argCcodes.forEach(function (ccode) {
        if (ccodes.indexOf(ccode) < 0) ccodes.push(ccode);
      });
      var result = emitChatPayload({ event: "im.singlemsg.onReceiveNewMsg", data: value }, lastSellerNick, mode);
      if (result && result.captured) {
        sent += result.sent;
        (result.ccodes || []).forEach(function (ccode) { capturedCcodes[ccode] = true; });
      }
    });
    // Only fall back to the foreground conversation when the callback exposed
    // neither a message nor any ccode. Never mix an unrelated foreground chat
    // into a known background notification.
    if (!sent && !ccodes.length) {
      var current = currentCcode();
      if (current) ccodes.push(current);
    }
    diagnostics.last_event_ccode_count = ccodes.length;

    ccodes.forEach(function (ccode) {
      if (capturedCcodes[ccode]) return;
      var local = emitLocalConversation(ccode);
      if (!local || !local.sent) {
        diagnostics.background_notifications += 1;
        retryLocalConversation(ccode);
      }
    });
  }

  function handleOutgoingEvent(eventName, data) {
    var mode = "event:" + eventName;
    diagnostics.event_callbacks += 1;
    diagnostics.last_event_at_ms = Date.now();
    try {
      diagnostics.last_event_raw = JSON.stringify(data);
      if (diagnostics.last_event_raw && diagnostics.last_event_raw.length > 4000) {
        diagnostics.last_event_raw = diagnostics.last_event_raw.slice(0, 4000);
      }
    } catch (e) {
      diagnostics.last_event_raw = String(data);
    }
    walkConversationIds(data, mode, 0);
    var ccode = conversationIdOf(data) || currentCcode();
    if (!ccode && conversationOrder.length) ccode = conversationOrder[conversationOrder.length - 1];
    var result = emitChatPayload({ event: eventName, data: data }, lastSellerNick, mode);
    if (ccode && (!result || !result.sent)) result = emitLocalConversation(ccode);
    if (ccode && (!result || !result.sent)) {
      diagnostics.background_notifications += 1;
      retryLocalConversation(ccode);
    }
  }

  var SDK_EVENTS = [
    "im.singlemsg.onReceiveNewMsg",
    "im.imbamsg.onReceiveNewMsg",
    "im.amptribemsg.onReceiveNewMsg",
    "im.singlemsg.onSendNewMsg",
    "im.singlemsg.onMessageUpdate",
  ];

  function subscribeSdkEvents() {
    if (!window.imsdk || typeof window.imsdk.on !== "function") return false;
    if (hookedImSdk !== window.imsdk) {
      hookedImSdk = window.imsdk;
      window.__qn_standalone_event_hooks = {};
    }
    var hooks = window.__qn_standalone_event_hooks || {};
    window.__qn_standalone_event_hooks = hooks;
    var subscribed = 0;
    SDK_EVENTS.forEach(function (eventName) {
      if (hooks[eventName]) {
        subscribed += 1;
        return;
      }
      try {
        var handler;
        if (eventName === "im.singlemsg.onReceiveNewMsg") {
          handler = receiveNewMessageHandler;
        } else if (eventName === "im.singlemsg.onSendNewMsg" || eventName === "im.singlemsg.onMessageUpdate") {
          handler = function (data) { handleOutgoingEvent(eventName, data); };
        } else {
          handler = eventHandler(eventName);
        }
        // Qianniu's real API takes an event-name array. This is the exact
        // contract used by openbot and works independently of the active chat.
        window.imsdk.on([eventName], handler);
        hooks[eventName] = { handler: handler, sdk: window.imsdk };
        diagnostics.subscriptions[eventName] = "array";
        subscribed += 1;
      } catch (e) {
        diagnostics.subscriptions[eventName] = "failed:" + String(e && e.message ? e.message : e).slice(0, 120);
      }
    });
    console.log("[qn-bridge] array subscriptions", subscribed, SDK_EVENTS.length);
    return subscribed > 0;
  }

  function currentCcode() {
    try {
      if (window._conversationId && window._conversationId.ccode) return String(window._conversationId.ccode);
      var v = window._vs;
      if (v && v.conversationID && v.conversationID.ccode) return String(v.conversationID.ccode);
    } catch (e) {}
    return "";
  }

  function hookImSdk() {
    try {
      if (!window.imsdk) {
        diagnostics.imsdk_hooked = false;
        return false;
      }
      // Observe only. Never replace invoke(), unsubscribe handlers, or call a
      // message-fetch API: those operations can advance Qianniu's UI cursor.
      diagnostics.imsdk_hooked = subscribeSdkEvents();
      installInvokeNotifyHook();
      console.log("[qn-bridge] passive imsdk hook", BRIDGE_VERSION);
      return diagnostics.imsdk_hooked;
    } catch (e) {
      diagnostics.imsdk_hooked = false;
      return false;
    }
  }

  function truncateDiagnosticValue(value, depth, budget) {
    if (budget <= 0 || depth > 4) return "[truncated]";
    if (value == null) return value;
    if (typeof value === "string") {
      return value.length > 240 ? value.slice(0, 240) + "..." : value;
    }
    if (typeof value !== "object") return value;
    var out;
    if (Array.isArray(value)) {
      out = [];
      for (var i = 0; i < Math.min(value.length, 8); i++) {
        out.push(truncateDiagnosticValue(value[i], depth + 1, budget - out.length));
      }
      return out;
    }
    out = {};
    var keys = Object.keys(value).slice(0, 12);
    for (var j = 0; j < keys.length; j++) {
      out[keys[j]] = truncateDiagnosticValue(value[keys[j]], depth + 1, budget - Object.keys(out).length);
    }
    return out;
  }

  function diagnosticSample(value) {
    try {
      var json = JSON.stringify(truncateDiagnosticValue(value, 0, 300));
      return String(json || "").slice(0, 3000);
    } catch (e) {
      try { return String(value).slice(0, 1500); } catch (e2) { return ""; }
    }
  }

  function diagnosticShape(value, depth) {
    depth = depth || 0;
    if (depth > 4) return "truncated";
    if (value == null) return value === null ? "null" : "undefined";
    if (Array.isArray(value)) {
      return { type: "array", length: value.length, items: value.slice(0, 4).map(function (item) {
        return diagnosticShape(item, depth + 1);
      }) };
    }
    if (typeof value === "object") {
      var out = {};
      Object.keys(value).slice(0, 20).forEach(function (key) {
        out[key] = diagnosticShape(value[key], depth + 1);
      });
      return out;
    }
    if (typeof value === "string") return { type: "string", length: value.length };
    return typeof value;
  }

  function probeLocalMsgDb() {
    // Probe the Qianniu local message map; keep this cheap and bounded.
    try {
      var db = window._db;
      diagnostics.has_db = !!db;
      var mdm = db && db.msgDataMap;
      diagnostics.has_msgDataMap = !!(mdm);
      if (!mdm) {
        diagnostics.msgDataMap_size = -1;
        diagnostics.msgDataMap_error = "";
        return;
      }
      var keys = [];
      var cnt = 0;
      var sample = null;
      if (mdm instanceof Map) {
        cnt = mdm.size;
        var it = mdm.entries();
        var step = it.next();
        var scanned = 0;
        while (!step.done && scanned < 20) {
          if (keys.length < 5) keys.push(String(step.value[0]));
          if (sample === null) sample = step.value[1];
          step = it.next();
          scanned += 1;
        }
      } else if (typeof mdm === "object") {
        var allKeys = Object.keys(mdm);
        cnt = allKeys.length;
        keys = allKeys.slice(0, 5);
        sample = allKeys.length ? mdm[allKeys[0]] : null;
      }
      diagnostics.msgDataMap_size = cnt;
      diagnostics.msgDataMap_keys = keys;
      diagnostics.msgDataMap_sample = diagnosticSample(sample);
      diagnostics.msgDataMap_error = "";
    } catch (e) {
      diagnostics.msgDataMap_error = String(e && e.message ? e.message : e).slice(0, 200);
    }
  }

  function installInvokeNotifyHook() {
    // 仅诊断：探测千牛页面里是否有 openbot 依赖的内部变量，供后续决策。
    // 不 wrap window.onInvokeNotify——不同千牛版本该函数的契约不同，冒然
    // hook 可能干扰事件分发（曾导致 onReceiveNewMsg 回调失效）。
    diagnostics.has_onInvokeNotify = typeof window.onInvokeNotify === "function";
    diagnostics.has_task_cache = typeof window.TASK_CACHE !== "undefined";
    diagnostics.has_conversationId = !!(window._conversationId && window._conversationId.ccode);
    diagnostics.has_vs = !!(window._vs);
    diagnostics.current_ccode_hint = currentCcode();
    diagnostics.invoke_notify_hooked = false;
    probeLocalMsgDb();
    return false;
  }

  function scanDomNode(root) {
    if (!root || root.nodeType !== 1) return;
    var attrs = ["data-ccode", "ccode", "data-conversation-id", "conversation-id", "data-conversationid", "conversationid"];
    function scanElement(element) {
      attrs.forEach(function (name) {
        var value = element.getAttribute && element.getAttribute(name);
        if (value) registerConversation(value, "dom:" + name, false);
      });
    }
    scanElement(root);
    try {
      var selector = attrs.map(function (name) { return "[" + name + "]"; }).join(",");
      var nodes = root.querySelectorAll ? root.querySelectorAll(selector) : [];
      for (var i = 0; i < Math.min(nodes.length, 300); i++) scanElement(nodes[i]);
    } catch (e) {}
  }

  function scanConversationDom(force) {
    if (!force && Date.now() - lastDomScanAt < 5000) return;
    lastDomScanAt = Date.now();
    scanDomNode(document.documentElement);
  }

  function startDomObserver() {
    scanConversationDom(true);
    if (typeof MutationObserver !== "function" || !document.documentElement) {
      diagnostics.dom_observer_running = false;
      return;
    }
    if (domObserver && domObserverRoot === document.documentElement) {
      diagnostics.dom_observer_running = true;
      return;
    }
    if (domObserver && typeof domObserver.disconnect === "function") domObserver.disconnect();
    domObserver = new MutationObserver(function (mutations) {
      mutations.slice(0, 60).forEach(function (mutation) {
        for (var i = 0; i < Math.min(mutation.addedNodes.length, 30); i++) scanDomNode(mutation.addedNodes[i]);
      });
    });
    domObserver.observe(document.documentElement, { childList: true, subtree: true });
    domObserverRoot = document.documentElement;
    diagnostics.dom_observer_running = true;
  }

  function passiveRefresh() {
    var startedAt = Date.now();
    diagnostics.last_poll_at_ms = Date.now();
    scanConversationDom(false);
    probeLocalMsgDb();
    scanPassiveLocalCache();
    diagnostics.last_poll_success_at_ms = Date.now();
    diagnostics.last_poll_duration_ms = Date.now() - startedAt;
  }

  function selfHeal(reason) {
    diagnostics.self_heal_count += 1;
    console.warn("[qn-bridge] self-heal", reason || "watchdog");
    // Rebinding the same SDK on every route/visibility/CDP probe can multiply
    // callbacks on Qianniu builds whose off() is incomplete. Only rebind when
    // the SDK object itself was replaced.
    hookImSdk();
    if (domObserver && typeof domObserver.disconnect === "function") domObserver.disconnect();
    domObserver = null;
    domObserverRoot = null;
    diagnostics.dom_observer_running = false;
    startDomObserver();
    passiveRefresh();
    setup();
  }

  function watchdog() {
    var now = Date.now();
    diagnostics.watchdog_last_at_ms = now;
    var disconnected = !socket || (socket.readyState !== WebSocket.OPEN && socket.readyState !== WebSocket.CONNECTING);
    var sdkChanged = hookedImSdk !== window.imsdk;
    var domChanged = domObserverRoot !== document.documentElement;
    if (disconnected || sdkChanged || domChanged || !diagnostics.dom_observer_running) {
      selfHeal(disconnected ? "websocket" : (sdkChanged ? "imsdk" : "dom"));
    } else {
      passiveRefresh();
      flushOutbox();
    }
  }

  function startWatchdog() {
    if (watchdogTimer) clearInterval(watchdogTimer);
    watchdogTimer = setInterval(watchdog, 5000);
    diagnostics.poll_timer_running = true;
  }

  async function runExpression(expression) {
    if (/im\.singlemsg\.(?:GetNewMsg|PeekNewMsg|GetRemoteHisMsg)/i.test(String(expression || ""))) {
      throw new Error("message-fetch APIs are blocked by passive bridge policy");
    }
    return await eval(expression);
  }

  function setup() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    try {
      socket = new WebSocket(WS_URL);
    } catch (e) {
      console.error("[qn-bridge] new WebSocket fail", e);
      scheduleReconnect();
      return;
    }
    socket.onopen = function () {
      console.log("[qn-bridge] connected", WS_URL, BRIDGE_VERSION);
      diagnostics.websocket_connected = true;
      if (everConnected) diagnostics.reconnect_count += 1;
      everConnected = true;
      window.__qn_standalone_socket = socket;
      startHeartbeat();
      hookImSdk();
      passiveRefresh();
      safeSend({ type: "hi", response: BRIDGE_VERSION, diagnostics: diagnostics });
      flushOutbox(true);
    };
    socket.onmessage = async function (event) {
      var param;
      try { param = JSON.parse(event.data); } catch (e) { return; }
      if (!param) return;
      if (param.type === "chat_ack" || param.method === "chat_ack") {
        acknowledgeEvent(param.event_id || (param.response && param.response.event_id));
        return;
      }
      if (param.method !== "execute") return;
      try {
        var res = await runExpression(param.expression);
        safeSend({
          type: "execute",
          request_id: param.request_id,
          response: JSON.stringify(res === undefined ? null : res),
        });
      } catch (err) {
        safeSend({
          type: "execute",
          request_id: param.request_id,
          response: JSON.stringify({ ok: false, err: String(err && err.message ? err.message : err) }),
        });
      }
    };
    socket.onclose = function () {
      diagnostics.websocket_connected = false;
      if (heartbeatTimer) clearInterval(heartbeatTimer);
      if (window.__qn_standalone_socket === socket) window.__qn_standalone_socket = null;
      socket = null;
      scheduleReconnect();
    };
    socket.onerror = function () {
      try { socket.close(); } catch (e) {}
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(setup, 2000);
  }

  function boot() {
    loadOutbox();
    hookImSdk();
    setup();
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (hookImSdk() || tries > 60) clearInterval(timer);
    }, 1000);
    if (invokeNotifyTimer) clearInterval(invokeNotifyTimer);
    invokeNotifyTimer = setInterval(installInvokeNotifyHook, 30000);
    passiveRefresh();
    startDomObserver();
    startWatchdog();
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) selfHeal("visible");
  });
  if (window.addEventListener) {
    window.addEventListener("pageshow", function () { selfHeal("pageshow"); });
    window.addEventListener("popstate", function () { setTimeout(function () { selfHeal("route"); }, 100); });
    window.addEventListener("hashchange", function () { setTimeout(function () { selfHeal("route"); }, 100); });
  }

  if (document.readyState === "complete" || document.readyState === "interactive") {
    setTimeout(boot, 300);
  } else {
    document.addEventListener("DOMContentLoaded", function () { setTimeout(boot, 300); });
  }

  window.__qn_standalone_reconnect = setup;
  window.__qn_standalone_self_heal = selfHeal;
  window.__qn_standalone_add_conversation = function (ccode) { return registerConversation(ccode, "debug", true); };
})();
