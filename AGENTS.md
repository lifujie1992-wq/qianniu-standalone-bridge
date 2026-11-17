# 项目协作约定

- 用户长期要求：本项目每次完成代码更新，都要提交并推送到 GitHub，不能只留在本地。
- 推送前检查当前分支和远端状态，使用正常推送，保留远端已有提交。若网络或认证阻止推送，保留本地提交并如实说明尚未同步。

## Cloud Agent

- 依赖安装：`sudo python3 -m pip install --break-system-packages -r requirements.txt`。配置对话框依赖系统包 `python3-tk`。镜像里的 Python 3.12 满足仓库要求的 3.10+。
- 跑测试或本地工作台前设置 `QN_BRAIN_SERVER_URL=http://127.0.0.1:1`，避免连到 `brain_endpoint.py` 里写死的线上大脑。
- 可移植检查：`QN_BRAIN_SERVER_URL=http://127.0.0.1:1 python3 -m unittest discover -s tests -p 'test_*.py'`，以及 `node --test tests/browser_bridge_contract.test.js`。
- `docked_workbench.py` 在导入时调用 `ctypes.WinDLL("user32")`。Linux 上因此无法加载 `tests/test_standalone.py`、`tests/test_bridge_health.py` 和 `tests/test_tray_lifecycle.py`。千牛进程、原生发送和 Windows 打包不在这个环境里。
- 不要用 `config.example.json` 直接启动 `standalone_bridge.py`。非 Windows 会因为读不到 MachineGuid 退出，而且默认配置会把大脑地址钉回线上。
