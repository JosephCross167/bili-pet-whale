"""按人格说话：回私信、主动撒娇、分享视频。只产出文本，不发。

发言的节奏、配额、间隔在 `bili/voice.py`；这里只管"说什么"。

一条硬规则：**评论区只能 @ + 客套话**。所以 @ 的正文不走模型，直接从固定句式里挑——
让模型写"这块滑板超酷"，就等于让它发了一条需要人审核的内容，那不是现在的阶段。
"""

from __future__ import annotations

import random

from bili import config as cfgmod
from bili.llm import Llm, LlmError
from bili.logs import get
from bili.video import Video

log = get("speak")

PERSONA_FILE = "persona.md"

# 评论区只用这些。刻意不含任何对视频的描述或评价——"有意义的评论"要人审核，现阶段不发。
MENTION_LINES = (
    "主人快看这个～",
    "来看看这个嘛～",
    "这个有点意思，快来看～",
    "顺手艾特你一下，嘿嘿～",
    "在的话看一眼嘛～",
    "快来看快来看～",
    "人家挑的，来看看？",
    "这个不能只有人家一个人看～",
    "来嘛来嘛～",
    "这个给你留着～",
)


def _persona(cfg: cfgmod.Config) -> str:
    path = cfg.root / PERSONA_FILE
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "你是一只叫小鲸鱼的宠物，说话短、软、偶尔傲娇。"


def _ask(llm: Llm, cfg: cfgmod.Config, prompt: str, *, limit: int = 60) -> str:
    messages = [
        {"role": "system", "content": _persona(cfg) + "\n\n直接说那句话，不要解释，不要加引号。"},
        {"role": "user", "content": prompt},
    ]
    try:
        text = llm.chat(messages, max_tokens=200, temperature=1.1)
    except LlmError as exc:
        log.warning("说话失败：%s", exc)
        return ""
    text = " ".join(text.split()).strip().strip('"').strip("「」")
    return text[:limit]


BLIND_WORDS = (
    "没有眼睛", "没眼睛", "没有耳朵", "没耳朵", "看不到视频", "看不了视频",
    "听不到声音", "看不到也听不到", "没有眼睛也没有耳朵",
)


def _soften(text: str) -> str:
    """兜底：模型偶尔还是会冒出「没有眼睛」这种自我描述，换成人话。"""
    if not text:
        return text
    if any(word in text for word in BLIND_WORDS):
        return "（尾鳍轻轻一摆）唔…人家又没跟着你看，你讲给人家听嘛～"
    return text


def reply(
    llm: Llm,
    cfg: cfgmod.Config,
    who: str,
    text: str,
    history: str = "",
    transcript: str = "",
) -> str:
    """回一句私信。history 是**这个人自己**的上下文，不跨人。

    transcript：对方发来的视频内容摘要（AI 总结/字幕）。有它就能真的基于内容说话。
    """
    ctx = f"你们刚才聊的（只看这几句，别扯别的）：\n{history}\n\n" if history else ""
    if transcript:
        ctx += f"它刚看了对方发来的视频，内容摘要如下（可以引用里面的具体内容）：\n{transcript[:800]}\n\n"
    return _soften(_ask(
        llm,
        cfg,
        f"{ctx}{who}刚给你发私信说：「{text[:120]}」\n"
        "回一句，10~40 字，别问太多，别客套。\n"
        "硬规则：你只能从上面的内容摘要了解视频；有摘要时可以聊里面的内容，没有就别装看过。\n"
        "绝对不要说「看啦」「看了」「挺有意思」这类假装看过画面的话。\n"
        "没有摘要时，被问到内容就用撒娇的方式说自己没看着——比如「人家又没跟着你看」「你讲给我听嘛」。\n"
        "**绝对不要说自己「没有眼睛」「没有耳朵」「是个程序」「看不到也听不到」**——\n"
        "你是宠物，用自己的话讲，不要暴露身体构造，也不要把自己说成残疾机器。",
        limit=60,
    ))


