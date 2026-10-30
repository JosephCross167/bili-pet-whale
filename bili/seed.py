"""养推荐流：让宠物号"真的看"几条**画像里最像你们口味**的视频。

为什么不是按分区：分区是几百条视频混在一起，而 tag 就是「你喜欢的那件东西」。
所以这里直接拿画像的 Top tag 去搜视频，挑高播放的，真播 25 秒。

为什么必须真播：只调接口拉列表不会改变推荐——B 站记的是观看行为。

顺序上依赖画像：画像（tag）先跑完，这里才有有效的关键字。
"""

from __future__ import annotations

import random
import time
from collections import Counter

from . import config as cfgmod
from . import taste
from .client import API, BiliError, Client
from .logs import get
from .store import atomic_write_json, read_json

log = get("seed")

TOP_TAGS = 8          # 每人取几个 tag 作为搜索词
# 这些 tag 太泛，搜出来跟口味无关，直接跳过
GENERIC_TAGS = {
    "二次元", "可爱", "搞笑", "动画", "搞笑视频", "绘画", "剪辑",
    "日常", "生活", "吐槽", "游戏", "音乐", "手书",
}
VERIFY_TRIES = 3      # 每个 tag 最多验几条候选
PER_TAG_RESULTS = 12  # 每个 tag 取多少条候选
PER_DAY = 3
SECONDS = 25


def _seen_path(cfg: cfgmod.Config):
    return cfg.paths.state / "seed_seen.json"


def _load_seen(cfg: cfgmod.Config) -> dict:
    return read_json(_seen_path(cfg), {}) or {}


def top_tags(cfg: cfgmod.Config, uid: int, limit: int = TOP_TAGS) -> list[str]:
    """取画像里权重最高的 tag —— 但要跳过泛 tag，它们搜出来的东西全不相干。"""
    profile = taste.load(cfg, uid)
    tokens = profile.get("tokens") or {}
    picked: list[str] = []
    for tag, _ in sorted(tokens.items(), key=lambda kv: -kv[1]):
        if tag in GENERIC_TAGS:
            continue
        if len(tag.strip()) <= 1:
            continue
        picked.append(tag)
        if len(picked) >= limit:
            break
    return picked


def verify(client: Client, bvid: str, tag: str) -> bool:
    """回头验一次：这条视频自己的 tag 里有没有目标 tag。

    搜索是模糊匹配（"百合"会搜到演员白百合，"祀"会匹配到"资料"），
    所以必须用视频真实 tag 兜底。
    """
    actual = {t.strip().lower() for t in taste.tags_of(client, bvid)}
    if not actual:
        return False
    want = tag.strip().lower()
    return any(want == item or want in item for item in actual)


def search(client: Client, keyword: str, *, limit: int = PER_TAG_RESULTS) -> list[dict]:
    """按 tag 搜视频，按播放量排序。"""
    try:
        payload = client.get_wbi(
            f"{API}/x/web-interface/wbi/search/type",
            {
                "search_type": "video",
                "keyword": keyword,
                "order": "click",
                "page": 1,
                "page_size": 20,
            },
        )
    except (BiliError, Exception) as exc:  # noqa: BLE001
        log.warning("搜索「%s」失败：%s", keyword, type(exc).__name__)
        return []

    out: list[dict] = []
    for item in ((payload.get("data") or {}).get("result") or [])[:limit]:
        bvid = item.get("bvid")
        if not bvid:
            continue
        title = (item.get("title") or "").replace('<em class="keyword">', "").replace("</em>", "")
        out.append({"bvid": bvid, "title": title[:40], "tag": keyword})
    return out


def pick(cfg: cfgmod.Config, client: Client, *, count: int = PER_DAY) -> list[dict]:
    """从两个人的画像里各取几个 tag，搜视频，挑没看过的。"""
    seen = _load_seen(cfg)
    pools: list[dict] = []

    for uid in (cfg.targets.admin_uid,):
        for tag in top_tags(cfg, uid):
            tried = 0
            for item in search(client, tag):
                if item["bvid"] in seen:
                    continue
                if tried >= VERIFY_TRIES:
                    break
                tried += 1
                if verify(client, item["bvid"], tag):
                    pools.append(item)
                    break

    if not pools:
        log.info("养推荐流：没有可用的候选（画像还没生成？）")
        return []

    random.shuffle(pools)
    # 同一条视频可能被多个 tag 命中，按 bvid 去重
    picked: list[dict] = []
    used: set[str] = set()
    for item in pools:
        if item["bvid"] in used:
            continue
        used.add(item["bvid"])
        picked.append(item)
        if len(picked) >= count:
            break
    return picked


def run(cfg: cfgmod.Config, client: Client, *, count: int = PER_DAY, seconds: int = SECONDS) -> list[dict]:
    """真播几条，让推荐流记住。"""
    from bili import watch_video

    videos = pick(cfg, client, count=count)
    if not videos:
        return []

    seen = _load_seen(cfg)
    done: list[dict] = []
    for video in videos:
        result = watch_video.watch(cfg, video["bvid"], seconds=seconds)
        if result.ok:
            seen[video["bvid"]] = int(time.time())
            done.append({**video, "played": round(result.played_seconds, 1)})
            log.info(
                "养推荐流：看了 %s（tag=%s）%.0f 秒",
                video["bvid"], video.get("tag"), result.played_seconds,
            )
        else:
            log.warning("养推荐流：%s 播放失败（%s）", video["bvid"], result.reason)
        time.sleep(random.randint(5, 12))

    if done:
        items = sorted(seen.items(), key=lambda kv: -kv[1])[:500]
        atomic_write_json(_seen_path(cfg), dict(items))
    return done
