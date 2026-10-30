"""情绪状态：一个 0~100 的数，决定它今天"心情好不好"。

为什么必须落库：persona 只决定"怎么说话"，决定不了"做不做"。
没有状态的话，每次请求都是新鲜的热情，它不会累、不会记仇、也不会撒娇。

规则：
  - 50 = 平静；被塞难看内容 / 被使唤太多次 → 降；被投喂好东西 → 升
  - 每天向 50 缓慢回归（不记仇记到死）
  - 情绪只影响**态度**（接受 / 讨价还价 / 拒绝），不影响安全动作
"""

from __future__ import annotations

import time

from . import config as cfgmod
from .logs import get
from .store import atomic_write_json, read_json

log = get("mood")

NEUTRAL = 50
MIN_SCORE = 0
MAX_SCORE = 100
KEEP_EVENTS = 50


def _path(cfg: cfgmod.Config):
    return cfg.paths.state / "mood.json"


def load(cfg: cfgmod.Config) -> dict:
    data = read_json(_path(cfg), {}) or {}
    if "score" not in data:
        data = {"score": NEUTRAL, "events": [], "updated_at": _now()}
    return data


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def current(cfg: cfgmod.Config) -> int:
    return int(load(cfg).get("score", NEUTRAL))


def apply(cfg: cfgmod.Config, kind: str, delta: int, note: str = "") -> int:
    """记一件事对情绪的影响，返回新的情绪值。"""
    data = load(cfg)
    score = max(MIN_SCORE, min(MAX_SCORE, int(data.get("score", NEUTRAL)) + int(delta)))
    data["score"] = score
    data["updated_at"] = _now()
    events = data.get("events") or []
    events.append({"kind": kind, "delta": int(delta), "note": note[:60], "at": _now()})
    data["events"] = events[-KEEP_EVENTS:]
    atomic_write_json(_path(cfg), data)
    return score


def decay(cfg: cfgmod.Config, *, toward: int = NEUTRAL, rate: float = 0.15) -> int:
    """每天向平静回归一次（同一天重复调用不会叠加）。"""
    data = load(cfg)
    today = time.strftime("%Y-%m-%d")
    if data.get("decayed_on") == today:
        return int(data.get("score", NEUTRAL))

    score = int(data.get("score", NEUTRAL))
    score = int(round(score + (toward - score) * rate))
    data["score"] = max(MIN_SCORE, min(MAX_SCORE, score))
    data["decayed_on"] = today
    data["updated_at"] = _now()
    atomic_write_json(_path(cfg), data)
    log.info("情绪回归：%s", data["score"])
    return data["score"]


def summary(cfg: cfgmod.Config) -> str:
    """日报用的一句话。"""
    score = current(cfg)
    if score >= 70:
        mood = "心情很好"
    elif score >= 45:
        mood = "还行"
    elif score >= 25:
        mood = "有点不爽"
    else:
        mood = "很不高兴"
    data = load(cfg)
    recent = (data.get("events") or [])[-3:]
    notes = "；".join(f"{e['note']}({e['delta']:+d})" for e in recent if e.get("note"))
    return f"- 情绪：{score}/100（{mood}）" + (f"，最近：{notes}" if notes else "")
