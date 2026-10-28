#!/usr/bin/env python3
"""查看 / 打开千牛「无障碍模式」。

现场为什么需要它：
  无障碍模式关闭时，千牛不会向 webview 注入 `workbench` 等原生 JSAPI，
  DX 新接待台（dx-h5）页面里连 imsdk/QN 都没有，取订单必然失败。

用法：
  python tools/qn_accessibility.py            # 只看状态
  python tools/qn_accessibility.py --enable   # 打开（要求千牛已完全退出）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qn_client_settings as settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="千牛无障碍模式开关")
    parser.add_argument("--enable", action="store_true", help="写入 EnableAccessibility=true")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if args.enable:
        result = settings.enable()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    state = settings.state()
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    print(f"配置文件 : {state.get('path') or '(未找到)'}")
    enabled = state.get("enabled")
    label = "开" if enabled is True else "关" if enabled is False else "读不到"
    print(f"无障碍模式: {label}  (raw={state.get('raw')!r})")
    if state.get("error"):
        print(f"错误     : {state['error']}")
    if state.get("advice"):
        print(f"\n!! {state['advice']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
