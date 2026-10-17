# 浮窗「开启 / 暂停 AI 回复」（1.6.15）

老板口径（2026-10-17）：**拼多多浮窗已经能在浮窗上开启、暂停 AI 回复，千牛这边一直缺失。**

## 现状（1.6.14）

* 浮窗（dock 模式，300×720，贴在千牛接待中心旁边）**只显示会话列表**：
  `body.dock .conversation-pane { display: none; }`，所以工作台上的「AI / 人工」按钮在浮窗里根本够不着。
* 即便打开完整工作台，那两个按钮**也是坏的**：客户端发的是
  `POST /api/bridge/v1/events {type: "session_control"}`，而大脑侧**没有这个 event 类型**
  （`pdd_brain_bridge_events` 里历史上一条都没有），客户端又要求 ACK `session_control_applied`，
  于是必然抛「大脑未确认会话接管状态」——点了没反应。
* 真要暂停 AI，只能由大脑侧（运营后台）设 `ai_takeover_override=false`。

## 本版改了什么

### 客户端：走大脑已经开放给工位的接口

`POST /api/session/state`（`pdd_brain/operation_routes.py::api_session_state`，`_seat_api_allowed` 白名单里本来就有）
才是大脑给坐席的正式契约：

```json
{"account": "联想官方旗舰店:瑞秋", "buyer_id": "...", "ai_takeover_enabled": false, "reason": "..."}
→ {"ok": true, "ai_takeover_enabled": false, "ai_takeover_state": "paused",
   "shop_ai_takeover_enabled": true, "sticky_handoff": false}
```

* 新增 `BrainConnector.set_ai_takeover(account, buyer_id, enabled)`：调这个接口，**只落大脑确认后的状态**
  到本地 `session_controls`（source `brain` / `brain_pause`），本机不自己改发送规则。
* `set_tmall_session_mode()`（工作台的 AI/人工按钮）改为调用它 —— 顺手把两个按钮修好。
* `/api/v1/session/control` 不再按「是否联想官方旗舰店」分流：只要配置了大脑就一律走大脑确认，
  否则其它淘宝店点了开关会**静默只写本地**（AI 照常回复，同属"点了没反应"）。没配置大脑时才退回本地。
* 暂停 ≠ 转人工：只有店铺开了 sticky handoff 时大脑才会同时置 `handoff=true`；
  否则该会话只是「AI 暂停」，不会进待人工队列（与拼多多的 `manual_ai_pause` 口径一致）。

### 浮窗：每个会话一行一个 AI 开关

dock 模式下每个会话行右侧新增一枚芯片：

* `AI 开启`（绿）→ 点击暂停该会话的 AI 回复；
* `AI 暂停中`（橙）→ 点击恢复；
* 点击/回车都 `stopPropagation`，**不会顺带打开千牛会话**；
* 请求失败（大脑没确认）会用底部 toast 报错并保持原状态，不做乐观切换；
* 完整工作台的布局与按钮**不变**（芯片只在 dock 模式渲染）。

作用范围是**单个会话**，不影响同店其它买家——这正是客服"我先自己回两句"的场景。

## 大脑侧配套（同一批改动，两个仓库）

* `bridge/hub_state.py::enqueue_session_state(..., ai_takeover_enabled=None)`：
  下发给坐席的 `session_state` 命令现在带**独立的** AI 开关字段，不再由 `handoff` 反推。
* `pdd_brain/sending.py::notify_bridge_session_state(..., ai_takeover_enabled=None)`：透传。
* `pdd_brain/operation_routes.py::api_session_state`：暂停（`ai_takeover_enabled=false` 且不转人工）
  以前算出 `bridge_handoff=None` → **根本不通知坐席**，浮窗会一直显示「AI 可回复」；
  现在暂停也会推一条 `session_state`，坐席据此把该会话标成暂停。

老坐席只认 `handoff` / `meta.ai_takeover_enabled`（后者本来就是 `not handoff`），字段形状没变，
所以**向后兼容**。

## 回归测试

客户端 `tests/test_ai_toggle_control.py`（10 条）：

* 暂停/恢复都真的走 `POST /api/session/state`，本地状态与大脑确认一致；
* 大脑拒绝时抛错且**本地状态不变**；
* 大脑返回"实际仍是暂停"（sticky handoff）时以大脑为准，不做乐观置 `ai`；
* 工作台旧入口 `set_tmall_session_mode` 委托到同一实现；
* 浮窗芯片存在、`stopPropagation`、调 `/api/v1/session/control`、失败有 toast、
  完整工作台不受影响。

大脑 `tests/test_ai_takeover_seat_signal.py`（5 条）：

* `handoff=false + ai_takeover_enabled=false` 的命令 meta 正确；
* 恢复时不带 handoff；
* 转人工的 meta 形状与改动前逐字一致（老坐席兼容）；
* `notify_bridge_session_state` 透传，老调用方缺省值仍按 `not handoff` 推导。

全量：客户端 203 passed（改前 179 + 14 停摆护栏 + 10 本功能）。

## 打包与验收

1. 打包前把 `installer/QianniuAIService.iss` 的版本对齐 1.6.15（同其它版本发布惯例）。
2. 装好后：浮窗点某会话的 `AI 开启` → 变 `AI 暂停中`；该买家下一条消息**不再自动回复**；
   点回 `AI 暂停中` → 恢复。其它会话不受影响。
3. 大脑侧核对：`pdd_brain_conversations` 里该会话 `ai_takeover_override=false`；
   `/api/sessions` 里 `ai_takeover_state="paused"`。

## 回滚

* 客户端：revert 本分支；或运行时把浮窗芯片隐藏（`body.dock .ai-toggle{display:none}`）。
* 大脑：revert 上述三处；旧行为是"暂停不通知坐席"，不影响发送。
