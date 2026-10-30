"""记录自己发过的评论，并追踪「谁回复了我」。

两个用途：
1. 日报里列出今天谁回复了它；
2. 把陌生人回复和你们的回复分开——陌生人的只记录，不自动回。

状态文件：
  state/my_comments.json   # 我发过的评论（rpid / aid / 正文 / 时间 / @了谁）
  state/replies_seen.json  # 已经记过的回复 rpid，避免重复
"""

from __future__ import annotations

import time

from . import config as cfgmod
from .client import API, BiliError, Client
from .logs import get
from .store import atomic_write_json, read_json

log = get("replies")

KEEP_DAYS = 7


def _comments_path(cfg: cfgmod.Config):
    return cfg.paths.state / "my_comments.json"


def _seen_path(cfg: cfgmod.Config):
    return cfg.paths.state / "replies_seen.json"


def record_comment(
    cfg: cfgmod.Config,
    *,
    rpid: int,
    aid: int,
    bvid: str,
    text: str,
    mentioned: str = "",
) -> None:
    data = read_json(_comments_path(cfg), {}) or {}
    items = data.get("items") or []
    items.append(
        {
            "rpid": rpid,
            "aid": aid,
            "bvid": bvid,
            "text": text[:80],
            "mentioned": mentioned,
            "at": int(time.time()),
        }
    )
    data["items"] = items[-200:]
    atomic_write_json(_comments_path(cfg), data)


def _recent_comments(cfg: cfgmod.Config, days: int = KEEP_DAYS) -> list[dict]:
    data = read_json(_comments_path(cfg), {}) or {}
    cutoff = int(time.time()) - days * 86400
    return [item for item in (data.get("items") or []) if int(item.get("at") or 0) >= cutoff]


def _replied_path(cfg: cfgmod.Config):
    return cfg.paths.state / "replied.json"


def from_msgfeed(cfg: cfgmod.Config, client: Client, *, pages: int = 1) -> list[dict]:
    """B 站「回复我的」消息列表——比自查评论更全，能捞到所有回我们的评论。

    返回字段：rpid（对方那条回复）/ root_rpid / aid / bvid / uid / name /
    my_comment（被回复的我方评论原文）/ whitelisted。
    """
    out: list[dict] = []
    for pn in range(1, pages + 1):
        try:
            payload = client.get_payload(f"{API}/x/msgfeed/reply", {"pn": pn, "ps": 30})
        except BiliError as exc:
            log.warning("拉「回复我的」失败：%s", exc)
            break
        for item in (payload.get("data") or {}).get("items") or []:
            user = item.get("user") or {}
            body = item.get("item") or {}
            rpid = body.get("source_id")
            if not rpid:
                continue
            mid = int(user.get("mid") or 0)
            out.append(
                {
                    "rpid": str(rpid),
                    "root_rpid": body.get("root_id"),
                    "aid": body.get("subject_id"),
                    "bvid": (body.get("uri") or "").rsplit("/", 1)[-1],
                    "uid": mid,
                    "name": user.get("nickname") or str(mid),
                    "my_comment": (body.get("title") or "").strip(),
                    "whitelisted": cfg.targets.is_allowed(mid),
                }
            )
    return out


def fetch_text(client: Client, aid: int, root_rpid: int, rpid: str, *, ps: int = 50) -> str:
    """取某条回复的正文。"""
    try:
        payload = client.get_payload(
            f"{API}/x/v2/reply/reply",
            {"oid": aid, "type": 1, "root": root_rpid, "ps": ps, "pn": 1},
        )
    except BiliError as exc:
        log.warning("拉楼失败 %s：%s", root_rpid, exc)
        return ""
    for reply in (payload.get("data") or {}).get("replies") or []:
        if str(reply.get("rpid")) == str(rpid):
            return ((reply.get("content") or {}).get("message") or "").strip()
    return ""


def replied_set(cfg: cfgmod.Config) -> set[str]:
    data = read_json(_replied_path(cfg), {}) or {}
    return set(data.get("rpids") or [])


def mark_replied(cfg: cfgmod.Config, rpid: str | int) -> None:
    data = read_json(_replied_path(cfg), {}) or {}
    rpids = set(data.get("rpids") or [])
    rpids.add(str(rpid))
    data["rpids"] = sorted(rpids)[-500:]
    atomic_write_json(_replied_path(cfg), data)


def find_comment(cfg: cfgmod.Config, root_rpid: int | str) -> dict | None:
    for item in _recent_comments(cfg):
        if str(item.get("rpid")) == str(root_rpid):
            return item
    return None


def pending_replies(cfg: cfgmod.Config, items: list[dict]) -> list[dict]:
    """还没回过的、白名单里的人的回复。"""
    done = replied_set(cfg)
    return [item for item in items if item.get("whitelisted") and str(item.get("rpid")) not in done]


def collect_new(cfg: cfgmod.Config, client: Client, *, include_seen: bool = False) -> list[dict]:
    """拉取最近评论下的新回复。返回这次新出现的（不含自己）。"""
    seen = read_json(_seen_path(cfg), {}) or {}
    comments = _recent_comments(cfg)
    if not comments:
        return []

    fresh: list[dict] = []
    for comment in comments:
        try:
            payload = client.get_payload(
                f"{API}/x/v2/reply/reply",
                {"oid": comment["aid"], "type": 1, "root": comment["rpid"], "ps": 20, "pn": 1},
            )
        except BiliError as exc:
            log.warning("拉回复失败 %s：%s", comment["rpid"], exc)
            continue

        for reply in (payload.get("data") or {}).get("replies") or []:
            rpid = str(reply.get("rpid"))
            if rpid in seen and not include_seen:
                continue
            member = reply.get("member") or {}
            mid = member.get("mid")
            if not mid or int(mid) == cfg.account.uid:
                continue
            is_new = rpid not in seen
            seen[rpid] = int(time.time())
            fresh.append(
                {
                    "root_rpid": comment["rpid"],
                    "rpid": rpid,
                    "uid": int(mid),
                    "name": member.get("uname") or str(mid),
                    "text": ((reply.get("content") or {}).get("message") or "")[:120],
                    "at": int(reply.get("ctime") or time.time()),
                    "whitelisted": cfg.targets.is_allowed(int(mid)),
                    "is_new": is_new,
                }
            )

    if fresh:
        atomic_write_json(_seen_path(cfg), seen)
        log.info("发现 %s 条新回复", len(fresh))
    return fresh


def _attempt_path(cfg: cfgmod.Config):
    return cfg.paths.state / "reply_attempts.json"


def bump_attempt(cfg: cfgmod.Config, rpid: str | int) -> int:
    """记录一次失败尝试，返回累计次数。"""
    path = _attempt_path(cfg)
    data = read_json(path, {}) or {}
    key = str(rpid)
    data[key] = int(data.get(key, 0)) + 1
    atomic_write_json(path, data)
    return data[key]


def attempts(cfg: cfgmod.Config, rpid: str | int) -> int:
    data = read_json(_attempt_path(cfg), {}) or {}
    return int(data.get(str(rpid), 0))
