"""运行时状态：本地大模型连通性缓存等（避免每次渲染页面都去探测网络）。"""
from __future__ import annotations

import time

_STATUS: dict = {"ok": False, "msg": "未检测", "ts": 0.0}


def llm_status() -> tuple[bool, str]:
    return bool(_STATUS["ok"]), str(_STATUS["msg"])


def set_llm_status(ok: bool, msg: str) -> None:
    _STATUS.update(ok=ok, msg=msg, ts=time.time())


def probe_llm(timeout: float = 2.5) -> tuple[bool, str]:
    from .llm import check_connection
    ok, msg = check_connection(timeout=timeout)
    set_llm_status(ok, msg)
    return ok, msg


def llm_age() -> float:
    return time.time() - float(_STATUS["ts"])
