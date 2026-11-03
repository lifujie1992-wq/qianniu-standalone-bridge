# 大脑拒绝工位令牌（invalid agent token）的诊断与处置 — 2026-11-04

现场机器：`DESKTOP-FHQHG6C`，助手 1.6.81（commit `5df9f44`），大脑 `http://47.107.138.228:18765`。

## 1. 现象

工作台「大脑连接」长期显示 `invalid agent token`，`/api/v1/status` 里：

```
brain.state = degraded      brain.registered = false
brain.last_error = "invalid agent token"
brain.request_count = 33    brain.last_request_ms = 25.37
```

千牛侧一切正常（`browser.connected=3`、`imsdk_hooked=true`、`appbiz_send.ready=true`、
`delivery.pending=0`），所以不是买家、也不是千牛或网络的问题。

## 2. 现场取证结论

在客户机上绕过助手，用 `config.json` 里的凭证直接打注册接口：

```
POST /api/bridge/v1/register
X-Agent-Token / X-Agent-Id / X-Device-Id  ← 取自 config.json

403  {"error":"invalid agent token","ok":false}
Server: nginx/1.29.8   X-PDD-Gateway-Upstream: pdd_primary
```

对照实验：

| 试探 | 结果 |
|---|---|
| `GET /health` | 200，大脑在线 |
| `GET /api/bridge/v1/ws` | 426（websockets 服务正常，说明路由到了应用） |
| 真实令牌 / 空令牌 / 随机令牌 / 不带令牌 / `OPTIONS` | 同一句 `403 invalid agent token` |

结论：**服务端在鉴权第一层就拒绝了这台工位**。可能是令牌被重置、工位被删，
或网关把 `/api/bridge/v1/*` 转到了错误的上游。重装客户端、改网络、关代理都不会恢复。

## 3. 客户端原来的问题

不是"连不上"，而是"说不清"：

- `BrainConnector.request()` 把 `HTTPError` 压成 `"大脑 HTTP 403: {raw json}"`，
  `BrainConnector.status()` 只有一句 `last_error`，界面自然只显示服务器的英文原文；
- `brain_auth` 之前没有——`token_fingerprint()` / `classify_brain_http_error()`
  在 `standalone_bridge.py` 里是死代码，而且匹配词表里没有 `invalid agent token`
  （`"invalid token"` 不是它的子串），403 会被归成"服务端拒绝访问"；
- 面板看不到"是哪一串令牌被拒"（这台机器上同时存在大脑工位令牌和本机网关令牌），
  也看不到"该找谁"；
- 鉴权失败和普通抖动共用 15 秒退避，前台每 2 秒重试一次同样的错误。

## 4. 本次改动

| 文件 | 改动 |
|---|---|
| `brain_auth.py`（新增） | 把「状态码 + 响应体」归一化成 `code / status / auth / retryable / message / hint / fingerprint`；只依赖标准库，诊断自身永远可用 |
| `standalone_bridge.py` | `BrainRequestError` 携带结构化字段；`note_failure()` / `clear_failure()` 统一写入状态；`status()` 新增 `agent_id_source`、`token_source`、`token_fingerprint`、`last_error_code/status/hint`、`auth_rejected`、`needs_operator`、`config_warnings`；鉴权失败退避到 60 秒且一分钟只记一条时间线 |
| `config_dialog.py` | 「测试连接」的弹窗直接给出中文结论 + 令牌指纹；把本地网关令牌误填成大脑令牌时先行拦截 |
| `workbench.html` | 大脑面板新增「工位来源 / 令牌指纹 / 处理建议」；鉴权失败时状态标签显示「令牌被拒」 |
| `/api/v1/brain/test` | 拒绝时返回 `error`（结论+建议）、`hint`、`code`、`status`、`token` 指纹，并同步刷新面板 |
| `field-support/采集诊断.ps1` | 新增「大脑注册探针」：现场直接打一次 register，把状态码/回包/令牌指纹写进诊断包（令牌本身不落盘） |
| `tools/check_brain_token.py`（新增） | 一条命令核对令牌：打印指纹、服务端原始回包、结论；退出码 0/2/1 可被脚本使用 |
| `tests/test_brain_auth.py`（新增） | 23 个用例覆盖识别、指纹、状态字段、退避、配置告警与面板渲染 |

