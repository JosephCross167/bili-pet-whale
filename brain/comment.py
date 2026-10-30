"""评论草稿（P1 只写进日报，一个字都不发）。

人格目前是占位：中性、短句、不玩梗。附录 A 填好之前，宁可不说话也别输出模板句——
模板句正是最容易被判成机器号的特征。
"""

from __future__ import annotations

from bili.llm import Llm, LlmError
from bili.logs import get
from bili.video import Video

log = get("comment")

PERSONA = (
    "你是一只住在笔记本里的赛博宠物。说话短，10~30 字，最多一句半。"
    "不夸、不总结视频内容、不喊口号、不用 emoji、不用网络烂梗、不反问。"
    "只说你真正注意到的一个细节，或一句很轻的判断。"
)

ALLOWED_ACTIONS = ("点赞", "收藏", "投币", "什么都不做")

_PROMPT = """\
下面这条视频你今晚「看」过了。

标题：{title}
UP：{up_name}
时长：{minutes} 分钟
分区：{tname}
标签：{tags}
简介：{desc}
内容摘要（AI 总结 / 字幕，可能为空）：{transcript}
下面的人在看什么（只作参照，判断大家在聊什么；不要回应、不要复述他们）：
{comments}

输出 JSON：
{{"would_do": ["点赞", "收藏", "投币", "什么都不做"], "draft": "评论正文"}}

would_do：只从 [{allowed}] 里挑 0~2 个，别贪。
draft：一条你会留在下面的评论，10~30 字，一句话。不夸、不总结内容、不提问。
      有内容摘要时，**优先评论摘要里真实出现的某个点**；
      没有摘要时，只能引用标题、标签、简介里出现过的词——
      **绝对不许编造镜头、台词、颜色、场景细节**。宁可只说一句很轻的判断，也不要杜撰。
      宁短勿长。
"""


def draft_for(
    llm: Llm,
    video: Video,
    comments: list[str],
    *,
    transcript: str = "",
) -> tuple[list[str], str]:
    joined = "\n".join(f"- {c[:60]}" for c in comments[:6]) or "（没有热评）"
    tags = "、".join((video.tags or [])[:8]) or "（无）"
    prompt = _PROMPT.format(
        title=video.title,
        up_name=video.up_name,
        minutes=video.minutes,
        tname=video.zone_label,
        tags=tags,
        desc=video.desc[:200] or "（无简介）",
        transcript=(transcript[:700] or "（没拿到摘要，只能看标题和标签）"),
        comments=joined,
        allowed=", ".join(ALLOWED_ACTIONS),
    )
    messages = [
        {"role": "system", "content": PERSONA + " 你只输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    try:
        data = llm.chat_json(messages, max_tokens=400, temperature=0.9)
    except LlmError as exc:
        log.warning("评论草稿失败 %s：%s", video.bvid, exc)
        return [], ""

    actions = [a for a in data.get("would_do") or [] if a in ALLOWED_ACTIONS][:2]
    draft = " ".join(str(data.get("draft") or "").split())[:60]
    return actions, draft
