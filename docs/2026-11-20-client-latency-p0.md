# 2026-11-20 客户端上报/发送时延 P0 收尾

服务端计算+投递已经压到均值 7.66s（投递 1.84s），全链路剩下的尾巴都在客户端两段：
上报延迟（均值 11.96s，最差 28.9s）和本地发送等待（7.8~9.5s）。本轮把这两段剩下的
空转全部去掉。

## 1. 上报段：实时消息不再排在积压后面

`StateDB.claim_brain_events` 原来是纯 `ORDER BY queued_at` FIFO。转接历史分片和
WS 重连重放会在 `brain_events` 里堆几百条 pending，新进来的实时买家消息必须等这
整批发完才轮到，这就是上报均值 8~29s 的主因。

现在分两段认领（同一个 `BEGIN IMMEDIATE` 事务内，多 worker 仍然不会重复领）：

- **实时段**：`queued_at` 与 `events.created_at`（本地首次捕获时间）都落在
  `brain_event_live_window_seconds`（默认 30s）内的消息，排在最前面按时间出队。
  用 `created_at` 而不是只看 `queued_at`，是因为重放的历史消息「入队时间很新」但
  「捕获时间很旧」，只看入队时间分不出来。
- **积压段**：窗口外的历史/重试事件按 `queued_at` FIFO 补满剩余名额，并固定保留
  `limit//4` 个名额，实时洪峰持续时历史也不会被饿死。

`brain_event_live_window_seconds=0` 回到改动前的纯 FIFO 行为。

另：`enqueue_brain_event` 本来就对 `status in {sending, delivered}` 直接返回 False，
`upsert_event` 对已 delivered 的事件不再重排，因此已确认过的历史终端消息不会被
WS 重连重新塞回上传队列——这一点不需要额外改动，这里记录以免后续被改回去。

## 2. 上报段：WS 卡顿不再让每批白等 10s

`brain_ws_ack_timeout_seconds` 默认从 `10.0` 降到 `2.5`，并加短期熔断：

- 一批 WS 上报只要回来**任意一条**确认就算链路正常（服务端会按自身规则跳过或
  拒绝某些事件，部分确认不能当成卡死）；
- 整批 **0 确认**记一次 miss，连续 `brain_ws_failover_misses`（默认 2）次后熔断
  `brain_ws_failover_seconds`（默认 30s），这期间事件直接走
  `POST /api/bridge/v1/events`；
- 整批 0 确认时**当场**回退 HTTP，不再把这几秒拖到下一轮重试。服务端按
  `event_id` 幂等（`duplicate` 也算 committed），即使 WS 帧其实已经到达也不会
  重复入库。

上报路径从 `upload_events` 的内嵌函数抽成 `BrainConnector.post_events(batch)`，
便于单测直接覆盖三种分支（WS 有确认 / WS 零确认 / WS 抛错）。

## 3. 发送段：回执轮询退避 100ms → 40ms

`appbiz` 适配器的 `pollsend` 退避是「前 0.5s 25ms，之后 100ms」。100ms 的退避意味
着回执确认平均多等 ~50ms，串行几条回复就累积几百毫秒，改为 40ms。

发送池（`command_sender_pool_enabled` / `command_sender_workers`）和
「已知 route_ready 就跳过 `warm_send_route` 热身」在更早的改动里已经落地，本轮
没有回退，只补了默认值与回归测试。

## 配置与迁移

新增/变更（`config.example.json` 与 `config_defaults.py` revision 10 同步）：

| 配置项 | 默认 |
|---|---|
| `brain_ws_ack_timeout_seconds` | 2.5（旧默认 10.0 会被迁移改写，现场自调值保留） |
| `brain_ws_failover_misses` | 2 |
| `brain_ws_failover_seconds` | 30.0 |
| `brain_event_live_window_seconds` | 30.0 |

`/status` 新增 `event_ws_ack_timeout_seconds`、`event_ws_failover_active`、
`event_ws_ack_misses`、`event_ws_failover_until`、`event_live_window_seconds`，
现场可直接确认熔断是否打开。

## 回归测试

`tests/test_latency_p0.py`：

- `RealtimePriorityClaimTests`：新消息先出队、实时洪峰下积压仍保住 `limit//4`
  名额且能排空、跨批不重复认领、`window=0` 保持旧 FIFO；
- `WsAckFailoverTests`：2.5s 默认与夹取、连续 miss 熔断、任意确认即复位、熔断
  到期、`post_events` 的 WS/HTTP 三种分支、示例配置与迁移、回执退避常量。

## 打包与启动验证（2026-11-20 实测）

`tools/build_windows.ps1` 产出 `delivery/windows-2.0.0-20261120-162606/`：
`QianniuAgent.exe` 57 MB、`QianniuAIService-2.0.0-windows-x64.zip` 54.2 MB
（sha256 `2d5761e047726583…`），`build-info.json` 记录 commit `5fda5cc`。

把包解到独立目录（端口 42120/42121/18786，`dock_enabled=false`）启动
`QianniuAgent.exe --role bridge`，用一个同端口的假大脑（HTTP + 从不回 ack 的 WS）
实测：

| 验证项 | 实测结果 |
|---|---|
| 包启动 | `standalone bridge started version=2.0.0`，`/health` 200，工作台 `/` 200 |
| 配置迁移 | revision 10；`brain_ws_ack_timeout_seconds` 10.0 → 2.5；三个新键写入 |
| 实时优先 | 同一批先发的 5 条中，2 条实时消息排在 3 条历史重放之前（假大脑侧按到达顺序记录） |
| WS 短超时 | 发帧后 **2.5s** 放弃并当场 `POST /api/bridge/v1/events`（t+2.5s 出现 HTTP 上报，misses=1） |
| 熔断 | 第 2 次无确认后 `event_ws_failover_active=true`（窗口 30s）；随后 12s 内 WS 发帧数不变、HTTP 上报继续 |
| 队列不丢 | HTTP 故意回 503 时事件保持 pending 重试，不丢不重复 |

注：入口门控 `brain_event_delay_seconds`（0.2s）会作用于**新入队**事件，所以
在门控窗口内恰好走到认领的积压会先发一批——这是门控语义，不是分层失效；
两者都过了门控时，实时消息稳定排在积压前。

