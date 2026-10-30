"""推荐流。只读，且不模拟观看行为（PLAN §5 红线 6）。"""

from __future__ import annotations

import random

from .client import API, BiliError, Client
from .logs import get
from .video import Video, detail

log = get("feed")

RCMD = f"{API}/x/web-interface/wbi/index/top/feed/rcmd"

_BASE_PARAMS = {
    "web_location": 1430650,
    "y_num": 5,
    "fresh_type": 4,
    "feed_version": "V8",
    "fresh_idx_1h": 1,
    "homepage_ver": 1,
}


def _from_item(item: dict) -> Video | None:
    # 推荐流里混着直播、广告位、番剧卡，P1 只要普通稿件
    if item.get("goto") != "av":
        return None
    bvid = item.get("bvid")
    title = (item.get("title") or "").strip()
    if not bvid or not title:
        return None
    owner = item.get("owner") or {}
    stat = item.get("stat") or {}
    reason = item.get("rcmd_reason")
    if isinstance(reason, dict):
        reason = reason.get("content") or ""
    return Video(
        bvid=bvid,
        title=title,
        aid=item.get("id"),
        cid=item.get("cid"),
        up_mid=owner.get("mid") or 0,
        up_name=owner.get("name") or "",
        duration=int(item.get("duration") or 0),
        pubdate=int(item.get("pubdate") or 0),
        view=int(stat.get("view") or 0),
        like=int(stat.get("like") or 0),
        reply=int(stat.get("danmaku") or 0),
        rcmd_reason=str(reason or "").strip(),
    )


def fetch_recommend(
    client: Client,
    *,
    pages: int = 2,
    ps: int = 12,
    rng: random.Random | None = None,
) -> list[Video]:
    """brush/fresh_idx 是「这是本次会话第几次刷新」，不随机会连着两次拿到同一批。"""
    rng = rng or random.Random()
    videos: list[Video] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        params = dict(
            _BASE_PARAMS,
            fresh_idx=page,
            fresh_idx_1h=rng.randint(1, 3),
            brush=rng.randint(0, 9),
            ps=ps,
        )
        try:
            payload = client.get_wbi(RCMD, params)
        except BiliError as exc:
            log.warning("第 %s 页推荐流失败：%s", page, exc)
            break
        items = (payload.get("data") or {}).get("item") or []
        for item in items:
            video = _from_item(item)
            if video and video.bvid not in seen:
                seen.add(video.bvid)
                videos.append(video)
        log.info("推荐流第 %s 页：%s 条", page, len(items))
    return videos


def enrich(client: Client, videos: list[Video], *, limit: int | None = None) -> list[Video]:
    """补分区/简介。单条失败就保留原对象，不影响整批。"""
    out: list[Video] = []
    for index, video in enumerate(videos):
        if limit is not None and index >= limit:
            out.extend(videos[index:])
            break
        try:
            full = detail(client, video.bvid)
        except BiliError as exc:
            log.warning("详情失败 %s：%s", video.bvid, exc)
            out.append(video)
            continue
        full.rcmd_reason = full.rcmd_reason or video.rcmd_reason
        out.append(full)
    return out