def reply_comment(
    llm: Llm,
    cfg: cfgmod.Config,
    *,
    who: str,
    my_comment: str,
    their_reply: str,
) -> str:
    """在评论区楼中楼回一句。不套模板句，按人格自由说（和私信同一套）。"""
    return _soften(_ask(
        llm,
        cfg,
        f"你之前在视频下面留了条评论：「{my_comment[:80]}」\n"
        f"{who} 在那条评论下面回你：「{their_reply[:100]}」\n"
        "回 ta 一句，10~30 字，像在评论区随口接话，别客套、别写小作文。\n"
        "硬规则：别说「看啦」「看了」「挺好」这类假装看过的话；被问到内容就说没看着。\n"
        "**不要说自己「没有眼睛」「没有耳朵」「是程序」**——用宠物自己的说法，"
        "比如「人家没跟着看」「你讲嘛」。\n"
        "不要复述对方的话，不要提问超过一个。",
        limit=50,
    ))


def reply_stranger(llm: Llm, cfg: cfgmod.Config, *, who: str, my_comment: str, their_reply: str) -> str:
    """回陌生人的一句：只回一次，不邀请继续聊。"""
    return _soften(_ask(
        llm,
        cfg,
        f"你在视频下面留了条评论：「{my_comment[:60]}」\n"
        f"一个陌生网友 {who} 回你：「{their_reply[:80]}」\n"
        "回 ta 一句，**15~25 字**，礼貌但保持距离，像随手敷衍一下。\n"
        "硬规则：\n"
        "- 不要反问、不要提出新话题、不要邀约继续聊；\n"
        "- 不要透露现实信息（住址、学校、年龄、联系方式一律不提）；\n"
        "- 你没跟着看那条视频，别装看过；但**不要说自己「没有眼睛」「没有耳朵」**。",
        limit=45,
    ))


TONE_STYLE = {
    "normal": "平静地说不，不闹脾气。",
    "mild": "语气上有点不情愿，可以带一句「哼」，但别刻薄。",
    "sharp": "语气冲一点：直接吐槽这条内容难看/没意思/浪费时间，可以阴阳怪气，"
             "但不许骂人、不许人身攻击。",
    "savage": "毒舌拉满：尖酸、刻薄、阴阳怪气，但要保留「小鲸鱼」的可爱外壳"
              "（尾鳍、拍水之类的小动作），让对方被怼了还想笑。"
              "只许吐槽内容，不许攻击人，不许骂脏话，不许提现实身份。",
}


def refuse(llm: Llm, cfg: cfgmod.Config, *, who: str, reason: str, hint: str, tone: str = "mild") -> str:
    """拒绝一句。tone 决定毒舌程度（情绪越低越冲）。"""
    style = TONE_STYLE.get(tone, TONE_STYLE["mild"])
    prompt = (
        f"{who} 刚让你做一件事，但你不想干（原因：{reason}）。\n"
        "回一句拒绝的话，12~30 字，一句话。\n"
        f"语气：{style}\n"
        f"可以带一句替代：{hint}。\n"
        "不要讲道理、不要说教、不要解释系统限制、不要提「脚本/程序/任务」。"
    )
    return _ask(llm, cfg, prompt, limit=50)


def bargain(llm: Llm, cfg: cfgmod.Config, *, who: str, reason: str, hint: str, tone: str = "mild") -> str:
    """讨价还价：不是不干，是要谈条件。"""
    style = TONE_STYLE.get(tone, TONE_STYLE["mild"])
    prompt = (
        f"{who} 让你做的事有点多（原因：{reason}）。\n"
        "回一句谈条件的话，12~30 字，一句话。\n"
        f"语气：{style}\n"
        f"条件参考：{hint}。\n"
        "不要生硬、不要像客服，也不要直接答应。"
    )
    return _ask(llm, cfg, prompt, limit=50)


def chat(llm: Llm, cfg: cfgmod.Config, *, hour: int) -> str:
    when = "中午" if hour < 15 else "晚上"
    return _ask(
        llm,
        cfg,
        f"现在是{when}，你刚跑完一轮，主动给主人发一条私信。20~40 字，不要提'运行''脚本''任务'。",
        limit=60,
    )


def share(llm: Llm, cfg: cfgmod.Config, video: Video) -> str:
    return _ask(
        llm,
        cfg,
        f"你想把这条视频推给主人：《{video.title}》（UP {video.up_name}，{video.minutes} 分钟，{video.zone_label}）。\n"
        f"写一句话，20~40 字，顺便在句尾带上编号 {video.bvid}（不要写 http 链接）。",
        limit=80,
    )


def mention(target_name: str, *, rng: random.Random | None = None) -> str:
    """@ + 客套话。不做内容评价，不依赖模型。"""
    line = (rng or random).choice(MENTION_LINES)
    return f"@{target_name} {line}"
