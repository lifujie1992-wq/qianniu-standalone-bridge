# Qianniu Standalone Bridge

## 大脑与 AI 回复

工作台已内置旧版探域镜像方案兼容的大脑连接器，同时保留当前独立、稳定的千牛收发链路。打开 `http://127.0.0.1:18776/`，点击右上角设置按钮，填写大脑地址和 Agent Token 后即可测试连接并启用。

- 大脑协议兼容 `/api/bridge/v1/register`、`heartbeat`、`events`、`commands` 和命令结果回传。
- AI 的 `send_text` 命令复用当前千牛发送链路，并以命令 ID 做持久化幂等，避免重复发送。
- 同一命令续租只更新租约回执；调试/测试/站外引流文案会在触发千牛发送前被拦截。
- 退款、投诉、改地址、激烈情绪和媒体消息会先发送转接提示，再自动转人工一小时；也可在会话顶部手动切换 `AI / 人工`。
- 服务端返回非空 `allowed_shop_ids` 时，本机同时约束事件上报和 AI 命令执行。
- 新消息先落本地数据库，再异步上传大脑；大脑离线时保留队列，不阻塞千牛收消息。
- 商品、订单和物流上下文在独立线程中补充，不阻塞主收发链路。
- 工作台右侧提供连接状态、积压、命令记录、原始事件和完整诊断信息。
- 大脑默认关闭，令牌不会返回到浏览器，也不会写入诊断日志。

完整协议证据、架构和验证说明见 [`docs/2026-08-15_reverse-brain-workbench-integration-report.md`](docs/2026-08-15_reverse-brain-workbench-integration-report.md)。

独立千牛消息桥。它复用操作者在官方千牛中的正常登录会话，不依赖探域账号、探域进程、探域云接口、旧 IPC 或旧注入 DLL。

完整逆向与验收记录见 [docs/2026-08-14_reverse-qianniu-standalone-bridge-report.md](docs/2026-08-14_reverse-qianniu-standalone-bridge-report.md)。

## 当前运行方式

- 接收：打包进千牛 `webui.zip` 的 `browser_bridge.js` 只订阅 IMSDK 事件；兼容多参数和嵌套 ccode 回调，消息体不完整时对千牛本地 `msgDataMap` 做最长约 5 秒的有限只读重试。
- 隔离：桥不会调用 `GetNewMsg` / `PeekNewMsg` / `GetRemoteHisMsg`，不会替换 `imsdk.invoke`，也不会调用 `imsdk.off`，避免推进千牛主对话框的消息游标。
- 投递：事件先按平台 `messageId` 写入 SQLite，再由内置 `127.0.0.1:18776` 工作台确认；收到 durable ACK 后标记为已送达。
- 去重：同一平台消息统一使用 `qn-msg-v1|taobao|<messageId>`，重复回调和本地缓存重试不会生成第二条。
- 发送：`appbiz_agent.js` 连接当前千牛 9.97 的 `AppBiz.dll`，通过已验收的 x64 ABI 适配器发送文本。
- 聚合接待：`docked_workbench.py` 启动一个 286 px 宽的独立窗口并贴在千牛接待中心旁；移动、最小化或切换千牛时会同步跟随或隐藏。
- 会话跳转：点击聚合消息行后，通过千牛官方 `QNAbilityCenter` 的 `openChat` 能力打开对应 ccode，并校验实际打开的会话，不依赖探域的 `open_customer_dialog` 服务。
- 边界：HTTP 和 WebSocket 仅绑定 `127.0.0.1`，发送 API 使用 bearer token。
- 兼容：旧 9.77 `qnmsg` 适配器保持关闭；`AliUpdater` 被守卫禁用，避免覆盖独立 WebUI。

默认配置关闭投递和文本发送，完成当前版本人工验收后再显式启用。它仍要求官方千牛正常登录，不绕过千牛认证，也不提取登录凭证。冷会话若尚未被千牛自身活动建立 AppBiz 路由，发送会安全拒绝，不会通过主动拉取新消息来预热。

独立工作台：`http://127.0.0.1:18776/`。它直接读取本桥 SQLite，不依赖原 `18766` 工作台或远程会话详情同步。`start.ps1` 会在本地服务就绪后自动启动聚合接待窗；窄窗模式地址为 `http://127.0.0.1:18776/?dock=1`。

## 启动与状态

```powershell
Set-Location <repo-root>
.\start.ps1
.\status.ps1
```

状态接口：`http://127.0.0.1:42111/api/v1/status`

正常状态至少应满足：

```text
ok=true
browser.connected=1
browser.diagnostics.imsdk_hooked=true
appbiz_send.ready=true
delivery.pending=0
```

主工作台点击会话只切换本地详情；只有吸附聚合窗的列表行会在人工点击时打开千牛。自动刷新或默认选中不会切换千牛会话。页面请求有超时恢复，服务未就绪时聚合窗会等待，不会停在空列表。

## 发送 API

`buyer_cid` 必须是完整千牛 ccode；不需要 `seller_nick` 或 `is_set_time`。

