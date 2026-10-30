"""按 UID 分开的对话上下文。

每个对话对象单独存一份上下文，互不可见。
只保留最近 10 轮、24 小时内的内容：留太久就变成监控了。
"""

from __future__ import annotations

import time

from . import config as cfgmod
from .store import atomic_write_json, read_json

MAX_TURNS = 10
MAX_AGE_HOURS = 24
MAX_TEXT = 300


def _path(cfg: cfgmod.Config, uid: int):
    return cfg.paths.state / "context" / f"{uid}.json"


def load(cfg: cfgmod.Config, uid: int) -> list[dict]:
    data = read_json(_path(cfg, uid), {}) or {}
    turns = data.get("turns") or []
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    fresh = [t for t in turns if int(t.get("ts") or 0) >= cutoff]
    return fresh[-MAX_TURNS:]


def append(cfg: cfgmod.Config, uid: int, role: str, text: str) -> None:
    """role: user=对方说的话，pet=它自己说的。"""
    turns = load(cfg, uid)
    turns.append({"role": role, "text": " ".join(text.split())[:MAX_TEXT], "ts": int(time.time())})
    atomic_write_json(
        _path(cfg, uid),
        {"uid": uid, "updated_at": int(time.time()), "turns": turns[-MAX_TURNS:]},
    )


def render(cfg: cfgmod.Config, uid: int, own_name: str = "你") -> str:
    """把上下文拼成给模型看的几行；空上下文返回空串。"""
    turns = load(cfg, uid)
    if not turns:
        return ""
    lines = []
    for turn in turns:
        speaker = "对方" if turn["role"] == "user" else own_name
        lines.append(f"{speaker}：{turn['text']}")
    return "\n".join(lines)


def clear(cfg: cfgmod.Config, uid: int) -> None:
    try:
        _path(cfg, uid).unlink()
    except OSError:
        pass
