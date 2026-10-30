"""粗筛（规则，免费）→ 细筛（模型，一次调用）。

规则层的每一条都能用一句话解释，出问题时一眼能看出是它把好东西筛掉了：
挑视频不是做数据，但也不该像随机数。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bili import config as cfgmod
from bili.llm import Llm, LlmError
from bili.logs import get
from bili.video import Video

log = get("select")

# 标题里出现这些词的多半是带货/招生/录播，不是「想看的视频」
TITLE_BLOCKLIST = ("抽奖", "带货", "广告", "推广", "招生", "课程", "付费", "直播回放")

MIN_SECONDS = 60
MAX_SECONDS = 40 * 60
MIN_VIEW = 1000


@dataclass
class Pick:
    video: Video
    why: str = ""
    would_do: list[str] = field(default_factory=list)
    draft: str = ""


def coarse(
    videos: list[Video],
    cfg: cfgmod.Config,
    *,
    seen: set[str],
    max_per_up: int = 1,
) -> list[Video]:
    kept: list[Video] = []
    per_up: dict[int, int] = {}
    for video in videos:
        if video.bvid in seen:
            continue
        if not (MIN_SECONDS <= video.duration <= MAX_SECONDS):
            continue
        if video.view < MIN_VIEW:
            continue
        if any(word in video.title for word in TITLE_BLOCKLIST):
            continue
        if per_up.get(video.up_mid, 0) >= max_per_up:
            continue
        per_up[video.up_mid] = per_up.get(video.up_mid, 0) + 1
        kept.append(video)

    kept.sort(key=lambda v: (v.like_rate, v.view), reverse=True)
    picked = kept[: cfg.p1.coarse_keep]
    log.info("粗筛：%s → %s 条（上限 %s）", len(videos), len(picked), cfg.p1.coarse_keep)
    return picked


_PROMPT = """\
你是筛选器，不是评论员。从下面的候选视频里挑出 {n} 条「今晚真的会点开看完」的。

标准：不要爆款、不要合集、不要教程、不要情绪营销、不要政治。
优先「有个具体的人在做一件具体的事」，宁可冷门也不要热闹但空的。

候选（每行一条）：
{rows}

只输出 JSON：
{{"picks": [{{"bvid": "...", "why": "一句话，说清它哪里勾住你（20 字内）"}}]}}
挑不满 {n} 条就少挑，不要凑数。
"""


def _rows(videos: list[Video]) -> str:
    lines = []
    for index, video in enumerate(videos, 1):
        reason = f" | 推荐语:{video.rcmd_reason}" if video.rcmd_reason else ""
        lines.append(
            f"{index}. {video.bvid} | {video.title} | UP {video.up_name} | "
            f"{video.minutes}分钟 | 播放{video.view} 赞{video.like} | {video.tname}{reason}"
        )
    return "\n".join(lines)


def fine(llm: Llm, candidates: list[Video], cfg: cfgmod.Config) -> list[Pick]:
    if not candidates:
        return []
    target = min(cfg.p1.fine_keep, len(candidates))
    messages = [
        {"role": "system", "content": "你只输出 JSON，不解释。"},
        {"role": "user", "content": _PROMPT.format(n=target, rows=_rows(candidates))},
    ]
    try:
        data = llm.chat_json(messages, max_tokens=700, temperature=0.7)
    except LlmError as exc:
        log.warning("细筛失败，退回规则排序：%s", exc)
        return [Pick(video=v, why="（模型不可用，按规则排序）") for v in candidates[:target]]

    by_bvid = {video.bvid: video for video in candidates}
    picks: list[Pick] = []
    for item in data.get("picks") or []:
        if not isinstance(item, dict):
            continue
        video = by_bvid.get(str(item.get("bvid", "")))
        if video is None:
            continue
        picks.append(Pick(video=video, why=(item.get("why") or "").strip()[:60]))
    if not picks:
        log.warning("细筛返回空，退回规则排序")
        return [Pick(video=v, why="（模型没给出选择，按规则排序）") for v in candidates[:target]]
    log.info("细筛：%s → %s 条", len(candidates), len(picks))
    return picks[:target]
