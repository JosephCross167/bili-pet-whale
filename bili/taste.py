"""从收藏夹里提取「口味画像」——基于视频 tag。

为什么用 tag 而不是收藏夹名：
  收藏夹名很多时候只是「分类习惯」（默认收藏夹里堆着大量没归类的视频），
  而且会随时间失效（淡圈了还挂着旧名字）。tag 是视频自带的、稳定的语义。

抽样策略：**按收藏夹大小等比例抽样**，总量控制在 budget 条以内。
  大收藏夹贡献更多样本（本来就代表更强的偏好），小收藏夹至少抽 2~3 条。

泛 tag 压制：用推荐流的视频 tag 当背景语料，算一个 IDF：
  weight = 该 tag 在你收藏里的频次 × log(样本总量 / 出现该 tag 的视频数)
  「搞笑」「日常」这种人人有的 tag 会被压下去，小众 tag 会浮上来。

成本：每条视频一次 view/detail（约 1.3 秒），1000 条约 22 分钟，一周跑一次。
"""

from __future__ import annotations

import math
import time
from collections import Counter

from . import config as cfgmod
from .client import API, BiliError, Client
from .logs import get
from .store import atomic_write_json, read_json

log = get("taste")

PAGE_SIZE = 20
MAX_PAGES_PER_FOLDER = 12
MIN_PER_FOLDER = 3
BACKGROUND_SAMPLE = 50
DEFAULT_BUDGET = 1000


def _folders(client: Client, uid: int) -> list[dict]:
    payload = client.get_payload(f"{API}/x/v3/fav/folder/created/list-all", {"up_mid": uid})
    return ((payload.get("data") or {}).get("list")) or []


def _items(client: Client, media_id: int, want: int) -> list[dict]:
    """抓某个收藏夹最新的 want 条。"""
    out: list[dict] = []
    for page in range(1, MAX_PAGES_PER_FOLDER + 1):
        payload = client.get_payload(
            f"{API}/x/v3/fav/resource/list",
            {"media_id": media_id, "ps": PAGE_SIZE, "pn": page, "platform": "web"},
        )
        data = payload.get("data") or {}
        out.extend(data.get("medias") or [])
        if not data.get("has_more") or len(out) >= want:
            break
    return out[:want]


def sample_plan(folders: list[dict], budget: int = DEFAULT_BUDGET) -> list[tuple[dict, int]]:
    """按收藏夹大小等比例分配抽样名额。"""
    usable = [f for f in folders if f.get("id") and int(f.get("media_count") or 0) > 0]
    total = sum(int(f.get("media_count") or 0) for f in usable)
    if not usable or total <= 0:
        return []

    ratio = budget / total
    plan: list[tuple[dict, int]] = []
    for folder in usable:
        count = int(folder.get("media_count") or 0)
        want = max(MIN_PER_FOLDER, round(count * ratio))
        plan.append((folder, min(want, count)))

    allocated = sum(want for _, want in plan)
    if allocated > budget:
        shrink = budget / allocated
        plan = [(f, max(MIN_PER_FOLDER, int(want * shrink))) for f, want in plan]
    return plan


def tags_of(client: Client, bvid: str) -> list[str]:
    """取一条视频的 tag（view 接口不带 tag，必须用 view/detail）。"""
    try:
        payload = client.get_payload(f"{API}/x/web-interface/view/detail", {"bvid": bvid})
    except BiliError as exc:
        log.warning("取 tag 失败 %s：%s", bvid, exc)
        return []
    outer = payload.get("data") or {}
    return [str(t.get("tag_name")) for t in (outer.get("Tags") or []) if t.get("tag_name")]


def background(client: Client, sample: int = BACKGROUND_SAMPLE) -> Counter:
    """拉一批推荐流视频的 tag，用来当「人人都有」的对照。"""
    from bili import feed

    counter: Counter = Counter()
    try:
        pool = feed.fetch_recommend(client, pages=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("背景语料拉取失败：%s", type(exc).__name__)
        return counter
    for video in pool[:sample]:
        for tag in tags_of(client, video.bvid):
            counter[tag] += 1
    log.info("背景语料：%s 条视频、%s 个不同 tag", min(len(pool), sample), len(counter))
    return counter


def build(
    cfg: cfgmod.Config,
    client: Client,
    uid: int,
    *,
    budget: int = DEFAULT_BUDGET,
    bg: Counter | None = None,
) -> dict:
    folders = _folders(client, uid)
    plan = sample_plan(folders, budget)
    if not plan:
        log.warning("uid=%s 没有可读的收藏夹", uid)
        return {"uid": uid, "tokens": {}, "folders": []}

    tf: Counter = Counter()
    sampled = 0
    failed = 0
    used_folders: list[dict] = []

    for index, (folder, want) in enumerate(plan, 1):
        title = (folder.get("title") or "").strip() or "(无标题)"
        media_id = int(folder["id"])
        try:
            items = _items(client, media_id, want)
        except Exception as exc:  # noqa: BLE001
            log.warning("收藏夹 %s 抓取失败：%s", title, type(exc).__name__)
            continue

        used_folders.append(
            {"title": title, "count": int(folder.get("media_count") or 0), "sampled": len(items)}
        )
        for item in items:
            bvid = item.get("bvid")
            if not bvid:
                continue
            tags = tags_of(client, bvid)
            if not tags:
                failed += 1
                continue
            for tag in tags:
                tf[tag] += 1
            sampled += 1
        if index % 5 == 0:
            log.info("进度 %s/%s 个收藏夹，已采样 %s 条", index, len(plan), sampled)

    bg = bg if bg is not None else Counter()
    bg_total = max(1, sum(bg.values()))

    weighted: dict[str, float] = {}
    for tag, count in tf.items():
        df_bg = bg.get(tag, 0)
        idf = math.log((bg_total + 1) / (df_bg + 1)) if bg_total > 1 else 1.0
        weighted[tag] = count * max(idf, 0.05)

    total_weight = sum(weighted.values()) or 1.0
    tokens = {
        tag: round(value / total_weight, 6)
        for tag, value in sorted(weighted.items(), key=lambda kv: -kv[1])[:400]
    }

    profile = {
        "uid": uid,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "budget": budget,
        "sampled_items": sampled,
        "failed_items": failed,
        "folders": used_folders,
        "background_tags": bg_total,
        "tokens": tokens,
    }
    atomic_write_json(cfg.paths.state / f"taste_{uid}.json", profile)
    log.info(
        "画像已生成 uid=%s：采样 %s 条（失败 %s），%s 个 tag",
        uid, sampled, failed, len(tokens),
    )
    return profile


def load(cfg: cfgmod.Config, uid: int) -> dict:
    return read_json(cfg.paths.state / f"taste_{uid}.json", {}) or {}
