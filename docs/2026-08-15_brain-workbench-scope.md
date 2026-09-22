# 大脑工作台集成范围

- `auth`: 用户明确要求分析其本机旧桥接项目，并将除收发底座外的能力迁移到新项目。
- `in_scope`: 旧 `QianniuBridgeAgent-v0.5.16` 目录、新 `QianniuStandaloneBridge` 目录、本机 127 工作台和已登录的官方千牛进程。
- `out_of_scope`: 未提供凭证的真实大脑服务、绕过千牛登录、提取登录凭证、自动向真实买家发送测试消息。
- `network_profile`: 只对本机服务和测试内临时 HTTP server 主动验证；真实大脑协议仅实现，未连接。
- `safety_boundary`: 大脑默认关闭；启用前历史消息不会补发；UI 自动化不点击发送按钮。

