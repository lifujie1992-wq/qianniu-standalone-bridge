# 千牛独立桥发送回执修复逆向报告

> 分析日期：2026-08-15  
> 目标版本：QianniuStandaloneBridge 1.4.0 / 千牛运行时 9.97.59N  
> 工具链：Frida、静态反汇编、MSVC 2022、dumpbin、Python 3.10、SQLite、unittest  
> 报告类型：普通逆向工程，`flavor = null`

> **当前验收边界：** 自动化回归、DLL 构建和冷启动检查均已通过；为了不向真实买家擅自发消息，重启后的首次在线出站仍需先收到一条新入站消息完成当前进程绑定，再由操作员发送一条测试回复。

## 1. 执行摘要

发送失败的主要原因不是 HTTP 接口，而是旧链路把“C++ 适配器没有抛异常”误当成“千牛已经接受并发出消息”。同时，旧实现跨进程按候选对象序号恢复 AppBiz 服务指针；同样是 3 个候选对象，在新进程里序号相同也不代表对象角色相同，因此会出现静默失败、延迟和误报成功。

1.4.0 改为只接受当前千牛进程中由真实 `OnMessageArrive` 或官方发送调用观察到的服务对象，并加入 MessageSDK 异步结果回调。发送记录现在区分 `in_flight`、`submitted`、`confirmed`、`unknown` 和 `rejected`；没有确认的请求不会被自动重发，从源头阻断重复消息。

## 2. 范围与目标

授权与范围见 [case scope](../work/qianniu-send-receipt-fix/scope.md)。分析和修改仅覆盖本地项目、项目自带千牛运行时及 `127.0.0.1:18767/`、`127.0.0.1:42111/`。未测试第三方账号体系、远端服务安全或任何绕过登录行为。

| 项目 | 值 |
|---|---|
| 目标模块 | `runtime/9.97.59N/AppBiz.dll` |
| AppBiz.dll 大小 | 32,319,488 bytes |
| AppBiz.dll SHA256 | `5002311D9B7882354CD64521AF0D1ED0ECD11FE62A458AED4564ABCB54DDFCD6` |
| 新适配器 | `build/appbiz_adapter.dll` |
| 新适配器 SHA256 | `25CA425651AF68BBEFD0D6470DF41FD650A8B9DC77814F1BD5D1E36A9E5688FF` |
| 服务入口 | `http://127.0.0.1:18767/` |

分析目标是让工作台发送状态与千牛真实结果一致，并确保重试、刷新和进程重启不会制造重复消息。

## 3. 静态与动态分析

### 3.1 模块与导入

新适配器为 x64 PE DLL，导出 5 个函数：布局探测、v1 兼容发送、v2 带回执发送、回执轮询和回执取消。`dumpbin /imports` 显示其依赖仅包括 `MSVCP140.dll`、`VCRUNTIME140*.dll`、`KERNEL32.dll` 和 Universal CRT；没有新增网络、认证或持久化依赖。对应证据为 E-003。

### 3.2 关键函数

| 位置 | 作用 | 修复后约束 |
|---|---|---|
| `AppBiz.dll + 0xa6f9a0` | `CAppMessageService::OnMessageArrive` | 只用真实入站回调的 `this` 选择当前服务对象 |
| `AppBiz.dll + 0xa77ff0` | 官方文本发送入口 | 观察官方发送并校验服务对象 |
| `ResultCode + 0x8` | MessageSDK 结果码 | `0` 才能由回调确认成功 |
| `appbiz_send_text_v2` | 带 token 调用千牛 | 本地返回仅表示调用已提交 |
| `appbiz_poll_send_result_v1` | 读取异步回调 | 返回成功、失败或仍等待 |

### 3.3 运行时选择

旧版保存“候选总数 + 候选序号”，进程重启后只要候选总数相同就恢复该序号。该假设无法证明对象身份。新版只允许三种选择理由：`message_arrive`、`official_send`、同一 PID 且进程创建时间一致的 `same_process_persisted`。此外，注入时如果千牛尚未完成初始化，代理会每 3 秒补扫一次候选对象；冷启动实测从未就绪恢复到 3 个候选对象。

