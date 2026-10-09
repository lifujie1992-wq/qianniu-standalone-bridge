# 天猫生成后未送达：发送链路故障注入复盘

范围：联想官方旗舰店天猫。修复客户端 1.6.10；其他店铺保持原有等待和结果兼容规则。本次未向真实客户发送测试消息，未修改后端部署，也未生成 EXE。

## 已复现和修复

1. 回执等待过早结束：默认等待 2 秒后调用 cancelsend；原生实现删除 receipt_token 对应记录，晚到的回调不再保存。注入第 3 秒的 MessageSDK code 5，旧代码返回 submitted 而没有捕获失败；第 3 秒的成功回调同样丢失。
   - 修复：目标店继续等待既有 send_confirmation_timeout_seconds（默认 15 秒，扩展窗口最多 60 秒）；明确拒绝返回失败，成功记录 confirmed。发送池仍让不同买家独立执行，但每条无回执消息占用线程的时间会增加。
2. 未完成发送被改成成功：命令执行层把 status=in_flight 一律转换为 ok=true、real_send=true，即使原始持久化回执 ok=false、submitted=false。
   - 修复：目标店只允许 submitted/confirmed 的原始非失败结果成为成功；in_flight 保持未确认，也不再声称已实际提交。
3. 无有效回执没有提示：submitted/confirmed=false 的回报没有说明等待失败，后端只能展示 accepted。
   - 修复：目标店的无回执或不可读回执增加持久化 error_user，明确“需要人工核对消息是否发出；当前不会自动重发”。后端现有 accepted 分支可展示此说明。不是买家回复，不会发给客户。

## 自测证据

运行环境：macOS Python 3.14，隔离 venv，安装仓库 requirements；只替换原生 SDK 边界和时钟，实际执行 Python 适配器、StandaloneBridge.send_text、SQLite StateDB、BrainConnector.handle_command。

命令：

```
QN_BRAIN_SERVER_URL=http://127.0.0.1:1 PYTHONWARNINGS=ignore::ResourceWarning \
/tmp/qianniu-send-audit-venv/bin/python -m unittest discover -s tests -p 'test_tmall*.py'
```

初始最小自测 4 项中 3 项失败：晚到拒绝未抛错、晚到成功未获取回执、in_flight 被报成功。无回执说明的新增断言修复前也失败。最终 103 项天猫相关测试通过，其中新增 8 项故障注入测试覆盖延迟拒绝/成功、其他店范围、未完成重放、实际命令到持久化记录、无回执不重发、结果回报断连加客户端重启不重复调用原生发送。

浏览器采集契约：node --test tests/browser_bridge_contract.test.js，21 项通过。
发送池/延迟测试：22 项中 21 项通过，1 项既有失败：test_config_revision_backfills_cadence 硬编码 CONFIG_DEFAULTS_REVISION=5，而未修改的 HEAD 已是 9。本次未改该配置或测试。
完整 tests/test_standalone.py 在 macOS 无法导入 Windows UI ctypes.WinDLL，未声称通过；Windows UI、原生 ABI 和真实平台投递仍需 Windows 主机验证。

## 仍不能宣称排除的情况

- 超过最终等待窗口、RPC 断连、客户端在提交中退出：实际是否送达不确定，仍禁止自动重发以免重复回复。记录保留 unknown/未确认，提示人工核对。
- 命令结果 ACK 已提交后，服务端重放首个持久化结果；本地后来转 unknown 不会通过相同 ACK 改写首次结果。本次提供未确认提示，不宣称新增了服务端最终回执同步机制。
- SDK callback code 0 是千牛 SDK 回执，不是客户屏幕证据。此前父消息 4332176092563、4343687653649 的生产回报均为 code 0 路径；本次没有证据证明这两条历史投诉由延迟回调漏洞导致。需要实际 Windows 客户端日志/目标会话核对 SDK、会话路由和真实聊天气泡。
- 原生 ResultCode 的读取使用内部结构偏移 +8；千牛升级造成 ABI/对象布局变化仍需要真实主机验证，不可通过模拟 SDK 证明安全。

## 交付与回滚

在既有修复分支提交到 GitHub，未合并 main。Windows 主机按 tools/build_windows.ps1 打包 1.6.10；只有客户安装后客户端修复才生效。保留旧 1.6.9 安装包可以回滚。未改 SQLite schema、配置文件或 native DLL。
