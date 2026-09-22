# 千牛 MessageSDK code 5 路由修复报告

> 日期：2026-08-15  
> 类型：本地可靠性逆向，`flavor = null`  
> 目标：QianniuStandaloneBridge 1.4.0 / AppBiz.dll  
> 工具：Frida 17.9.6、Python 3.10、Node.js、unittest

## 执行摘要

> **2026-08-18 安全替代说明：** 本报告记录的发送前主动 `GetNewMsg` 预热已在 1.4.2 删除。该调用可能与千牛主对话框竞争新消息游标，不得恢复。当前实现只被动观察千牛自身的 AppBiz 活动；目标会话没有近期路由时发送会安全拒绝。

工作台发送后千牛未实际发出并返回 `result code 5`，根因已经定位为 AppBiz 服务实例选错。当前进程内存在三个布局相同的 `CAppMessageService` 对象，旧逻辑依据 `im.singlemsg`、提示音字符串和账号字段猜测，选中了候选 0；12 秒只读虚表追踪证明，千牛页面针对目标 ccode 的 `GetNewMsg` 全部由候选 1 处理。千牛日志同时将 code 5 标记为 `CheckConvExist_error`，与错误实例缺少会话发送上下文一致。

修复后不再使用字符串特征、候选序号或候选数量放行发送。桥接器按完整 ccode 记录真实 `GetNewMsg` 的 `this` 指针；发送前静默调用一次 `im.singlemsg.GetNewMsg` 刷新目标会话，不打开千牛窗口、不抢焦点，然后只使用该 ccode 对应的实例。若没有最近的真实路由，发送会在本地直接拒绝，不进入 MessageSDK。

## 范围

授权和边界见 [case scope](../work/qianniu-send-receipt-fix/scope.md)。分析范围仅包含本地桥、随项目运行的千牛客户端及 `127.0.0.1` 服务；未测试登录绕过或第三方账号系统。

## Evidence

| ID | 证据 | 来源 | 结论 |
|---|---|---|---|
| E-002 | MessageSDK 异步回调 | `native/appbiz_adapter.cpp` | 同步返回 0 不能代表消息已发送 |
| E-004 | 自动化回归 | `tests/test_standalone.py` | 28 项测试通过 |
| E-007 | AppBiz 虚表活动追踪 | `notes/appbiz-service-vtable-activity.json` | 候选 1 才处理目标 ccode 的 `GetNewMsg` |

E-007 复现命令：

```powershell
cd <repo-root>
py -3.10 tools\trace_appbiz_service_vtable.py 15816 --seconds 12 --output notes\appbiz-service-vtable-activity.json
```

关键观测：候选 0 调用数为 0；候选 1 在虚表槽 3 和 4 上各出现 3 次调用，参数包含目标完整 ccode，槽 3 还包含 `GetNewMsg|...`；候选 2 调用数为 0。

## Findings

### F-005

- title: 字符串启发式选择了错误的 AppBiz 消息服务
- severity: medium
- category: design
- status: validated
- evidence_ids: [E-007]
- confidence: high
- location: `appbiz_agent.js`
- impact: MessageSDK 无法在错误实例中找到会话，返回 code 5；工作台收到拒绝而千牛没有消息
- remediation: 已改为按完整 ccode 的真实 `GetNewMsg` 活动路由，并删除启发式自动选择

### F-006

- title: 发送前未显式刷新目标会话上下文
- severity: low
- category: design
- status: validated
- evidence_ids: [E-007]
- confidence: high
- location: `standalone_bridge.py:prepare_send_context`
- impact: 冷启动或长时间未轮询的会话可能没有可用发送上下文
- remediation: 已在发送前后台调用 `im.singlemsg.GetNewMsg`，不触发界面跳转

## Path

### P-002

- title: 从工作台发送到正确 AppBiz 实例
- path_type: callflow
- start: 工作台提交唯一 `request_id` 和完整 ccode
- goal: 仅在目标 ccode 的真实消息实例上调用 MessageSDK
- steps:
  1. action: SQLite 原子记录 `in_flight` | evidence: E-004 | finding: F-005
  2. action: 后台调用目标 ccode 的 `GetNewMsg` | evidence: E-007 | finding: F-006
  3. action: Frida 从真实调用记录 ccode 到 AppBiz `this` 指针的映射 | evidence: E-007 | finding: F-005
  4. action: 仅使用该映射进入 native adapter 和 MessageSDK | evidence: E-002, E-007 | finding: F-005
  5. action: 按 MessageSDK 回调写入 `confirmed` 或 `rejected`，未知结果禁止自动重试 | evidence: E-002, E-004 | finding: F-005
- residual_risks: 千牛升级后 RVA 或对象布局可能变化；每个新版本仍需做一次带唯一文本的人工收发验收

## 修复与验证

| 项目 | 结果 |
|---|---|
| JavaScript 语法 | `node --check appbiz_agent.js` 通过 |
| Python 编译 | `py -3.10 -m py_compile ...` 通过 |
| 自动化测试 | 28/28 通过 |
| 浏览器桥 | `connected=1`，`imsdk_hooked=true` |
| AppBiz 路由 | `selection_reason=singlemsg_getnewmsg` |
| ccode 上下文 | `observed_ccode_count=1` |
| 当前服务 | 候选 1，地址 `0x1e726b485b0` |
| 本地入口 | `http://127.0.0.1:18767/` |

人工出站验收未由自动化执行，避免向真实会话擅自发送或重试旧内容。下一条测试消息应由操作员只点击一次；成功条件是工作台返回 `confirmed`、千牛出现同一条消息、对端仅收到一条。

## Timeline 摘要

- 01:15：真实失败返回 code 5，千牛日志记录 `CheckConvExist_error`。
- 01:31：虚表追踪证明旧选择为候选 0，真实 `GetNewMsg` 使用候选 1。
- 01:36：完成按 ccode 的动态路由和后台上下文预热。
- 01:40：28 项测试通过，桥接服务重启，运行态显示正确实例和一个已观测 ccode。

