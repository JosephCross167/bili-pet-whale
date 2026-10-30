"""给「小鲸鱼自己的口味」拍一张快照。

为什么：我们正在用你们的画像去"养"它的推荐流，几周后它的信息源会被改变。
这份快照是它的**原生口味基线**——以后要回答"它本来喜欢什么""是不是被我们带偏了"，
就有东西可对比。

存两份：
  state/pet_taste.json                  最新一份（覆盖）
  state/pet_taste_history.jsonl         每次快照一行摘要（追加）
  state/pet_taste/<日期>.json           每次的完整明细

采集内容：推荐流 tag 分布、宠物号自己收藏夹的条目、它发过的评论。
"""

from __future__ import annotations

import json
import time
from collections import Counter

from . import config as cfgmod
from . import taste
from .client import API, BiliError, Client
from .logs import get
from .store import atomic_write_json, read_json

log = get("pet_taste")

FEED_SAMPLE = 30
FAV_FOLDER_NAME = "我的收藏"


def _feed_tags(client: Client, sample: int = FEED_SAMPLE) -> tuple[list[dict], Counter]:
    from . import feed

    pool = feed.fetch_recommend(client, pages=3)
    items: list[dict] = []
    counter: Counter = Counter()
    for video in pool[:sample]:
        tags = taste.tags_of(client, video.bvid)
        items.append({"bvid": video.bvid, "title": video.title[:60], "tags": tags})
        for tag in tags:
            counter[tag] += 1
    return items, counter


def _favorite_folder(cfg: cfgmod.Config, client: Client, name: str = FAV_FOLDER_NAME) -> list[dict]:
    try:
        payload = client.get_payload(
            f"{API}/x/v3/fav/folder/created/list-all", {"up_mid": cfg.account.uid}
        )
    except BiliError as exc:
        log.warning("列收藏夹失败：%s", exc)
        return []

    folders = ((payload.get("data") or {}).get("list")) or []
    target = next((f for f in folders if (f.get("title") or "") == name), None)
    if not target:
        return []

    out: list[dict] = []
    for page in (1, 2, 3):
        try:
            data = client.get_payload(
                f"{API}/x/v3/fav/resource/list",
                {"media_id": int(target["id"]), "ps": 20, "pn": page, "platform": "web"},
            )
        except BiliError:
            break
        medias = (data.get("data") or {}).get("medias") or []
        out.extend(
            {
                "bvid": m.get("bvid"),
                "title": (m.get("title") or "")[:60],
                "up": (m.get("upper") or {}).get("name") or "",
            }
            for m in medias
        )
        if not (data.get("data") or {}).get("has_more"):
            break
    return out


def snapshot(cfg: cfgmod.Config, client: Client) -> dict:
    feed_items, feed_tags = _feed_tags(client)
    favorites = _favorite_folder(cfg, client)
    comments = (read_json(cfg.paths.state / "my_comments.json", {}) or {}).get("items") or []

    data = {
        "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "原生口味基线（养推荐流之前）",
        "feed": {
            "sampled": len(feed_items),
            "top_tags": feed_tags.most_common(30),
            "items": feed_items,
        },
        "favorites": favorites,
        "comments": [
            {"bvid": c.get("bvid"), "text": c.get("text"), "at": c.get("at")}
            for c in comments[-50:]
        ],
    }

    day = time.strftime("%Y-%m-%d")
    folder = cfg.paths.state / "pet_taste"
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_json(folder / f"{day}.json", data)
    atomic_write_json(cfg.paths.state / "pet_taste.json", data)

    summary = {
        "taken_at": data["taken_at"],
        "top_tags": [t for t, _ in data["feed"]["top_tags"][:10]],
        "favorites": len(favorites),
        "comments": len(data["comments"]),
    }
    with (cfg.paths.state / "pet_taste_history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False) + "\n")

    log.info("口味快照已存：%s 条推荐、%s 条收藏、%s 条评论", len(feed_items), len(favorites), len(comments))
    return data
