# 浮窗点击会话跳转修复（1.6.17）

## 原因

浮窗点击调用 `/api/v1/open-conversation`。原先只要浏览器桥接有连接，
就调用网页 `QNAbilityCenter.openChat`。当前运行的 dx-h5 页面不含
`abilitycenter`，接口返回 502；原生 WWCmd 入口被跳过。浮窗模式又隐藏了
详情区的 openState，错误不可见。

## 修复

- 优先调用千牛自带 WWCmd，传入本地会话中的店铺账号与买家昵称。
  不传消息正文，仅打开会话；前端只提交 account 和 buyer_id。
- 根据 AliWorkbench.ini 的实际启动版本选择 WWCmd，避免误选旁边尚未
  启动的新版本。原生入口缺失时才使用旧网页入口。
- 用调用之后新产生的 im.uiutil.onConversationChange 通知确认跳转，
  严格比对店铺账号和完整 ccode。使用独立日志游标，不消耗昵称与发送回执。
- 命令退出码为 0 只代表已提交。无法确认时明确提示；确认后才显示已打开。
  浮窗直接显示成功或失败提示。

## 本机诊断证据

- 旧接口实际返回 502：Qianniu native abilitycenter object is unavailable。
- 运行版本为 9.97.86N，同目录存在尚未启用的 9.98.49N。
- 同权限调用 9.97.86N/WWCmd.exe 后，千牛原生日志产生目标店铺的
  onConversationChange，ccode 与本地目标会话完全一致。
- 普通权限命令调用管理员权限千牛时，可能退出码为 0 却未切换；因此不能
  用进程退出码声称跳转成功。
