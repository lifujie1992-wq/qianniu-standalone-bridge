# 1.6.25 自动寻找旧版升级程序

`tools/upgrade_installed.py` 是独立 Windows 图形升级器，打包后单个 EXE 内含已审核的 1.6.25 升级 ZIP，无需目标电脑安装 Python。
它不提供全新安装的千牛运行库，适用于已安装 QianniuAgent 的电脑。

发现顺序：正在运行的 QianniuAgent 进程目录、当前用户和本机的 32/64 位卸载登记、各盘常见 QianniuAIService 目录及常用程序目录。
候选必须同时存在 QianniuAgent.exe 和有效 config.json；去重后在窗口显示目录与版本。
无法自动发现时，提示先打开原助手再重新检测，也保留选择文件夹入口。

升级前校验内嵌 ZIP 与 EXE 的哈希、压缩包路径和允许覆盖的文件清单。
还会按现有千牛 AppBiz.dll 的 PE image size 检查新脚本的兼容 profile。
有发送、订单查询或上传任务未完成时暂停升级，不改程序。

仅停止所选目录的助手进程和它拥有的浮窗 Edge 子进程，不停止千牛，也不停止其他安装目录的助手。
旧程序和数据库快照备份到原安装目录的 upgrade-backups/auto-1.6.25-时间戳。
config.json、state、runtime 不在覆盖清单内；校验允许现有应用自身的配置默认值和稳定设备 ID 迁移，不允许客户凭据被替换。
build 中额外的既有文件保留。所有目录移动前检查解析后的路径仍在选定安装目录或其备份内。
复制或启动失败时恢复原程序、依赖和配置，再启动旧版；发送去重记录和聊天数据库保持原位。

正常完成后自动启动桥接、所需浮窗和控制中心，验证新版 API 版本与 ok。
这不等同于真实接待、订单获取与大脑使用订单的验收。

构建：在已装 PyInstaller 的 Windows x64 Python 环境运行 tools/build_auto_upgrade.py，提供 --payload、--manifest、--output、--work。
`--hooks` 可指定本机已有的完整 Tcl/Tk 打包 hooks。结果 EXE 请求管理员权限以更新原安装位置。
`--check --report 路径` 只校验包和自动发现，不停止或升级程序，可用于打包后检查。

新增 15 项回归覆盖自动发现、路径与私有文件限制、哈希、成功保留数据、启动及复制失败回退、活跃发送拦截、其他安装隔离和配置迁移。