同时把 `main` 合并进来（订单兜底 / 工作台行尾重构），否则分支自带的
`test_ai_toggle_control`、`test_qn_native_orders` 用例本来就是红的。

## 5. 怎么用

现场（客户机，只读）：

```bat
python tools\check_brain_token.py
```

输出示例：

```
令牌指纹 : sha256:754c33be6c（43 位，令牌本身不打印）
服务端   : HTTP 403 {"error":"invalid agent token","ok":false}
结论     : 大脑已连通，但工位令牌无效（invalid agent token，令牌指纹 sha256:754c33be6c）。
           请在大脑后台这台客服工位下重新生成令牌，保存后重启助手。
```

工作台：打开「AI 白盒 → 大脑连接」，看「令牌」「最近错误」「处理建议」三行即可判断该找谁。

服务端同学：拿指纹 `sha256:754c33be6c`（完整 sha256 见票据）比对库里令牌，
若不等就是被重置过；若相等则查 `/api/bridge/v1/*` 的网关上游（响应头里的
`X-PDD-Gateway-Upstream: pdd_primary`）。

## 6. 最终结论（同日 03:10 收口）

大脑侧核对结果把问题钉死了：

- 客户机上的两把令牌（`754c33be6c` 旧、`f3f690c6be` 新签发）**都不在这台大脑的令牌库**
  里（`runtime/config.json` 108 条记录逐条比对，0 命中）；
- 大脑的 `config.json` mtime 停在 01:31:06，网关日志里 `POST /api/auth/bridge-tokens`
  自 10-27 之后一次都没有 —— 也就是"签发"这个动作根本没发生在这台机器上；
- 反证：用库里一条真令牌注册，鉴权通过，回的是另一句 219 字节中文
  「该令牌已在另一台电脑使用」（`token_bound_to_another_device`）；而我们拿到的
  43 字节 `{"error":"invalid agent token"}` 只在"令牌不在库"时出现。

结论：**签发令牌的那个页面指向的是另一套大脑/环境，客户端连的是这台**。
换成这台大脑上、已绑定 `device-737e46…` 的令牌后立刻 `200`，
`agent_id=agent-b3e2e1480b1885f7`（服务端按 token 哈希派生，会覆盖客户端自带的工位 ID）。

同时验证了客户端没有"版本问题"：我们绕过助手、直接用 `config.json` 里的字节打接口，
拿到的 403 与面板一致；换令牌后同一个客户端立刻 `brain.state=online`、
`registered=true`、WS 通道接通、事件 3 条全部确认。

## 7. 验证记录

- `python -m unittest discover -s tests`：633 用例全绿（3 skip；需要 PATH 里有 node）。
- 用客户机真实配置跑 `tools/check_brain_token.py`，稳定复现
  `HTTP 403 invalid agent token`；换成有效令牌后返回 200 并打印 `agent_id`。
- 面板实测：`大脑 已连接`、`工位 agent-b3e2e1480b1885f7`、
  `令牌 sha256:b3e2e1480b`、心跳 7 秒前、持久队列 3 已确认。

## 8. 遗留

- 令牌要在这台大脑的 UI（`http://47.107.138.228:18765/` → 令牌管理）里新增，
  否则会签进另一套环境；`brain_auth` 现在把「令牌不在库」和「令牌已绑另一台电脑」
  分开报（`invalid_agent_token` / `token_bound_to_another_device`），并给出对应处置。
- 服务端若继续出现 43 字节 `invalid agent token`，先查签发的地址，不要先怀疑客户端。