### 3.4 回执状态机

```mermaid
sequenceDiagram
  actor Operator as 操作员
  participant UI as 本地工作台
  participant Bridge as Bridge 1.4
  database DB as SQLite
  participant Frida as AppBiz Agent
  participant Adapter as Native Adapter
  participant Qianniu as 千牛 AppBiz

  Operator->>UI: 发送一条回复
  UI->>Bridge: POST /api/v1/send/text
  Bridge->>DB: 原子写入 in_flight
  Bridge->>Frida: sendtext(receipt_token)
  Frida->>Adapter: appbiz_send_text_v2
  Adapter->>Qianniu: 调用文本发送入口
  Qianniu-->>Adapter: 异步 MessageSDK 回调
  alt result_code = 0
    Adapter-->>Bridge: callback success
    Bridge->>DB: confirmed
  else result_code != 0
    Adapter-->>Bridge: callback rejected
    Bridge->>DB: rejected
  else 回调暂未到达
    Bridge->>DB: submitted
    alt 捕获到相同会话和内容的卖家回显
      Bridge->>DB: confirmed
    else 超过确认窗口
      Bridge->>DB: unknown，禁止自动重发
    end
  end
  Bridge-->>UI: 返回并展示持久化状态
```

## 4. 核心发现

### F-001
- title: 旧适配器把同步返回 0 误当作千牛发送成功
- severity: medium
- category: design
- status: validated
- evidence_ids: [E-001, E-002]
- confidence: high
- location: native/appbiz_adapter.cpp; standalone_bridge.py send path before 1.4.0
- impact: 工作台显示成功但千牛未出现消息，操作员可能重复点击并造成重复发送。
- repro_steps: 旧版提交回复；观察本地 completed；对比千牛官方会话没有对应气泡。
- remediation: 已改为 MessageSDK 回调或卖家消息回显确认。

### F-002
- title: 跨进程按候选序号恢复 AppBiz 服务对象不可靠
- severity: medium
- category: design
- status: validated
- evidence_ids: [E-001, E-005]
- confidence: high
- location: appbiz_agent.js service selection before 1.4.0
- impact: 新进程可能选择错误对象，导致静默失败、无回调或异常延迟。
- repro_steps: 重启千牛；观察候选数量相同；旧逻辑恢复相同序号但无法证明对象身份。
- remediation: 已限制为当前进程真实调用选择，并绑定 PID 与进程创建时间。

### F-003
- title: 旧工作台使用临时乐观气泡掩盖真实发送状态
- severity: low
- category: design
- status: validated
- evidence_ids: [E-001, E-006]
- confidence: high
- location: workbench.html send rendering before 1.4.0
- impact: 刷新后气泡消失，操作员难以区分发送成功、待确认和未知结果。
- repro_steps: 旧版点击发送；看到临时气泡；刷新页面；气泡消失或与千牛不一致。
- remediation: 已完全使用 SQLite 持久化回执渲染状态。

### F-004
- title: 冷启动只扫描一次会永久错过延迟创建的服务对象
- severity: low
- category: design
- status: validated
- evidence_ids: [E-004, E-005]
- confidence: high
- location: appbiz_agent.js:44
- impact: 千牛启动较慢时发送链路一直显示未绑定，直到重启桥接器。
- repro_steps: 桥接器先于 AppBiz 对象初始化完成时注入；旧代理得到 0 个候选且不再扫描。
- remediation: 已加入 3 秒补扫及真实调用触发的强制补扫。

## 5. 调用路径

### P-001
- title: 从工作台请求到千牛确认的发送调用流
- path_type: callflow
- start: 本地工作台提交唯一 request_id
- goal: 只在千牛明确成功或采集到卖家回显时标记 confirmed
- steps:
  1. action: SQLite 原子领取请求并写入 in_flight | evidence: E-004 | finding: F-001
  2. action: 当前进程真实回调选择 AppBiz 服务对象 | evidence: E-005 | finding: F-002
  3. action: v2 适配器注册 receipt_token 并调用发送入口 | evidence: E-002, E-003 | finding: F-001
  4. action: MessageSDK 回调或卖家回显确认；超时转 unknown | evidence: E-002, E-004, E-006 | finding: F-001
  5. action: 工作台从持久化记录展示最终状态 | evidence: E-006 | finding: F-003
