"""「指定 @ 某人一次」的任务队列。

规则（和两个人商量过的）：
  - 只有你能下指令，且**必须给 UID**（只给昵称不执行，防止 @ 错人）
  - 每条授权**只用一次**，24 小时过期
  - 每天最多 2 条授权
  - 被 @ 的人**不进白名单**（不能给它发私信），也不会被主动搭讪

存在 state/mention_tasks.json。
"""

from __future__ import annotations

import time

from . import config as cfgmod
from .client import API, BiliError, Client
from .logs import get
from .store import atomic_write_json, read_json

log = get("mentions")

TTL_SECONDS = 24 * 3600
MAX_PER_DAY = 2


def _path(cfg: cfgmod.Config):
    return cfg.paths.state / "mention_tasks.json"


def _load(cfg: cfgmod.Config) -> list[dict]:
    data = read_json(_path(cfg), {}) or {}
    return [t for t in (data.get("tasks") or []) if not t.get("used")]


def _save(cfg: cfgmod.Config, tasks: list[dict]) -> None:
    atomic_write_json(_path(cfg), {"tasks": tasks[-50:]})


def name_of(client: Client, uid: int) -> str:
    """从卡片接口取昵称；取不到说明 UID 不对，不允许 @。"""
    try:
        payload = client.get_payload(f"{API}/x/web-interface/card", {"mid": uid})
    except BiliError as exc:
        log.warning("查 UID %s 失败：%s", uid, exc)
        return ""
    card = (payload.get("data") or {}).get("card") or {}
    return str(card.get("name") or "")


def add(
    client: Client,
    cfg: cfgmod.Config,
    *,
    target_uid: int,
    from_uid: int,
    bvid: str = "",
) -> tuple[bool, str]:
    """下一条 @ 授权。**必须指定视频**，否则不知道在哪 @。"""
    if target_uid <= 0:
        return False, "UID 看起来不对，再发一次完整的数字给我。"
    if not bvid:
        return False, "在哪条视频下面 @ 呀？把 BV 号也发给我。"

    # 校验视频存在
    try:
        video = client.get_payload(f"{API}/x/web-interface/view", {"bvid": bvid})
        title = ((video.get("data") or {}).get("title") or "")[:30]
    except BiliError as exc:
        log.warning("校验视频 %s 失败：%s", bvid, exc)
        return False, f"这个 BV 号我没查到（{bvid}），是不是抄错了？"

    tasks = _load(cfg)
    today = time.strftime("%Y-%m-%d")
    todays = [t for t in tasks if str(t.get("added_at", ""))[:10] == today]
    if len(todays) >= MAX_PER_DAY:
        return False, f"今天已经存了 {MAX_PER_DAY} 条 @ 指令了，明天再说吧。"

    if any(int(t.get("uid", 0)) == target_uid for t in tasks):
        return False, "这条已经记着了，等下一条视频就会 @ 他。"

    name = name_of(client, target_uid)
    if not name:
        return False, "这个 UID 我没查到人，可能输错了。"

    tasks.append(
        {
            "uid": target_uid,
            "name": name,
            "bvid": bvid,
            "video_title": title,
            "from_uid": from_uid,
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "expires_at": int(time.time()) + TTL_SECONDS,
            "used": False,
        }
    )
    _save(cfg, tasks)
    log.info("记下 @ 指令：%s(%s) 由 %s 下达", name, target_uid, from_uid)
    return True, f"记住啦～会在《{title}》下面 @ {name}，只用一次，24 小时内有效。"


def consume(cfg: cfgmod.Config) -> dict | None:
    """取一条还没过期的授权，并标记为已用。"""
    now = int(time.time())
    tasks = _load(cfg)
    alive = [t for t in tasks if int(t.get("expires_at", 0)) > now]
    expired = len(tasks) - len(alive)
    if expired:
        log.info("丢弃 %s 条过期 @ 指令", expired)

    if not alive:
        _save(cfg, alive)
        return None

    task = alive.pop(0)
    task["used"] = True
    _save(cfg, alive)
    return task


def pending_count(cfg: cfgmod.Config) -> int:
    now = int(time.time())
    return sum(1 for t in _load(cfg) if int(t.get("expires_at", 0)) > now)
