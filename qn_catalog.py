"""千牛商品 / 订单取数（复刻福客的 mtop 通道做法）。

福客（fuke / zhihuige.cc）获取淘宝订单与商品的方式不是破解登录，而是
**在千牛页面上下文里执行 `invokeMTopChannelService`**，借用商家自己的
登录态调用千牛客服的 MTOP 接口：

    商品：
        mtop.taobao.qianniu.cs.item.onsale.query     在售商品列表（pageNo/pageSize）
        mtop.taobao.qianniu.cs.item.detail.query     商品详情
        mtop.wdetail.getItemDescx                    商品描述
    订单：
        mtop.taobao.qianniu.cs.trade.query           买家在本店的订单
        mtop.taobao.qianniu.cs.trade.history.query   买家历史订单
        mtop.com.taobao.order.sold.memo              订单备注 / 标旗
        mtop.taobao.qianniu.cs.item.record.query     会话内询价 / 最近购买商品
    店铺：
        mtop.taobao.jdy.resource.shop.info.get       店铺信息（含 tmallSeller 判定）

本模块只做两件事：
  1. 把上面的 mtop 调用固化成稳定 API（带分页、去重、字段归一化）；
  2. 保留 `raw()` 逃生口，方便继续逆向福客用的其它方法。

所有调用都经过页面里的 `window.imsdk.invoke`，因此**不接触账号密码、
不导出 cookie**，与人的手工点击等价，区别只是自动化。

字段口径对齐福客（`work/fuke-taobao-order/report/findings.md`）：
商品取 `data.data.itemList` / `data.data.total`，订单取 `data.data.orders`
（或 `orderList` / `list`）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# mtop 方法名（与福客一致）
# ---------------------------------------------------------------------------
ITEM_ONSALE_METHOD = "mtop.taobao.qianniu.cs.item.onsale.query"
ITEM_DETAIL_METHOD = "mtop.taobao.qianniu.cs.item.detail.query"
ITEM_DESC_METHOD = "mtop.wdetail.getItemDescx"
ITEM_RECORD_METHOD = "mtop.taobao.qianniu.cs.item.record.query"
TRADE_QUERY_METHOD = "mtop.taobao.qianniu.cs.trade.query"
TRADE_HISTORY_METHOD = "mtop.taobao.qianniu.cs.trade.history.query"
ORDER_MEMO_METHOD = "mtop.com.taobao.order.sold.memo"
SHOP_INFO_METHOD = "mtop.taobao.jdy.resource.shop.info.get"

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# 页面侧表达式模板：与福客 `invoke_mtopchannel_service` 等价。
# __PAYLOAD__ 用 json.dumps 结果替换，避免二次转义。
# 同时返回 err / error：桥接转发层取的是 err 字段。
_INVOKE_TEMPLATE = """
(() => {
  const call = __PAYLOAD__;
  const only = call.__only || '';
  const want = name => !only || only === name;
  const channels = [];
  // 通道 1：老接待台的 window.imsdk.invoke（现状可用）
  if (want('imsdk') && window.imsdk && typeof window.imsdk.invoke === 'function') {
    channels.push(['imsdk', () => window.imsdk.invoke(
      'application.invokeMTopChannelService',
      Object.assign({httpMethod: 'post', version: '1.0'}, call)
    )]);
  }
  // 通道 1b：部分客户端注入的是 window.imsdk2
  if (want('imsdk2') && window.imsdk2 && typeof window.imsdk2.invoke === 'function') {
    channels.push(['imsdk2', () => window.imsdk2.invoke(
      'application.invokeMTopChannelService',
      Object.assign({httpMethod: 'post', version: '1.0'}, call)
    )]);
  }
  // 通道 2/3：千牛原生注入的 QN 桥（页面自己的代码就是用它，dx-h5 里也存在）
  const qn = window.QN;
  if (want('qn_app') && qn && qn.app && typeof qn.app.invoke === 'function') {
    channels.push(['qn_app', () => qn.app.invoke({
      api: 'invokeMTopChannelService',
      query: {
        method: call.method,
        version: call.version || '1.0',
        param: JSON.stringify(call.param || {}),
        httpMethod: 'get'
      }
    })]);
  }
  if (want('qn_application') && qn && qn.application && typeof qn.application.invoke === 'function') {
    channels.push(['qn_application', () => qn.application.invoke({
      cmd: 'invokeMTopChannelService',
      param: { method: call.method, version: call.version || '1.0', param: call.param || {} }
    })]);
  }
  // 通道 4：官方 QNAbilityCenter JSSDK（sendMtop，不是把 mtop 方法名当 cmd）
  const center = window.QNAbilityCenter;
  if (want('QNAbilityCenter') && center && center.ability && typeof center.ability.invoke === 'function') {
    channels.push(['QNAbilityCenter', () => new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (!settled) { settled = true; reject(new Error('ability invoke timed out')); }
      }, 6000);
      try {
        center.ability.invoke({
          cmd: 'sendMtop',
          param: {
            method: call.method,
            version: call.version || '1.0',
            param: JSON.stringify(call.param || {})
          },
          success(info) { if (!settled) { settled = true; clearTimeout(timer); resolve(info === undefined ? null : info); } },
          error(err) {
            if (settled) return;
            settled = true; clearTimeout(timer);
            let detail = ''; try { detail = typeof err === 'string' ? err : JSON.stringify(err); } catch (e) { detail = String(err); }
            reject(new Error(detail || 'ability rejected'));
          }
        });
      } catch (e) {
        if (!settled) { settled = true; clearTimeout(timer); reject(e); }
      }
    })]);
  }
  // 通道 5：部分版本挂在 window.abilitycenter 上
  const alt = window.abilitycenter;
  if (want('abilitycenter') && alt && alt.ability && typeof alt.ability.invoke === 'function') {
    channels.push(['abilitycenter', () => new Promise((resolve, reject) => {
      try {
        alt.ability.invoke({
          cmd: call.method, param: call.param,
          success(info) { resolve(info === undefined ? null : info); },
          error(err) { reject(new Error(String(err))); }
        });
      } catch (e) { reject(e); }
    })]);
  }
  if (!channels.length) {
    const missing = 'Qianniu imsdk is unavailable (no mtop channel on this page)';
    return {ok: false, error: missing, err: missing};
  }
  const errors = [];
  const next = index => {
    if (index >= channels.length) {
      const detail = 'all mtop channels failed: ' + errors.join(' | ');
      return {ok: false, error: detail, err: detail, channels: channels.map(c => c[0])};
    }
    const [name, fn] = channels[index];
    return Promise.resolve()
      .then(fn)
      .then(value => {
        // 某些页面的 imsdk2 会 resolve(null)，这不是 mtop 响应。
        // 必须在同一个页面继续走 qn_app，而不是把空值交给外层
        // result_filter；否则广播只会等其它页面，最后误报超时/空结果。
        const empty = value === null || value === undefined ||
          (typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 0) ||
          (typeof value === 'string' && !value.trim());
        if (empty) {
          errors.push(name + ': empty response');
          return next(index + 1);
        }
        return {ok: true, channel: name, value: value};
      }, error => {
        errors.push(name + ': ' + String(error && error.message ? error.message : error).slice(0, 120));
        return next(index + 1);
      });
  };
  return next(0);
})()
"""

class QnCatalogError(RuntimeError):
    """mtop 调用失败（页面不可用 / 接口返回非 SUCCESS）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def decode(value: Any, depth: int = 5) -> Any:
    """mtop 有些字段是「JSON 字符串里再套 JSON」，逐层解开。"""
    current = value
    for _ in range(depth):
        if not isinstance(current, str):
            break
        text = current.strip()
        if not text or text[0] not in "[{":
            break
        try:
            current = json.loads(text)
        except ValueError:
            break
    return current


