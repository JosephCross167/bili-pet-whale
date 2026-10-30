"""写完之后「有没有真的被看见」——只读复查，不发任何东西。

起因是一次实测：新号的第一条评论发出去返回 code=0，但它被置成「仅自己可见」，
人工申诉之后才变正常（state 0 → 别人可见）。所以「接口成功」不能当成功用，
每条写操作都要有一条独立于接口返回的可见性证据。
"""

from __future__ import annotations

import time

from . import config as cfgmod
from .client import Client
from .logs import get
from .store import atomic_write_json, read_json

log = get("watch")

COMMENT_STATE_OK = 0
COMMENT_STATE_HIDDEN = 17
REPLY_PATH = "https://api.bilibili.com/x/v2/reply/reply"


def _path(cfg: cfgmod.Config):
    return cfg.paths.state / "comments.json"


def record_comment(
    cfg: cfgmod.Config,
    *,
    rpid: int | str,
    aid: int,
    bvid: str,
    message: str,
    state: int = COMMENT_STATE_HIDDEN,
) -> None:
    path = _path(cfg)
    data = read_json(path, {}) or {}
    comments = data.get("comments") or {}
    comments[str(rpid)] = {
        "aid": aid,
        "bvid": bvid,
        "message": message[:200],
        "posted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "state": state,
    }
    atomic_write_json(path, {"comments": comments})


MAX_CHECKS_PER_RUN = 6
CHECK_WINDOW_DAYS = 3


def check_comments(client: Client, cfg: cfgmod.Config) -> list[dict]:
    """复查已发评论是否可见。返回仍不可见的（需要人工申诉）。

    常驻模式下这条每轮都会跑，所以只查「最近 3 天」且「不是明确可见」的，
    并且一次最多查 MAX_CHECKS_PER_RUN 条——否则评论攒多了会变成每轮几十个请求。
    """
    path = _path(cfg)
    data = read_json(path, {}) or {}
    comments = data.get("comments") or {}
    if not comments:
        return []

    cutoff = time.time() - CHECK_WINDOW_DAYS * 86400
    pending: list[dict] = []
    checked = 0

    for rpid, item in comments.items():
        if int(item.get("state", -1)) == COMMENT_STATE_OK:
            continue
        posted = item.get("posted_at") or ""
        try:
            posted_ts = time.mktime(time.strptime(posted, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            posted_ts = 0
        if posted_ts and posted_ts < cutoff:
            continue
        if checked >= MAX_CHECKS_PER_RUN:
            break
        checked += 1

        payload = client.get_payload(
            REPLY_PATH,
            {"oid": item.get("aid"), "type": 1, "root": rpid, "ps": 1, "pn": 1},
        )
        root = (payload.get("data") or {}).get("root") or {}
        state = root.get("state")
        if state is None:
            continue
        item["state"] = int(state)
        item["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if int(state) != COMMENT_STATE_OK:
            pending.append({"rpid": rpid, **item})

    atomic_write_json(path, {"comments": comments})
    if pending:
        log.warning("%s 条评论仍不可见，需要人工申诉", len(pending))
    return pending
