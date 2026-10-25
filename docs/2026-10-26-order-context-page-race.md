# 1.6.24：订单补全页面竞争与失败记录

基于本机 1.6.23 安装清单对应提交 `41ac29516e7de8dbdd51d55688115014fd14a4bb`。

## 已复现的问题

`BrowserServer.execute()` 向已连接页面广播表达式，默认采用第一个响应。
当没有 `imsdk.invoke` 的页面先返回 `Qianniu imsdk is unavailable`，
即使另一个页面已经返回非空订单，`ContextEnricher` 也会放弃这次有效结果。
空值响应也能触发同样的问题。离线回归测试使用真实 BrowserServer 的响应队列复现。

此外，缺少买家加密 ID 时，原逻辑只增加本机 skipped 计数，不在事件里记录原因；
非联想店铺的通道异常原先也只留在 worker 的 last_error。

## 修复

- 订单调用使用 result_filter：跳过无 SDK 页面错误和空结果，在同一超时窗口内等待包含订单调用结果的 MTop 信封。
- 调用信封中的业务错误仍进入原有重试及证据解析，不视为成功或“没有订单”。
- 全部页面均报 SDK 不可用、连接不存在或全部发送失败时，记录 error_unknown，停止本次相同重试；后续事件可再次尝试。超时仍保留重试。
- 非联想店铺的通道异常同样生成失败信封，保留已有字段结构和业务结果判定。
- 缺少买家 ID 时，在持久化事件中写入 context_enrich.status=error_unknown、error=missing_buyer_encrypt_id；不覆盖已有 order_info，也不写 no_orders。
- 本机 context_enrich 状态增加 enqueue_calls、enqueued、skip_reasons、result_counts，区分 disabled、role_filtered、missing_buyer_encrypt_id、already_queued、stopped，以及 found / confirmed_empty / error_unknown。successes 保留原语义，包含有效空响应；result_counts 同样包括缓存命中。

其他 execute 调用的默认行为保持原状。此修复仍使用原有广播方式，不新增店铺登录态、页面 URL 或跨账号路由规则。

## 验证与边界

新增 13 项离线回归，覆盖错误页面先返回、延迟订单响应、超时、业务失败、空响应、断连、其他店铺失败记录、缺少买家 ID 的持久化证据和诊断计数。

Windows 上运行 `python -m unittest discover -s tests -v`：443 项，440 通过、3 跳过。
跳过项需要仓库外的千牛 DLL、编译适配器或完整打包运行时；本次不把它们计为已验收。

本机 2026-10-25 的近期 11 条买家事件均为 history_snapshot，订单为非空，最新 revision 本地记录为上传成功。
这只能证明当前环境能查询订单，不能证明实时买家首轮回复已经使用订单，也不能解释其他客户端 96% 缺失记录的原因。

上线后以自然到达的新买家消息验收：分别统计实时事件与历史快照，核对 found 数量、跳过原因、最新 revision 的上传确认，并在大脑端核对对应回复的 order_info_provided 和实际订单状态字段。
缺少买家 ID 的根本采集问题、跨账号页面选择和大脑生成时机仍须现场证据；本次不以历史快照代替实时验收。