def first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value not in (None, "", [], {}):
                return value
    return None


def collect_lists(value: Any, keys: Iterable[str], depth: int = 0) -> list[list[Any]]:
    """在任意深度的响应里收集名字命中 keys 的数组（mtop 包装层不固定）。"""
    wanted = set(keys)
    found: list[list[Any]] = []
    value = decode(value)
    if depth > 7:
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(collect_lists(item, wanted, depth + 1))
        return found
    if not isinstance(value, dict):
        return found
    for key, item in value.items():
        if key in wanted:
            item = decode(item)
            if isinstance(item, list):
                found.append(item)
        found.extend(collect_lists(item, wanted, depth + 1))
    return found


def mtop_ret(value: Any) -> list[str]:
    value = decode(value)
    if not isinstance(value, dict):
        return []
    ret = value.get("ret")
    if isinstance(ret, str):
        return [ret]
    if isinstance(ret, list):
        return [str(item) for item in ret]
    return []


def mtop_ok(value: Any) -> bool:
    """判断 mtop 是否至少返回了一个有效响应对象。

    某些页面桥（现场的 imsdk2）会 Promise.resolve(null)。旧逻辑把
    "没有 ret" 一律当成功，导致 null/{} 抢在真正的 QN.app 通道前面，
    对外显示 ok=true、订单商品却都是 0。空响应必须失败；没有 ret 的
    非空对象仍允许通过，因为 QN.app 的包装响应可能只带 data/api。
    """
    value = decode(value)
    if not isinstance(value, dict) or not value:
        return False
    ret = mtop_ret(value)
    if not ret:
        return True
    return ret[0].startswith("SUCCESS")


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def find_number(value: Any, keys: Iterable[str], depth: int = 0) -> int | None:
    """在多层 data 包装里找数字型总量（福客口径：data.data.total）。"""
    wanted = set(keys)
    value = decode(value)
    if depth > 5:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if key in wanted:
                if isinstance(item, bool):
                    continue
                if isinstance(item, int):
                    return item
                if isinstance(item, str) and item.strip().isdigit():
                    return int(item.strip())
        for item in value.values():
            found = find_number(item, wanted, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_number(item, wanted, depth + 1)
            if found is not None:
                return found
    return None


def find_dict(value: Any, key: str, depth: int = 0) -> dict[str, Any] | None:
    """在多层包装里找第一个名字命中的 dict（如 detail 的 `item`）。"""
    value = decode(value)
    if depth > 5:
        return None
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            return candidate
        for item in value.values():
            found = find_dict(item, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_dict(item, key, depth + 1)
            if found is not None:
                return found
    return None


def normalize_goods(http_value: Any) -> dict[str, Any]:
    """把 onsale/detail 响应归一化成 {goods_id, 名称, 价格, 库存...}。"""
    item_lists = collect_lists(http_value, {"itemList", "items", "item_list", "itemDOList"})
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in item_lists:
        for raw in bucket:
            row = decode(raw)
            if not isinstance(row, dict):
                continue
            goods_id = str(
                first(row, "itemId", "item_id", "auctionId", "auction_id", "id", "goodsId") or ""
            ).strip()
            name = str(
                first(row, "title", "itemTitle", "auctionTitle", "name", "goodsName") or ""
            ).strip()
            if not goods_id and not name:
                continue
            identity = goods_id or name
            if identity in seen:
                continue
            seen.add(identity)
            url = str(first(row, "itemUrl", "auctionUrl", "pcUrl", "url", "detailUrl") or "").strip()
            if goods_id and not url:
                url = f"https://item.taobao.com/item.htm?id={goods_id}"
            rows.append({
                key: value for key, value in {
                    "goods_id": goods_id,
                    "goods_name": name,
                    "goods_url": url,
                    "goods_thumb_url": str(
                        first(row, "picUrl", "pictUrl", "pic", "image", "mainImage", "imgUrl") or ""
                    ),
                    "goods_price": str(
                        first(row, "price", "auctionPrice", "itemPrice", "zkFinalPrice", "salePrice") or ""
                    ),
                    "goods_origin_price": str(first(row, "originPrice", "reservePrice", "stdPrice") or ""),
                    "stock": first(row, "quantity", "stock", "sellableQuantity", "inventory"),
                    "sold_count": first(row, "soldQuantity", "soldCnt", "tradeNum", "sold_count"),
                    "category_id": str(first(row, "categoryId", "cid", "category_id") or ""),
                    "status": first(row, "status", "itemStatus", "onsale"),
                    "raw": _jsonable(row),
                }.items() if value not in (None, "", [], {})
            })
    total = find_number(http_value, ("total", "totalCount", "totalNum"))
    return {"goods": rows, "total": total}


def normalize_order_items(raw_items: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_items = decode(raw_items)
    if not isinstance(raw_items, list):
        return rows
    for raw in raw_items[:50]:
        item = decode(raw)
        if not isinstance(item, dict):
            continue
        rows.append({
            key: value for key, value in {
                "goods_id": str(first(item, "auctionId", "itemId", "goodsId", "numIid") or ""),
                "goods_name": str(first(item, "auctionTitle", "title", "itemTitle", "goodsName") or ""),
                "goods_url": str(first(item, "auctionUrl", "itemUrl", "url") or ""),
                "goods_thumb_url": str(first(item, "picUrl", "pictUrl", "pic", "snapshotUrl") or ""),
                "goods_price": str(first(item, "price", "auctionPrice", "itemPrice") or ""),
                "goods_old_price": str(first(item, "oldPrice", "reservePrice") or ""),
                "goods_spec": str(first(item, "sku", "skuText", "props", "skuInfo") or ""),
                "outer_id": str(first(item, "outerId", "itemCode") or ""),
                "quantity": first(item, "buyAmount", "quantity", "num"),
                "refund_status": first(item, "refundStatus", "refund_status"),
                "pay_status": first(item, "payStatus", "pay_status"),
                "logistics_status": first(item, "logisticsStatus", "logistics_status"),
                "sub_order_id": str(first(item, "subOrderId", "sub_order_id", "oid") or ""),
            }.items() if value not in (None, "", [], {})
        })
    return rows


def normalize_orders(http_value: Any) -> list[dict[str, Any]]:
    """订单归一化。字段口径对齐福客 `parse_orders`。"""
    buckets = collect_lists(http_value, {"orders", "orderList", "order_list", "list", "tradeList"})
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in buckets:
        for raw in bucket:
            row = decode(raw)
            if not isinstance(row, dict):
                continue
            order_id = str(first(row, "bizOrderId", "biz_order_id", "orderId", "order_id", "tid", "id") or "").strip()
            if not order_id or order_id in seen:
                continue
            seen.add(order_id)
            items = normalize_order_items(first(row, "itemList", "items", "subOrders"))
            # trade.query 的订单级状态分散在 category / cardTypeText，物流与支付在子单上。
            single_item = items[0] if len(items) == 1 else {}
            order = {
                "order_id": order_id,
                "order_status_text": first(row, "category", "orderStatusText", "statusText", "statusDesc")
                or "",
                "evaluation_status": first(row, "cardTypeText"),
                "buyer_rate_status": first(row, "buyerRateStatus"),
                "pay_status": first(row, "payStatus", "pay_status") or single_item.get("pay_status"),
                "logistics_status": first(row, "logisticsStatus", "logistics_status")
                or single_item.get("logistics_status"),
                "refund_status": first(row, "refundStatus", "refund_status")
                or single_item.get("refund_status"),
                "create_time": first(row, "createTime", "gmtCreate"),
                "pay_time": first(row, "payTime"),
                "consign_time": first(row, "consignTime"),
                "end_time": first(row, "endTime", "end_time"),
                "order_price": first(row, "orderPrice", "totalFee", "payAmount"),
                "post_fee": first(row, "postFee", "post_fee"),
                "adjust_fee": first(row, "adjustFee"),
                "refund_fee": first(row, "refundFee"),
                "promotion_total_fee": first(row, "promotionTotalFee"),
                "buy_amount": first(row, "buyAmount"),
                "express_company": first(row, "expressCompany", "logisticsCompany"),
                "express_order_number": first(row, "expressOrderNumber", "invoiceNo", "mailNo"),
                "receiver_name": first(row, "receiverName"),
                "receiver_phone": first(row, "receiverMobilePhone"),
                "receiver_address": first(row, "receiverAddress"),
                "after_sale_text": first(row, "afterSaleText"),
                "seller_flag": first(row, "sellerFlag"),
                "seller_memo": first(row, "sellerMemo"),
                "under_inquiry": first(row, "underInquiry"),
                "risk_order": first(row, "riskOrder"),
                "buyer_nick": first(row, "buyerNick", "buyer_nick", "nick"),
                "item_count": len(items),
                "items": items,
                "raw": _jsonable(row),
            }
            rows.append({key: value for key, value in order.items() if value not in (None, "", [], {})})
    return rows


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------
class QnCatalog:
    """通过千牛页面执行 mtop，取商品目录与订单。

    传入的 `app` 需要满足：`app.browser.execute(expression, timeout=...)`。
    这与 `ContextEnricher` 用的是同一条通道（页面 `window.imsdk.invoke`）。
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.requests = 0
        self.last_channel = ""
        self.last_error = ""
        self.last_success_at = 0.0

    # -- 底层 -------------------------------------------------------------
    def _timeout(self, override: float | None = None) -> float:
        if override is not None:
            return float(override)
        try:
            return float(self.app.config.get("catalog_timeout_seconds", 8.0))
        except (AttributeError, TypeError, ValueError):
            return 8.0

    def raw(self, method: str, param: dict[str, Any] | None = None, timeout: float | None = None,
            strict: bool = True, version: str = "1.0",
            only_channel: str = "") -> Any:
        """执行任意 mtop 方法（福客 `invoke_mtopchannel_service` 的等价物）。

        strict=False 时不因 `ret` 非 SUCCESS 报错，直接返回原始响应，
        方便探索新方法（配合 `mtop_ret()` 判断成败）。
        `version` 要跟方法对齐：例如 `mtop.wdetail.getItemDescx` 必须用 "4.1"。
        """
        payload = {
            "method": str(method),
            "param": _jsonable(param or {}),
            "version": str(version),
        }
        if only_channel:
            payload["__only"] = str(only_channel)
        expression = _INVOKE_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
        self.requests += 1
        def usable(value: Any) -> bool:
            """只有"真能取数"的页面才算命中。

            多页面客户端里 `execute` 是广播、先答者胜：空壳页（无 mtop 通道）
            会秒答 {ok:false}，把真正能取单的页面挡掉 —— 这正是
            "同一个账号 1.6.27 能用、升级后取不到" 的根因。
            """
            if not isinstance(value, dict) or value.get("ok") is not True:
                return False
            return not strict or mtop_ok(value.get("value"))

        try:
            result = self.app.browser.execute(
                expression, timeout=self._timeout(timeout),
                result_filter=usable, raise_on_no_match=True,
            )
        except Exception as error:  # 页面未连接 / 超时 / 页面报错（如 imsdk 未加载）
            self.last_error = str(error) or "Qianniu browser command failed"
            raise QnCatalogError(self.last_error) from error
        if not isinstance(result, dict):
            self.last_error = f"Qianniu mtop response is not an object: {type(result).__name__}"
            raise QnCatalogError(self.last_error)
        if result.get("ok") is not True:
            self.last_error = str(result.get("error") or "Qianniu mtop invoke failed")
            raise QnCatalogError(self.last_error)
        channel = str(result.get("channel") or "imsdk")
        self.last_channel = channel
        value = result.get("value")
        if strict and not mtop_ok(value):
            self.last_error = ";".join(mtop_ret(value)) or "mtop returned non-success"
            raise QnCatalogError(self.last_error)
        self.last_error = ""
        self.last_success_at = time.time()
        return value

    # -- 商品 -------------------------------------------------------------
    def goods_page(self, page_no: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
                   timeout: float | None = None, only_channel: str = "") -> dict[str, Any]:
        """一页在售商品（福客 `getQNList1` 的分页口径）。"""
        page_no = max(1, int(page_no))
        page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
        value = self.raw(
            ITEM_ONSALE_METHOD,
            {"pageNo": page_no, "pageSize": page_size},
            timeout=timeout,
            only_channel=only_channel,
        )
        parsed = normalize_goods(value)
        parsed.update({
            "page_no": page_no,
            "page_size": page_size,
            "method": ITEM_ONSALE_METHOD,
            "has_more": bool(parsed["goods"]) and (
                parsed["total"] is None or page_no * page_size < int(parsed["total"])
            ),
        })
        return parsed

    def goods_all(self, max_pages: int = 20, page_size: int = DEFAULT_PAGE_SIZE,
                  timeout: float | None = None) -> dict[str, Any]:
        """翻页取全量在售商品，按 goods_id 去重。"""
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        pages = 0
        for page_no in range(1, max(1, int(max_pages)) + 1):
            page = self.goods_page(page_no, page_size, timeout=timeout)
            pages = page_no
            if page.get("total") is not None:
                total = int(page["total"])
            batch = page["goods"]
            if not batch:
                break
            for row in batch:
                identity = row.get("goods_id") or row.get("goods_name") or ""
                if identity in seen:
                    continue
                seen.add(identity)
                collected.append(row)
            if len(batch) < page_size:
                break
            if total is not None and len(collected) >= total:
                break
        return {
            "goods": collected,
            "count": len(collected),
            "total": total,
            "pages": pages,
            "method": ITEM_ONSALE_METHOD,
            "complete": bool(total is None or len(collected) >= total),
        }

    def goods_detail(self, item_id: str, buyer_encrypt_id: str = "",
                     timeout: float | None = None) -> dict[str, Any]:
        """商品详情。福客的真实参数：encryptId + isNewCustomer + itemId。"""
        param: dict[str, Any] = {"itemId": str(item_id), "isNewCustomer": True}
        if buyer_encrypt_id:
            param["encryptId"] = str(buyer_encrypt_id)
        try:
            value = self.raw(ITEM_DETAIL_METHOD, param, timeout=timeout)
        except QnCatalogError as error:
            return {"goods_id": str(item_id), "detail": {}, "method": ITEM_DETAIL_METHOD,
                    "error": str(error)[:300]}
        parsed = normalize_goods(value)
        item = find_dict(value, "item") or {}
        detail = parsed["goods"][0] if parsed["goods"] else {}
        if isinstance(item, dict):
            detail = {
                **detail,
                **{key: value for key, value in {
                    "goods_id": str(item.get("itemId") or "" ) or str(item_id),
                    "goods_name": item.get("title"),
                    "goods_thumb_url": item.get("pic"),
                    "goods_url": item.get("itemUrl"),
                }.items() if value not in (None, "")},
            }
        return {
            "goods_id": str(item_id),
            "detail": detail,
            "props_name": find_dict(value, "data") and (decode(find_dict(value, "data")) or {}).get("propsName"),
            "method": ITEM_DETAIL_METHOD,
            "version": "1.0",
        }

    def goods_desc(self, item_id: str, timeout: float | None = None) -> dict[str, Any]:
        """商品详情页图片/描述。福客：mtop.wdetail.getItemDescx + version 4.1 + item_num_id。"""
        value = self.raw(
            ITEM_DESC_METHOD,
            {"item_num_id": str(item_id)},
            timeout=timeout,
            version="4.1",
        )
        holder = decode(value) or {}
        images: list[str] = []
        seen: set[str] = set()
        for bucket in collect_lists(holder, {"images", "imageList"}):
            for entry in bucket:
                entry = decode(entry)
                if isinstance(entry, str):
                    url = entry
                elif isinstance(entry, dict):
                    url = first(entry, "url", "picUrl", "imageUrl") or ""
                else:
                    url = ""
                url = str(url).strip()
                if url and url not in seen:
                    seen.add(url)
                    images.append(url)
        return {"goods_id": str(item_id), "images": images, "method": ITEM_DESC_METHOD, "version": "4.1"}

    # -- 订单 -------------------------------------------------------------
    def buyer_orders(self, buyer_encrypt_id: str, biz_order_id: str = "",
                     timeout: float | None = None, only_channel: str = "") -> dict[str, Any]:
        """买家在本店的近期订单（福客订单上下文用的就是这个）。"""
        encrypt_id = str(buyer_encrypt_id or "").strip()
        if not encrypt_id:
            raise QnCatalogError("buyer_encrypt_id is required")
        value = self.raw(
            TRADE_QUERY_METHOD,
            {"securityBuyerUid": encrypt_id, "bizOrderId": str(biz_order_id or "")},
            timeout=timeout,
            only_channel=only_channel,
        )
        orders = normalize_orders(value)
        return {
            "orders": orders,
            "count": len(orders),
            "buyer_encrypt_id": encrypt_id,
            "method": TRADE_QUERY_METHOD,
            "raw": _jsonable(value),
        }

    def buyer_order_history(self, buyer_encrypt_id: str, page_num: int = 1,
                            page_size: int = DEFAULT_PAGE_SIZE,
                            timeout: float | None = None) -> dict[str, Any]:
        """买家历史订单（`context_enrich_history_orders` 打开后福客也用这条）。"""
        encrypt_id = str(buyer_encrypt_id or "").strip()
        if not encrypt_id:
            raise QnCatalogError("buyer_encrypt_id is required")
        page_num = max(1, int(page_num))
        page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
        value = self.raw(
            TRADE_HISTORY_METHOD,
            {"securityBuyerUid": encrypt_id, "pageNum": page_num, "pageSize": page_size},
            timeout=timeout,
        )
        orders = normalize_orders(value)
        return {
            "orders": orders,
            "count": len(orders),
            "page_num": page_num,
            "page_size": page_size,
            "buyer_encrypt_id": encrypt_id,
            "method": TRADE_HISTORY_METHOD,
            "raw": _jsonable(value),
        }

    def order_memo(self, order_id: str, timeout: float | None = None) -> dict[str, Any]:
        """订单备注 / 标旗。福客真实参数：operation=query + orderId（传订单号）。"""
        order_id = str(order_id or "").strip()
        if not order_id:
            raise QnCatalogError("order_id is required")
        value = self.raw(
            ORDER_MEMO_METHOD,
            {"operation": "query", "orderId": order_id},
            timeout=timeout or 5.0,
        )
        holder = decode(value) or {}
        data = decode(holder.get("data")) if isinstance(holder, dict) else {}
        rows: list[dict[str, Any]] = []
        if isinstance(data, dict):
            for bucket in collect_lists(data, {"editRecordList", "recordList"}):
                for raw in bucket:
                    row = decode(raw)
                    if not isinstance(row, dict):
                        continue
                    rows.append({
                        key: item for key, item in {
                            "memo": first(row, "memoContent", "memo", "content", "remark"),
                            "flag_tag": first(row, "flagTag", "tagName", "flagName", "tag"),
                            "updated_at": first(row, "gmtModified", "updateTime", "modified"),
                            "operator": first(row, "operator", "operatorNick"),
                        }.items() if item not in (None, "", [], {})
                    })
            tags: list[dict[str, Any]] = []
            for bucket in collect_lists(data, {"flagTagList"}):
                for raw in bucket:
                    row = decode(raw)
                    if isinstance(row, dict):
                        tags.append({
                            key: item for key, item in {
                                "flag_id": row.get("flagId"),
                                "latest_tag": row.get("latestTag"),
                                "flag_pic": row.get("flagPicUrl"),
                            }.items() if item not in (None, "")
                        })
        else:
            tags = []
        return {
            "order_id": order_id,
            "memos": rows,
            "flag_tags": tags,
            "method": ORDER_MEMO_METHOD,
            "params_used": {"operation": "query", "orderId": order_id},
        }

    # -- 店铺 -------------------------------------------------------------
    def shop_info(self, timeout: float | None = None) -> dict[str, Any]:
        value = self.raw(SHOP_INFO_METHOD, {}, timeout=timeout)
        result = decode(value)
        result = result.get("data") if isinstance(result, dict) else {}
        result = decode(result)
        holder = result.get("result") if isinstance(result, dict) else {}
        holder = decode(holder) if holder else (result if isinstance(result, dict) else {})
        tmall = None
        if isinstance(holder, dict):
            tmall = holder.get("tmallSeller")
        return {
            "shop_name": first(holder, "shopName", "shop_title", "nick", "sellerNick") if isinstance(holder, dict) else None,
            "seller_nick": first(holder, "sellerNick", "nick") if isinstance(holder, dict) else None,
            "is_tmall": bool(tmall) if tmall is not None else None,
            "method": SHOP_INFO_METHOD,
            "raw": _jsonable(value),
        }

    # -- 诊断 -------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "methods": {
                "goods": ITEM_ONSALE_METHOD,
                "goods_detail": ITEM_DETAIL_METHOD,
                "goods_desc": ITEM_DESC_METHOD,
                "item_record": ITEM_RECORD_METHOD,
                "buyer_orders": TRADE_QUERY_METHOD,
                "buyer_history": TRADE_HISTORY_METHOD,
                "order_memo": ORDER_MEMO_METHOD,
                "last_channel": self.last_channel,
                "shop_info": SHOP_INFO_METHOD,
            },
        }


class CatalogSyncer:
    """把在售商品目录定时沉凌到本地 SQLite，供工作台与 AI 检索。

    设计要点（对齐福客 `js_sync_job` 的意图，但更保守）：
      - 只在页面已连接且无进行中任务时跑，避免与聊天取数抢 mtop 通道；
      - 分批翻页，每批入库一次，失败只记错误并等下一轮，不影响主流程；
      - 默认开，间隔 30 分钟，页大小 100，最多 20 页（≈2000 款）。
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self.thread = threading.Thread(target=self.run, name="catalog-sync", daemon=True)
        self.wakeup = threading.Event()
        self.lock = threading.Lock()
        self.runs = 0
        self.failures = 0
        self.last_run_at = 0.0
        self.last_success_at = 0.0
        self.last_error = ""
        self.last_written = 0
        self.last_total: int | None = None
        self.syncing = False

    # -- 配置 -----------------------------------------------------------
    def _config(self) -> dict[str, Any]:
        get = getattr(self.app.config, "get", None)
        if not callable(get):
            return {}
        return {
            "enabled": bool(get("catalog_sync_enabled", True)),
            "interval": max(60.0, float(get("catalog_sync_interval_seconds", 1800) or 1800)),
            "page_size": max(1, min(MAX_PAGE_SIZE, int(get("catalog_sync_page_size", MAX_PAGE_SIZE) or MAX_PAGE_SIZE))),
            "max_pages": max(1, min(200, int(get("catalog_sync_max_pages", 20) or 20))),
        }

    # -- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        self.thread.start()

    def request_sync(self) -> None:
        self.wakeup.set()

    def run(self) -> None:
        stop_event = getattr(self.app, "stop_event", None)
        # 启动后先等 20s，避开启动时与消息采集/订单富化的高峰
        if stop_event is not None and stop_event.wait(20.0):
            return
        while True:
            cfg = self._config()
            if cfg.get("enabled", True):
                try:
                    self.sync_once()
                except Exception as error:  # noqa: BLE001 - 后台任务不允许把进程拉挂
                    self.failures += 1
                    self.last_error = str(error)[:300]
            stop_event = getattr(self.app, "stop_event", None)
            interval = self._config().get("interval", 1800.0)
            # 这里必须等 wakeup：原来只 wait(stop_event, interval)，工作台点
            # 「同步」设置的 wakeup 永远不会生效，只能等 30 分钟一轮。
            deadline = time.monotonic() + interval
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self.wakeup.wait(min(remaining, 5.0)):
                    break
                if stop_event is not None and stop_event.is_set():
                    return
            self.wakeup.clear()

    # -- 干活 -----------------------------------------------------------
    def sync_once(self) -> dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "error": "sync already running"}
        try:
            self.syncing = True
            self.runs += 1
            self.last_run_at = time.time()
            browser = getattr(self.app, "browser", None)
            if browser is None or not getattr(browser, "connected", 0):
                self.last_error = "Qianniu browser bridge is not connected"
                return {"ok": False, "error": self.last_error}
            cfg = self._config()
            written = 0
            total: int | None = None
            pages = 0
            for page_no in range(1, int(cfg["max_pages"]) + 1):
                page = self.app.catalog.goods_page(page_no, int(cfg["page_size"]))
                pages = page_no
                if page.get("total") is not None:
                    total = int(page["total"])
                batch = page.get("goods") or []
                if not batch:
                    break
                written += self.app.db.catalog_upsert_goods(batch)
                if total is not None and written >= total:
                    break
                if len(batch) < int(cfg["page_size"]):
                    break
            self.last_written = written
            self.last_total = total
            self.last_success_at = time.time()
            self.last_error = ""
            summary = {
                "ok": True,
                "written": written,
                "total": total,
                "pages": pages,
                "at": self.last_success_at,
            }
            self.app.db.catalog_set_meta("last_sync", summary)
            return summary
        finally:
            self.syncing = False
            self.lock.release()

    def status(self) -> dict[str, Any]:
        cfg = self._config()
        return {
            "enabled": cfg.get("enabled"),
            "interval_seconds": cfg.get("interval"),
            "page_size": cfg.get("page_size"),
            "max_pages": cfg.get("max_pages"),
            "syncing": self.syncing,
            "runs": self.runs,
            "failures": self.failures,
            "last_run_at": self.last_run_at,
            "last_success_at": self.last_success_at,
            "last_written": self.last_written,
            "last_total": self.last_total,
            "last_error": self.last_error,
        }


__all__ = [
    "QnCatalog",
    "QnCatalogError",
    "CatalogSyncer",
    "normalize_goods",
    "normalize_orders",
    "normalize_order_items",
    "mtop_ok",
    "mtop_ret",
    "collect_lists",
    "find_number",
    "find_dict",
    "decode",
    "ITEM_ONSALE_METHOD",
    "ITEM_DETAIL_METHOD",
    "ITEM_DESC_METHOD",
    "ITEM_RECORD_METHOD",
    "TRADE_QUERY_METHOD",
    "TRADE_HISTORY_METHOD",
    "ORDER_MEMO_METHOD",
    "SHOP_INFO_METHOD",
]
