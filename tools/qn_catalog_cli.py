"""千牛商品/订单一键取数 CLI（走本机桥接的 /api/v1/catalog/*）。

用法：
    py tools/qn_catalog_cli.py status
    py tools/qn_catalog_cli.py shop
    py tools/qn_catalog_cli.py goods --page 1 --page-size 20
    py tools/qn_catalog_cli.py goods --all
    py tools/qn_catalog_cli.py orders --buyer 2217298756354
    py tools/qn_catalog_cli.py history --buyer 2217298756354
    py tools/qn_catalog_cli.py memo --order 1234567890
    py tools/qn_catalog_cli.py mtop --method mtop.taobao.qianniu.cs.item.onsale.query --param '{"pageNo":1,"pageSize":5}'

说明：所有请求都通过千牛页面里的 `invokeMTopChannelService` 执行，
用的是本机已登录商家的会话，不读取/导出任何密码或 cookie。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def call(config: dict, path: str, params: dict[str, str] | None = None) -> dict:
    host = str(config.get("workbench_host") or "127.0.0.1")
    port = int(config.get("workbench_port") or 18776)
    token = str(config.get("workbench_token") or "")
    url = f"http://{host}:{port}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"X-Workbench-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"ok": False, "error": f"HTTP {error.code}: {body[:300]}"}
    except urllib.error.URLError as error:
        return {"ok": False, "error": f"bridge unreachable: {error}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="千牛商品/订单取数 CLI")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--pretty", action="store_true", default=True)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")
    sub.add_parser("shop")

    goods = sub.add_parser("goods")
    goods.add_argument("--page", type=int, default=1)
    goods.add_argument("--page-size", type=int, default=20)
    goods.add_argument("--all", action="store_true")
    goods.add_argument("--max-pages", type=int, default=20)

    orders = sub.add_parser("orders")
    orders.add_argument("--buyer", required=True, help="买家 securityBuyerUid（通常是 10-22 位数字）")
    orders.add_argument("--biz-order-id", default="")

    history = sub.add_parser("history")
    history.add_argument("--buyer", required=True)
    history.add_argument("--page-num", type=int, default=1)
    history.add_argument("--page-size", type=int, default=20)

    memo = sub.add_parser("memo")
    memo.add_argument("--order", required=True)

    mtop = sub.add_parser("mtop")
    mtop.add_argument("--method", required=True)
    mtop.add_argument("--param", default="{}")

    args = parser.parse_args(argv)
    config = load_config(Path(args.config))

    if args.command == "status":
        payload = call(config, "/api/v1/catalog")
    elif args.command == "shop":
        payload = call(config, "/api/v1/catalog/shop")
    elif args.command == "goods":
        params = {
            "page_no": str(args.page),
            "page_size": str(args.page_size),
            "all": "1" if args.all else "0",
            "max_pages": str(args.max_pages),
        }
        payload = call(config, "/api/v1/catalog/goods", params)
    elif args.command == "orders":
        payload = call(config, "/api/v1/catalog/orders", {
            "buyer_encrypt_id": args.buyer,
            "biz_order_id": args.biz_order_id,
        })
    elif args.command == "history":
        payload = call(config, "/api/v1/catalog/orders", {
            "buyer_encrypt_id": args.buyer,
            "history": "1",
            "page_num": str(args.page_num),
            "page_size": str(args.page_size),
        })
    elif args.command == "memo":
        payload = call(config, "/api/v1/catalog/order-memo", {"order_id": args.order})
    else:
        # 校验 param 是合法 JSON，避免把坏参数发给桥接
        try:
            json.loads(args.param)
        except ValueError as error:
            print(json.dumps({"ok": False, "error": f"invalid --param: {error}"}, ensure_ascii=False))
            return 2
        payload = call(config, "/api/v1/catalog/mtop", {
            "method": args.method,
            "param": args.param,
        })

    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