```http
POST /api/v1/send/text
Authorization: Bearer <config.json 中的 api_token>
Content-Type: application/json

{
  "request_id": "由调用方生成且永久唯一的请求 ID",
  "buyer_cid": "buyer.1-seller.1#11001@cntaobao",
  "content": "test"
}
```

`request_id` 在原生调用前以 `in_flight` 状态原子写入 SQLite。发送回执使用以下状态：

- `submitted`：已调用千牛发送函数，仍在等待 MessageSDK 回调或卖家消息回显。
- `confirmed`：MessageSDK 明确返回成功，或采集到相同会话、相同内容的卖家消息回显。
- `unknown`：超过确认窗口仍无回调或回显；结果不确定，禁止自动重发。
- `rejected`：千牛发送函数或 MessageSDK 明确拒绝。

- 相同 ID、相同内容重试时返回已存回执，不会再次调用千牛。
- 相同 ID、不同内容会被拒绝。
- 若进程在原生调用后、写入最终回执前退出，该 ID 会保持 `in_flight`，自动重试被阻止，因为发送结果无法安全判定。
- 旧版本仅凭原生函数返回记为 `completed` 的记录会迁移为 `unknown`，不会伪装成发送成功。

调用方只有在业务上确认未发送后，才应使用一个全新的 `request_id` 人工重试。

## 切换与回滚

`cutover.ps1` 默认只预览；`-Execute` 才执行切换。当前机器已经完成独立切换。

```powershell
.\cutover.ps1
.\cutover.ps1 -Execute
```

`rollback.ps1 -Execute` 会关闭独立投递并尝试恢复切换前进程。回滚会恢复旧依赖，只用于故障处置。

## 验证

```powershell
py -3.10 -m unittest discover -s .\tests -v
py -3.10 -m py_compile .\standalone_bridge.py
node --check .\browser_bridge.js
node --check .\appbiz_agent.js
```

当前回归集为 26 项，覆盖接收去重、durable ACK、内置工作台详情、主工作台与吸附窗点击边界、未绑定时草稿编辑、聚合窗左右停靠、运行时剥离、WebUI 注入幂等、更新守卫、当前进程 AppBiz 选择、MessageSDK 回调、发送回显确认、超时未知状态和发送请求的原子幂等。

## 消息源开关

页内桥有四条消息来源，由注入时写入页面的 `window.__qn_standalone_options` 控制，配置项同名：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `bridge_invoke_observer` | true | 观察 `imsdk.invoke`，仅用于发现会话 ID；不在该路径上报消息，避免与事件路径重复 |
| `bridge_ws_mirror` | true | 探测并镜像千牛自身的 IM WebSocket 帧（先调原函数再镜像） |
| `bridge_discovery_poll` | true | 每 4 秒轮换一个"最近会话列表"API，只为发现 ccode |
| `bridge_history_poll` | **false** | 主动拉取 `GetNewMsg` / `PeekNewMsg`。会推进千牛自己的消息游标，默认关闭 |

改动后需要重新注入并让千牛重新加载聊天页：

```powershell
py -3.10 tools\inject_runtime_webui.py runtime config.json browser_bridge.js
```

## 客户端版本自检

| 工具 | 用途 |
|---|---|
| `tools/check_client_support.py` | 报告每个客户端版本是否有 AppBiz profile、webui 是否已注入、是否存在待替换的 `runtime\new\*` |
| `tools/verify_appbiz_profile.py` | 在**运行中的**客户端进程里校验 profile 的 RVA 与 vtable 槽位 |
| `tools/verify_adapter_load.py` | 在运行中的客户端进程里加载 adapter 并校验导出与 MSVC STL 布局 |
| `tools/map_appbiz_offsets.py` | 客户端升级后，从已知版本推导新版本偏移（函数锚点 + vtable 内容 + 相对布局，三重自校验） |
| `tools/build_appbiz_adapter_offline.ps1` | 没有 Visual Studio 时，用拼装的 MSVC 工具链重建 adapter |

桥启动时也会自动跑一遍客户端支持检查（status 的 `client_support`），每 10 分钟复查并自动重新注入丢失的 webui 桥。

## 仓库边界

本仓库只包含桥自身的源码与逆向记录，以下内容不随仓库发布：

- `runtime/`：千牛客户端副本（阿里版权文件）。运行前请自备已登录的官方千牛工作台，并在 `config.json` 中指向自己的 `qianniu_exe`。
- `vendor/`：第三方插件二进制，仅特定历史版本的本机运行需要，不随源码分发。
- `build/`：`native/appbiz_adapter.cpp` 的编译产物，用 `tools/build_appbiz_adapter.ps1` 在本机重新生成。
- `state/` 与 `config.json`：本机运行态与凭据（API token、大脑地址等），仓库只提交 `config.example.json`。
- `delivery/` 与 `exe_build/`：历史安装包与 PyInstaller 构建目录。

首次运行请复制 `config.example.json` 为 `config.json`，填入自己的 `api_token`、`browser_token` 与大脑地址。`cutover.ps1` / `rollback.ps1` 中原先硬编码的旧环境路径与大脑地址已改为参数传入（见脚本参数）。

`tests/test_standalone.py` 中依赖打包运行时的用例在缺少 `runtime/` 与 `vendor/` 时会自动跳过，其余用例描述源码在本机可直接执行。