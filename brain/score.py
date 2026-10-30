"""给候选视频打分（0~20），低于阈值就不分享。

为什么要有：现在的分享是"掷骰子 + 按细筛顺序取第一条"，骰子只决定发不发，
不决定**值不值得发**。加一条打分线，宁可少发一条，也别推一条自己都不会点开的。
"""

from __future__ import annotations

from bili import config as cfgmod
from bili.llm import Llm, LlmError
from bili.logs import get
from bili.video import Video

log = get("score")

PROMPT = """\
给这条视频打分，0~20 分。只输出 JSON：{{"score": 0, "reason": "一句话"}}

评分维度（各占 5 分）：
- 具体性：有没有一个具体的人在具体地做一件事（不是合集、不是盘点、不是情绪营销）
- 信息密度：看完能带走一点东西，而不是纯热闹
- 新鲜度：不是那种已经被转烂的爆款
- 看完的可能性：你自己会点开并且看完，而不是划走

候选：
标题：{title}
UP：{up_name}
时长：{minutes} 分钟
分区：{zone}
播放 {view} · 赞 {like}
简介：{desc}
"""


def quality(llm: Llm, cfg: cfgmod.Config, video: Video, *, why: str = "") -> tuple[int, str]:
    prompt = PROMPT.format(
        title=video.title,
        up_name=video.up_name,
        minutes=video.minutes,
        zone=video.zone_label,
        view=video.view,
        like=video.like,
        desc=(video.desc or "")[:150],
    )
    messages = [
        {"role": "system", "content": "你是挑片的人，标准严格。只输出 JSON。"},
        {"role": "user", "content": prompt + (f"\n细筛理由：{why}" if why else "")},
    ]
    try:
        data = llm.chat_json(messages, max_tokens=120, temperature=0.3)
    except LlmError as exc:
        log.warning("打分失败，按及格处理：%s", exc)
        return cfg.speak.min_share_score, "（打分失败）"
    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    return max(0, min(20, score)), str(data.get("reason") or "")[:60]
