"""现场命令行：核对大脑工位令牌（只读，不启动助手、不改配置）。

用法（客户机，Python 或打包后的 exe 均可）::

    python tools/check_brain_token.py
    python tools/check_brain_token.py --config "C:\\Users\\x\\AppData\\Local\\QianniuAIService\\config.json"
    python tools/check_brain_token.py --server http://47.107.138.228:18765 --token <TOKEN> --agent-id <ID>

输出三样东西：令牌指纹、服务端原始应答、以及"该找谁修"的结论。
退出码：0 令牌有效；2 服务端拒了令牌；1 网络/配置问题。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brain_auth import describe_brain_error, format_brain_error, token_fingerprint  # noqa: E402

REGISTER_PATH = "/api/bridge/v1/register"
DEFAULT_CONFIG = Path(os.environ.get("LOCALAPPDATA", ".")) / "QianniuAIService" / "config.json"
PLACEHOLDERS = {"", "replace-with-your-brain-agent-id", "replace-with-a-random-token"}


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"找不到配置文件：{path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def probe(config: dict) -> tuple[int, str]:
    server = str(config.get("brain_server_url") or "").strip().rstrip("/")
    token = str(config.get("brain_agent_token") or "").strip()
    agent_id = str(config.get("brain_agent_id") or "").strip()
    device_id = str(config.get("device_id") or "").strip()
    body = json.dumps(
        {"agent_id": agent_id, "agent_name": "令牌核对 CLI", "version": "cli-check"},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        server + REGISTER_PATH,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Agent-Token": token,
            "X-Agent-Id": agent_id,
            "X-Device-Id": device_id,
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return int(getattr(response, "status", 200)), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return int(error.code), error.read().decode("utf-8", "replace")[:400]
    except (OSError, urllib.error.URLError) as error:
        return 0, str(error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对大脑工位令牌是否被服务端接受")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--server", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--agent-id", default="")
    parser.add_argument("--device-id", default="")
    args = parser.parse_args(argv)

    config: dict = {}
    if Path(args.config).is_file():
        config = load_config(Path(args.config))
    for key, value in (
        ("brain_server_url", args.server),
        ("brain_agent_token", args.token),
        ("brain_agent_id", args.agent_id),
        ("device_id", args.device_id),
    ):
        if value:
            config[key] = value

    token = str(config.get("brain_agent_token") or "").strip()
    if not token or token in PLACEHOLDERS:
        print("没有配置大脑工位令牌（brain_agent_token 为空）—— 先在配置窗口填令牌。")
        return 1
    if not str(config.get("brain_server_url") or "").strip():
        print("没有配置大脑地址（brain_server_url 为空）。")
        return 1

    fingerprint = token_fingerprint(token)
    print(f"大脑地址 : {config.get('brain_server_url')}")
    print(f"工位 ID  : {config.get('brain_agent_id') or '(空)'}")
    print(f"设备 ID  : {config.get('device_id') or '(空)'}")
    print(f"令牌指纹 : {fingerprint}（{len(token)} 位，令牌本身不打印）")

    status, detail = probe(config)
    described = describe_brain_error(
        status, detail, token=token, server_url=str(config.get("brain_server_url") or "")
    )
    print(f"服务端   : HTTP {status} {detail.strip()[:200]}")
    print(f"结论     : {format_brain_error(described)}")

    if status == 200:
        try:
            payload = json.loads(detail or "{}")
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("ok") is not False:
            print("令牌有效，注册通过。")
            return 0
    if described["auth"]:
        print("令牌被拒：这是服务端凭据问题，重装客户端/改网络/关代理都不会恢复。")
        return 2
    print("未能完成注册：先排查网络与大脑地址。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
