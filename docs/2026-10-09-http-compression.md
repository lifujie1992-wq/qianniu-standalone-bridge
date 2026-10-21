# 1.6.11：HTTP 压缩和被拒绝消息的空查询修复

客户端会声明 Accept-Encoding:gzip，解压正常及错误响应；服务器不压缩时兼容原 JSON。损坏的 gzip、非对象 JSON 继续失败，不伪装成功。发送正文、鉴权、request_id、轮询周期、发送回执超时和人工接管逻辑均保持原有规则。

线上 Nginx 已配置 gzip on、level 5、最小 1024 字节，无需强制旧客户端接收 gzip。只读健康接口实测：未压缩 4413 字节，压缩 680 字节，两者均为有效 JSON。它证明协商实际生效，不代表每日流量的整体节省比例。

模拟会话 JSON 为 77192 字节；按线上 level 5 压到 8373 字节（减少约 89.2%），压缩+解压+JSON 解析额外消耗约 0.47 毫秒 CPU 时间/响应。测试环境是 macOS，不是 Windows CPU 占用实测。

本次不会把 bootstrap 或会话列表的刷新一律降到 10/30/60 秒。这些接口还承载 AI 开关、权限和会话状态；缺少变更通知时降频会增加远端变化的可见延迟。

此外，事件 ACK 的 retryable=false 只表示停止上传重试，不能等同进入回复队列。此前服务端拒绝 hi 的事件也被启动草稿查询，可能查询不存在的会话。本次拒绝/error/ignored/context_traced 的事件不启动草稿查询；rejected 增加 business_rejected 计数及 event_rejected 活动，已持久化消息继续立即查询。上传完成文案改为“上报队列已处理”，不再把终态 ACK 全称为“大脑确认”。SQLite 历史 delivered 记录未改写。

验证：8 项真实回环 HTTP 协议测试通过（gzip、普通响应、POST 正文/鉴权、压缩错误响应、坏头/坏块、空响应及非对象 JSON）；3 项 ACK/查询回归通过；103 项天猫相关测试通过。Windows GUI 全套测试仍因 macOS 缺少 ctypes.WinDLL 无法运行，不声称实际客户送达率已测。

## 高流量本地会话网关

两小时审计里 bootstrap 和 sessions 占出网流量约 94%。对应 LocalSeatState._remote_json 原来没有声明压缩。服务器上的源码也已加入 gzip：

`/lfj/client-src/PddBridgeAgent-v0.5.14-source/run_frontend_service.py`

源码备份：`.rollback/gateway-gzip-20261009-150930/run_frontend_service.py`。没有改正在运行的客户 EXE。

这份源码没有 Git 仓库；对应补丁随本提交的 `docs/pdd-gateway-gzip.patch` 交付。将补丁应用到该网关源码再打包，或使用服务器上已修改的同版本源码。该网关也必须更新，千牛桥接器 1.6.11 并不会替换独立的旧网关 EXE。两项网关单测通过；合成数据回环下载基线和压缩版各 600 次，0 次解析/一致性失败。

## 生效与回滚

千牛使用既有 Windows 打包脚本构建 1.6.11，包含 1.6.10 的发送回执修复。客户安装后才会使用压缩，不需修改服务端配置。保留旧安装包可回滚。尚未测量更新后的实际每日 35 GB 流量，不能把模拟压缩比当作生产节省量。
