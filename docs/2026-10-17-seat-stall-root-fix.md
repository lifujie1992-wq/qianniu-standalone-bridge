# 坐席上送 10~30s + 面板「连接异常」的根因修复（1.6.15）

现场：2026-10-17 07 左右，联想官方旗舰店:瑞秋 那台坐席机（1.6.14）面板显示
**连接异常 / 桥接服务 已停止 未监听**，同时大脑侧测到它的事件上送
`captured_at → server_received_at` **p50 11.2s / p90 21.2s**，同店其它 4 台同类客户端
只要 0.6~1.9s；客户端自己上报的 `delivery.last_error` 在 73% 的采样里是 `timed out`，
命令轮询间隔从 1.8s 拉到 4.5s（峰值 10.3s）。

## 结论：不是掉线，是这台机的客户端进程被自己的本地链路拖住了

链路上真正慢的是**本机这一段**，不是网络：

1. 注入页捕获只花 0.2s（`captured_at` 与千牛 `ts` 相差 0.06~0.3s），说明页面发现消息很快。
2. 之后事件要过 `DeliveryWorker` 的**本地自检 POST**（`http://127.0.0.1:18776/api/local-seat/v1/events`），
   超时预算只有 **1.5s**。
3. 自检处理函数对**每一条事件**调用一次 `db.event_status()`：每次都新开一个 SQLite 连接，
   而且 `connect()` 里每次都执行 `PRAGMA journal_mode=WAL`（库级持久设置，只需要设一次），
   100 条一批就是 100 次开库 + 100 次全局锁。
4. 7 个注入页并发写同一个 SQLite，Windows 上杀软还会实时扫这个文件 → 自检常态超过 1.5s →
   整批被 `mark_failed` + 指数退避（0.2→5s）重试 → 事件实际入库被推迟 **10~30s**。
5. 与此同时，`db.workbench_sessions()`（最多 20000 条 payload 逐条 JSON 解析）**整段跑在全局锁里**，
   `/api/v1/status` 也要经过它。托盘探针（1.6.14 已经是 5s 超时）撞上重建就超时 →
   面板写「已停止 / 未监听」，而消息其实还在收发。

## 本版改了什么（1.6.15）

| # | 位置 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `StateDB.connect()` | 去掉每次连接都执行的 `PRAGMA journal_mode=WAL`，改为在 `_initialize()` 里设一次；新增 `PRAGMA busy_timeout=8000`；保留 `synchronous=FULL` | journal_mode 是库级持久设置；重复设置要拿文件锁，是单次开库几十~几百毫秒的来源。耐久性不变 |
| 2 | `StateDB.event_statuses()`（新） | 批量状态查询：一次连接、一条 `IN (...)` | 本地自检从 N 次开库降到 1 次 |
| 3 | `local_seat_ack_payload()`（新，模块级） | 自检 ACK 改用批量查询；语义不变（`sending`/`delivered` 才算 durable，未提交仍然整批拒绝） | 同上；抽成函数便于回归测试 |
| 4 | `DeliveryWorker.post()` | `delivery_timeout_seconds` 默认 **1.5 → 8.0**（`config.example.json` 同步） | 本地自检不该用 1.5s 预算；超时→退避重试才是 10~30s 延迟的直接放大项 |
| 5 | `StateDB.workbench_sessions()` | 会话快照的整表扫描**移出全局锁**，用独立 `_session_build_lock` 防重复重建 | 扫描期间不再阻塞 `/api/v1/status`、自检、事件入库；托盘不再把忙机报成停止 |
| 6 | `tray_app._probe_bridge()` | 托盘探测：**连续 2 次失败**才判异常；文案区分「未监听 / 响应超时（进程在忙）/ HTTP 4xx / 返回异常」 | 单帧失败不再翻成红色「连接异常」；真挂和只是慢能一眼分开 |
| 7 | `launcher.fetch_status_with_reason()`（新） | 返回失败原因（`refused` / `timeout` / `http_*` / `bad_response`）；`fetch_status()` 行为不变 | 托盘需要原因，其它调用方零改动 |
| 8 | `_initialize()` 新增 `events_created_at` 索引 | `events(created_at DESC)` | 会话快照的 `ORDER BY created_at DESC LIMIT 20000` 原来要全表扫描 + 排序（保留期 30 天），既慢又占 CPU |

**没有改**：消息捕获、去重口径、发送链路、AI/人工路由、任何买家可见文案。
`events.status` 的语义、`sending/delivered` 的判定、退避上限（5s）都保持原样。

## 回归测试

`tests/test_seat_stall_guards.py`（14 条，全部通过）：

* 批量状态查询与逐条查询结果一致；空输入/重复 ID/非 dict 条目/100 条上限；
* ACK 语义：pending 必须整批拒绝，`claim_pending()` 之后才成 committed，且整批只查一次库；
* WAL 不再依赖每连接 PRAGMA（读回仍是 `wal`），`busy_timeout=8000`；
* `events_created_at` 索引存在，且会话扫描的查询计划确实用到它（`EXPLAIN QUERY PLAN`）；
* **会话快照重建期间，其它需要全局锁的操作必须在 1s 内返回**（旧实现会排队到重建结束）；
* 快照缓存命中、聚合内容（account / message_count / last_message / ai_mode）不变；
* 本地自检默认超时 8s，显式配置仍然优先。

全量：`python3 -m pytest tests --ignore=tests/test_standalone.py --ignore=tests/test_tray_lifecycle.py`
→ **193 passed**（改动前 179 passed + 14 条新增）。两个被忽略的文件需要 Windows
（`tkinter` / `ctypes.windll`），改动涉及它们的部分靠 `py_compile` + 代码审阅。

## 还没做的（建议下一版）

1. `backfill_nicknames()` 每 5s 调一次 `workbench_sessions()`，等于每 5s 触发一次 2 万条 payload 扫描；
   短期可把扫描窗口做成 `最近 N 小时` 而不是固定 20000 条，长期建议落一张 `sessions` 增量表。
2. `context_enrich` 在 `imsdk unavailable` 时没有退避（现场 52 次请求 0 成功，每次失败占一个 6s 超时的线程）。
3. 发送链路仍只支持千牛 9.97.81N；`AliUpdater` 在非管理员下禁用失败（现场 147 次「拒绝访问」），
   千牛升到 9.98.x 会整条发送失效 —— 见客户端 `client_support.py` 的 `HOOK_PROFILES`。

## 打包（需要你们在构建机上做）

1. `installer/QianniuAIService.iss` 里的 `AppVersion` / `SourceDir` / `OutputBaseFilename`
   按你们惯例对齐到 1.6.15。**我没有改这个文件**：`SourceDir` 指向
   `..\delivery\淘宝千牛AI客服-安装版-<版本>`，命名是你们构建流程约定的，猜错会直接导致打包失败。
2. 装出来的包在坐席机上覆盖安装后，`/api/v1/status` 的 `version` 应为 `1.6.15`。

## 回滚

纯客户端改动，不涉及大脑协议：

* 代码：`git revert` 本分支提交（或 `git checkout v1.6.14-rc1 -- <文件>`）。
* 运行期：客户端配置里把 `delivery_timeout_seconds` 显式写回 1.5 即可恢复旧超时；
  其余为纯性能/显示改动，无开关。
