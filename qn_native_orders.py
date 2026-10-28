"""从千牛客户端原生日志里取订单（不依赖页面 JS 桥）。

背景（2026-10-28 现场结论）：

  9.98.49N 的新版 DX 接待台（dx-h5）webview 里没有 `workbench` /
  `imsdk` / `QN`，任何页面级 mtop 都发不出去，助手因此在那种机器上
  永远取不到订单。但千牛**客户端自己在原生层**渲染接待列表时，会调：

      mtop.taobao.qianniu.airisland.reception.detail.get

  并把响应原文写进 `AliWorkbenchData\\System\\log\\app.log`：

      data={"api":"...reception.detail.get","data":{"data":[
        {"buyerId":"2201250451713","snsNick":"wangchenyu2660",
         "tradeSimpleList":[{"bizOrderId":5127371283219050503,
                             "logisticsStatus":1,"payStatus":4,
                             "modifiedTime":1792622992000}]}]}}

  也就是说：**订单数据一直在本机日志里流，跟接待台形态无关。**
  本模块只读 tail 这个日志，把 buyerId -> 订单列表建成缓存，
  供 /api/v1/catalog/orders 与订单上下文做兜底。

约束：
  * 只读文件，不改千牛、不发消息、不模拟键鼠；
  * 启动时先 seek 到文件末尾（不重放历史），之后增量读取；
  * 单行 JSON 解析失败只计数，不影响其它行。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("qianniu_standalone.native_orders")

RECEPTION_API = "mtop.taobao.qianniu.airisland.reception.detail.get"

# `data=` / `#=` 之后是完整 JSON 对象；行尾还有 `]` 之类的包装，故用 raw_decode。
_JSON_START = re.compile(r'(?:data=|#=)(\{"api":"[^"]+")')
MAX_BUYERS = 5000
BACKFILL_BYTES = 8 * 1024 * 1024
MAX_ORDERS_PER_BUYER = 50


def normalize_order(raw: dict[str, Any]) -> dict[str, Any]:
    """把 tradeSimpleList 的一条规整成助手内部订单结构。"""
    order_id = str(raw.get("bizOrderId") or raw.get("orderId") or "").strip()
    modified = raw.get("modifiedTime")
    try:
        modified_at = float(modified) / 1000.0 if modified else 0.0
    except (TypeError, ValueError):
        modified_at = 0.0
    return {
        "order_id": order_id,
        "biz_order_id": order_id,
        "pay_status": raw.get("payStatus"),
        "logistics_status": raw.get("logisticsStatus"),
        "modified_time": modified,
        "modified_at": modified_at,
        "source": "qianniu_native_reception",
    }


def parse_line(line: str) -> list[dict[str, Any]]:
    """解析一行日志，返回 [{buyer_id, buyer_nick, orders:[...]}]（未命中返回 []）。

    兼容两种行：
      UnifiedMtop [...] data={"api":...,"data":{...}}
      qnmodel  [...] resultCode=...,#={"api":...,"data":{...}}
    """
    if RECEPTION_API not in line:
        return []
    match = _JSON_START.search(line)
    if not match:
        return []
    start = match.start(1)
    try:
        payload, _end = json.JSONDecoder().raw_decode(line[start:])
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    rows = ((payload.get("data") or {}).get("data")) or []
    if not isinstance(rows, list):
        return []
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        buyer_id = str(row.get("buyerId") or "").strip()
        if not buyer_id:
            continue
        trades = row.get("tradeSimpleList")
        if not isinstance(trades, list):
            trades = []
        parsed.append({
            "buyer_id": buyer_id,
            "buyer_nick": str(row.get("snsNick") or row.get("buyerNick") or "").strip(),
            "orders": [normalize_order(t) for t in trades if isinstance(t, dict)],
        })
    return parsed


def candidate_log_paths(config: Any = None) -> list[Path]:
    """按优先级列出可能的 app.log 路径。"""
    paths: list[Path] = []
    explicit = []
    if config is not None:
        try:
            explicit = list(config.get("native_order_log_paths") or [])
        except Exception:  # noqa: BLE001
            explicit = []
    paths.extend(Path(str(p)) for p in explicit if str(p).strip())
    for base in ("D:", "C:", "E:", "F:"):
        paths.append(Path(base + os.sep) / "AliWorkbenchData" / "System" / "log" / "app.log")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths.append(Path(local) / "AliWorkbenchData" / "System" / "log" / "app.log")
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


class NativeOrderLog:
    """tail 千牛 app.log，维护 buyerId -> 订单 缓存。"""

    def __init__(self, app: Any, poll_seconds: float = 1.0,
                 backfill_bytes: int = BACKFILL_BYTES) -> None:
        self.app = app
        self.poll_seconds = float(poll_seconds)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="native-orders", daemon=True)
        self.path: Path | None = None
        self.offset = 0
        self.lines_read = 0
        self.parse_errors = 0
        self.updates = 0
        self.last_error = ""
        self.last_update_at = 0.0
        self.orders: dict[str, list[dict[str, Any]]] = {}
        self.nicks: dict[str, str] = {}
        self.backfill_bytes = max(0, int(backfill_bytes))
        self.backfilled = 0
        self._first_chunk_done = False
        self.last_size = 0

    # ---- 生命周期 ----
    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    # ---- 读日志 ----
    def locate(self) -> Path | None:
        for path in candidate_log_paths(getattr(self.app, "config", None)):
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.path is None:
                    self.path = self.locate()
                    if self.path is not None:
                        try:
                            size = self.path.stat().st_size
                        except OSError:
                            size = 0
                        # 回读尾部：现场装上就要能查到，不能等新日志。
                        self.offset = max(0, size - self.backfill_bytes)
                        self.backfilled = int(self.offset > 0)
                        LOG.info("native order log attached path=%s offset=%s size=%s",
                                 self.path, self.offset, size)
                if self.path is not None:
                    self._consume()
            except Exception as error:  # noqa: BLE001
                self.last_error = str(error)[:200]
                LOG.debug("native order log error: %s", error)
            self.stop_event.wait(self.poll_seconds)

    def _consume(self) -> None:
        path = self.path
        if path is None:
            return
        try:
            size = path.stat().st_size
        except OSError as error:
            self.last_error = str(error)[:200]
            self.path = None
            return
        # 只认"文件真的变小"（轮转/截断）；日志正常追加时 offset 可能
        # 因为 stat/read 的时间差略大于 size，绝不能因此从头重扫 30MB。
        if self.last_size and size < self.last_size:
            self.offset = 0
            self._first_chunk_done = False
            self.backfilled = 0
        self.last_size = size
        if size <= self.offset:
            return
        with path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read(max(0, size - self.offset))
        if not chunk:
            return
        # 只处理完整行，半行留到下一次
        text = chunk.decode("utf-8", "replace")
        if self.backfilled and not self._first_chunk_done:
            # 从文件中段开始读，第一行大概率是半行，丢掉
            first_break = text.find("\n")
            if first_break >= 0:
                dropped = text[: first_break + 1]
                self.offset += len(dropped.encode("utf-8", "replace"))
                text = text[first_break + 1:]
        self._first_chunk_done = True
        self.backfilled = 0
        complete, _, _tail = text.rpartition("\n")
        if not complete:
            return
        consumed = len(complete.encode("utf-8", "replace")) + 1
        self.offset += consumed
        changed = False
        for line in complete.splitlines():
            self.lines_read += 1
            try:
                rows = parse_line(line)
            except Exception:  # noqa: BLE001
                self.parse_errors += 1
                continue
            if not rows:
                continue
            for row in rows:
                changed = True
                self._remember(row["buyer_id"], row["buyer_nick"], row["orders"])
        if changed:
            with self.lock:
                self.updates += 1
                self.last_update_at = time.time()

    def _remember(self, buyer_id: str, nick: str, orders: list[dict[str, Any]]) -> None:
        with self.lock:
            if nick:
                self.nicks[buyer_id] = nick
            merged = {row["order_id"]: row for row in self.orders.get(buyer_id, []) if row.get("order_id")}
            for row in orders:
                if not row.get("order_id"):
                    continue
                old = merged.get(row["order_id"])
                if old is None or (row.get("modified_at") or 0) >= (old.get("modified_at") or 0):
                    merged[row["order_id"]] = row
            rows = sorted(merged.values(), key=lambda r: r.get("modified_at") or 0, reverse=True)
            self.orders[buyer_id] = rows[:MAX_ORDERS_PER_BUYER]
            while len(self.orders) > MAX_BUYERS:
                self.orders.pop(next(iter(self.orders)))

    # ---- 查询 ----
    def orders_for(self, buyer_id: str) -> list[dict[str, Any]]:
        key = str(buyer_id or "").strip()
        if not key:
            return []
        with self.lock:
            rows = self.orders.get(key) or []
            nick = self.nicks.get(key, "")
        result = [dict(row) for row in rows]
        for row in result:
            row.setdefault("buyer_nick", nick)
        return result

    def buyer_ids(self) -> list[str]:
        with self.lock:
            return list(self.orders.keys())

    def status(self) -> dict[str, Any]:
        with self.lock:
            buyers = len(self.orders)
            order_count = sum(len(rows) for rows in self.orders.values())
        return {
            "enabled": bool(self.path is not None),
            "path": str(self.path) if self.path else "",
            "buyers": buyers,
            "orders": order_count,
            "lines_read": self.lines_read,
            "updates": self.updates,
            "last_update_at": self.last_update_at,
            "parse_errors": self.parse_errors,
            "backfill_bytes": self.backfill_bytes,
            "backfilled": self.backfilled,
            "last_error": self.last_error,
        }
