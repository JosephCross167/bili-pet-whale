"""「@我的」通知处理。

两种来源，两种待遇：

  白名单（只有你）@ 它
      → 按**私信标准**处理：能聊天、能执行小指令（点赞、推荐、看看这个视频）
      → 在评论区楼中楼回你
      另外：如果句子里带 UID + BV，就走「指定 @ 某人」的指令

  陌生人 @ 它
      → 按**陌生人评论**处理：只回一次、只回问句、每天有上限
      → 也走楼中楼

只有白名单的聊天会进上下文；陌生人只记录。
"""

from __future__ import annotations

import re
import time

from . import config as cfgmod
from .client import API, BiliError, Client
from .logs import get
from .store import atomic_write_json, read_json

log = get("at_mentions")

UID_RE = re.compile(r"uid\s*(?:[=:：是为]|equals?)?\s*(\d{5,12})", re.I)
BARE_UID_RE = re.compile(r"(?<!\d)(\d{6,12})(?!\d)")
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
LIKE_WORDS = ("点个赞", "点赞", "赞一下", "点一下赞")
RECOMMEND_WORDS = ("类似", "推荐", "还有没有", "相关的")


def _seen_path(cfg: cfgmod.Config):
    return cfg.paths.state / "at_seen.json"


def parse(text: str, default_bvid: str = "") -> tuple[int | None, str]:
    """从一句话里解析 (目标 uid, 视频 bvid)。"""
    uid = None
    match = UID_RE.search(text or "")
    if match:
        uid = int(match.group(1))
    elif "@" in (text or "") or "艾特" in (text or ""):
        bare = BARE_UID_RE.search(text or "")
        if bare:
            uid = int(bare.group(1))

    bvid_match = BV_RE.search(text or "")
    return uid, (bvid_match.group(0) if bvid_match else default_bvid)


def wants_like(text: str) -> bool:
    return any(word in (text or "") for word in LIKE_WORDS)


def wants_recommend(text: str) -> bool:
    return any(word in (text or "") for word in RECOMMEND_WORDS)


MAX_ATTEMPTS = 3


def mark(cfg: cfgmod.Config, item_id: str, ok: bool) -> None:
    """处理完再过标记：失败的会重试，最多 3 次。"""
    data = read_json(_seen_path(cfg), {}) or {}
    entry = data.get(str(item_id))
    if isinstance(entry, (int, float)):
        entry = {"at": int(entry), "attempts": 0, "ok": False}
    entry = entry or {"at": int(time.time()), "attempts": 0, "ok": False}
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["ok"] = bool(ok)
    entry["at"] = int(time.time())
    data[str(item_id)] = entry
    atomic_write_json(
        _seen_path(cfg), dict(sorted(data.items(), key=lambda kv: -int(kv[1].get("at", 0) if isinstance(kv[1], dict) else kv[1]))[:300])
    )


def collect(cfg: cfgmod.Config, client: Client) -> list[dict]:
    """拉「@我的」，返回还没处理过（或处理失败可重试）的条目。"""
    seen = read_json(_seen_path(cfg), {}) or {}
    try:
        payload = client.get_payload(f"{API}/x/msgfeed/at", {"pn": 1, "ps": 20})
    except BiliError as exc:
        log.warning("拉「@我的」失败：%s", exc)
        return []

    out: list[dict] = []
    for item in (payload.get("data") or {}).get("items") or []:
        key = str(item.get("id") or "")
        if not key:
            continue
        prev = seen.get(key)
        if isinstance(prev, dict):
            if prev.get("ok") or int(prev.get("attempts", 0)) >= MAX_ATTEMPTS:
                continue
        elif prev is not None:
            continue  # 老格式（只存时间戳）当已处理

        user = item.get("user") or {}
        body = item.get("item") or {}
        mid = int(user.get("mid") or 0)
        uri = body.get("uri") or ""
        out.append(
            {
                "id": key,
                "uid": mid,
                "name": user.get("nickname") or str(mid),
                # 评论正文在 source_content；title 是视频标题
                "text": (body.get("source_content") or body.get("title") or "").strip(),
                "bvid": uri.rsplit("/", 1)[-1] if "/" in uri else "",
                "comment_rpid": body.get("source_id"),
                "root_rpid": int(body.get("root_id") or 0),
                "is_root_comment": not int(body.get("root_id") or 0),
                "whitelisted": cfg.targets.is_allowed(mid),
            }
        )

    return out