- residual_risks: 千牛内部接口升级后 RVA 或对象布局可能变化；每次升级必须重新做带标签的收发验收。

## 6. 修复内容

| 文件 | 主要变化 |
|---|---|
| `appbiz_agent.js` | 真实调用选对象、同进程校验、冷启动补扫、v2 回执 RPC |
| `native/appbiz_adapter.cpp` | MessageSDK 回调捕获、token 回执表、轮询与取消导出 |
| `standalone_bridge.py` | 五态发送回执、旧数据迁移、回显确认、超时 unknown、状态计数 |
| `workbench.html` | 移除乐观成功气泡；未绑定时仍可编辑草稿；仅吸附窗点击会跳转千牛 |
| `tools/build_appbiz_adapter.ps1` | 支持版本化输出并恢复稳定 DLL 文件名 |
| `tests/test_standalone.py` | 扩展到 26 项，覆盖回调、迁移、去重、冷启动补扫和工作台交互边界 |

## 7. 验证结果

| 检查 | 结果 | 证据 |
|---|---|---|
| Python 回归测试 | 26/26 通过 | E-004 |
| Python 编译检查 | 通过 | E-004 |
| Frida JS 语法检查 | 通过 | E-004 |
| 原生 DLL 构建 | 通过，23,040 bytes | E-003 |
| 导出与 ABI 布局 | 5 个导出；32/64/64 | E-003 |
| 冷启动浏览器采集 | connected=1 | E-005 |
| 冷启动候选扫描 | candidate_count=3 | E-005 |
| 工作台 HTTP | 200 | E-005 |
| 旧 completed 迁移 | 8 条转为 unknown | E-006 |
| 重启后真人在线发送 | 尚未执行，等待新入站消息后由操作员授权发送 | E-005 |

复现自动化验证：

```powershell
cd <repo-root>
py -3.10 -m unittest discover -s tests -v
py -3.10 -m py_compile standalone_bridge.py
node --check appbiz_agent.js
.\tools\build_appbiz_adapter.ps1
.\start.ps1
.\status.ps1
```

## 8. Evidence 摘要

| ID | 内容 | source_ref | content_hash |
|---|---|---|---|
| E-001 | 旧本地 completed 与千牛官方气泡不一致 | `state/send-diagnostic.png`、修复前 DB | n/a |
| E-002 | ResultCode +8 与回调回执实现 | `AppBiz.dll`、`native/appbiz_adapter.cpp` | n/a |
| E-003 | DLL 构建、导出、ABI 和依赖 | `build/appbiz_adapter.dll` | n/a |
| E-004 | 26 项自动化验证 | `tests/test_standalone.py` | n/a |
| E-005 | 冷启动服务状态 | `status.ps1`、本地 HTTP | n/a |
| E-006 | 旧回执迁移与状态计数 | `state/bridge.sqlite3` | n/a |

完整 Evidence 记录在 [case evidence](../work/qianniu-send-receipt-fix/evidence/)，时间线见 [timeline](../work/qianniu-send-receipt-fix/timeline.md)。

## 9. 遗留风险与在线验收

当前服务已启动在 `http://127.0.0.1:18767/`。`service_selected=false` 在重启后的第一条真实消息到来前是预期状态，不是扫描失败。在线验收应严格按以下顺序执行：

1. 测试号只发送一条新的唯一文本。
2. `status.ps1` 应显示 `selection_reason=message_arrive` 且 `ready=true`。
3. 工作台只回复一次新的唯一文本。
4. 回执应进入 `confirmed`；若为 `unknown`，不得重复点击，先比对千牛官方会话。
5. 对方只应收到一条，工作台刷新后仍保留同一条持久化记录。

ATT&CK 与 IOC 不适用于本次本地可靠性逆向修复。
