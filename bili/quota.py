"""按小时计账：读 / 写 / 模型调用。

为什么要有：日预算挡不住"一小时里聊 50 句"。小红书式的风控看的是短时频率，
所以除了每日上限，还需要一个每小时的天花板。小时一翻就自动归零。
"""

from __future__ import annotations

import time

from . import config as cfgmod
from .store import atomic_write_json, read_json

KINDS = ("read", "write", "llm")


def _path():
    return cfgmod.resolve_root() / "state" / "hourly.json"


def _hour() -> str:
    return time.strftime("%Y-%m-%dT%H")


def _load() -> dict:
    data = read_json(_path(), {}) or {}
    if data.get("hour") != _hour():
        return {"hour": _hour(), "read": 0, "write": 0, "llm": 0}
    return data


def used(kind: str) -> int:
    return int(_load().get(kind) or 0)


def bump(kind: str, n: int = 1) -> int:
    data = _load()
    data[kind] = int(data.get(kind) or 0) + n
    atomic_write_json(_path(), data)
    return data[kind]


def limit(cfg: cfgmod.Config, kind: str) -> int:
    return int(getattr(cfg.guard.hourly, kind, 0))


def over(cfg: cfgmod.Config, kind: str) -> bool:
    cap = limit(cfg, kind)
    return cap > 0 and used(kind) >= cap
