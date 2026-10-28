"""读取 / 设置千牛客户端的「无障碍模式」开关。

2026-10-29 现场定位：DX 新接待台（dx-h5）的 webview 里没有
`workbench` / `imsdk` / `QN`，页面级 mtop 全部发不出去 —— 但这不是
客户端形态"天生"如此，而是千牛的一个本地开关：

    D:\\AliWorkbenchData\\NewAppData\\globalConfig\\Global.db
      key2value: EnableAccessibility = true | false

`EnableAccessibility` 在 `AppBiz.dll` 里被引用（字符串 ×5），就是设置
界面里的「无障碍模式」。打开它，客户端才会向 webview 注入 `workbench`
等原生 JSAPI；关掉就只有空壳页面。

本模块只做三件事：
  * `state()`   读开关（只读、复制 db+wal 到临时目录，不锁千牛）；
  * `enable()`  在千牛**未运行**时把开关写成 true（先备份，带校验）；
  * `advice()`  给现场一句人话结论。
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("qianniu_standalone.client_settings")

SETTING_KEY = "EnableAccessibility"
RELATIVE_DB = Path("NewAppData") / "globalConfig" / "Global.db"


def candidate_data_dirs() -> list[Path]:
    """千牛数据根目录（`AliWorkbenchData`）候选。"""
    dirs: list[Path] = []
    env = os.environ.get("QNQIANNIU_DATA_DIR")
    if env:
        dirs.append(Path(env))
    for base in ("D:", "C:", "E:", "F:"):
        dirs.append(Path(base + os.sep) / "AliWorkbenchData")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "AliWorkbenchData")
    seen: set[str] = set()
    unique: list[Path] = []
    for item in dirs:
        key = str(item).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def locate_global_db() -> Path | None:
    for root in candidate_data_dirs():
        candidate = root / RELATIVE_DB
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _read_setting(db: Path, key: str) -> tuple[bool, str]:
    """复制 db(+wal/shm) 到临时目录后只读，避免和千牛抢锁。"""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Global.db"
        try:
            shutil.copy2(db, copy)
        except OSError as error:
            return False, f"copy failed: {error}"
        for suffix in ("-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.is_file():
                try:
                    shutil.copy2(side, Path(str(copy) + suffix))
                except OSError:
                    pass
        try:
            connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error as error:
            return False, f"open failed: {error}"
        try:
            row = connection.execute(
                "select value from key2value where key = ?", (key,)
            ).fetchone()
        except sqlite3.Error as error:
            return False, f"query failed: {error}"
        finally:
            connection.close()
    if row is None:
        return False, "key not found"
    return True, str(row[0])


def _as_bool(text: str) -> bool:
    return str(text).strip().lower() in {"1", "true", "yes", "on"}


def state() -> dict[str, Any]:
    """当前无障碍模式状态（不会改动任何东西）。"""
    db = locate_global_db()
    if db is None:
        return {
            "path": "",
            "readable": False,
            "enabled": None,
            "raw": "",
            "error": "Global.db not found",
            "advice": "",
        }
    ok, raw = _read_setting(db, SETTING_KEY)
    enabled = _as_bool(raw) if ok else None
    result: dict[str, Any] = {
        "path": str(db),
        "readable": bool(ok),
        "enabled": enabled,
        "raw": raw if ok else "",
        "error": "" if ok else raw,
    }
    result["advice"] = advice(result)
    return result


def advice(state_payload: dict[str, Any]) -> str:
    """给现场的一句人话；无障碍关着时这就是取不到订单的根因。"""
    enabled = state_payload.get("enabled")
    if enabled is True:
        return ""
    if enabled is False:
        return ("千牛「无障碍模式」是关闭的：客户端不会向页面注入原生 JSAPI，"
                "取订单/商品必然失败。请在千牛 设置 → 通用 → 无障碍模式 打开，"
                "然后重启千牛（助手会自动重连）。")
    if state_payload.get("readable") is False and state_payload.get("path"):
        return f"读不到千牛配置：{state_payload.get('error', '')}"
    return ""


def qianniu_running() -> list[int]:
    """返回正在运行的千牛进程 pid（用 psutil，取不到就返回空）。"""
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return []
    pids: list[int] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = str(process.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in {"aliworkbench.exe", "alirender.exe"}:
            pids.append(int(process.info.get("pid") or 0))
    return [pid for pid in pids if pid]


def enable(db: Path | None = None) -> dict[str, Any]:
    """把 `EnableAccessibility` 写成 true。

    只在千牛**未运行**时允许：运行中改 write-ahead 配置可能被客户端在退出时
    覆盖回旧值，反而让现场以为改成功了。
    """
    pids = qianniu_running()
    if pids:
        return {
            "ok": False,
            "reason": "qianniu_running",
            "pids": pids,
            "message": "请先完全退出千牛，再执行打开无障碍模式。",
        }
    target = db or locate_global_db()
    if target is None:
        return {"ok": False, "reason": "db_not_found", "message": "找不到 Global.db"}
    backup = target.with_name(target.name + f".bak-accessibility-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(target, backup)
    except OSError as error:
        return {"ok": False, "reason": "backup_failed", "message": str(error)[:200]}
    try:
        connection = sqlite3.connect(str(target), timeout=5.0)
        try:
            connection.execute(
                "insert into key2value(key, value) values(?, ?) "
                "on conflict(key) do update set value = excluded.value",
                (SETTING_KEY, "true"),
            )
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error as error:
        return {"ok": False, "reason": "write_failed", "message": str(error)[:200],
                "backup": str(backup)}
    ok, raw = _read_setting(target, SETTING_KEY)
    return {
        "ok": bool(ok and _as_bool(raw)),
        "reason": "" if (ok and _as_bool(raw)) else "verify_failed",
        "value": raw if ok else "",
        "backup": str(backup),
        "message": "已打开无障碍模式，请重新启动千牛。",
    }
