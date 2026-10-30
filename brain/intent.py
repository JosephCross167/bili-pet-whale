"""判断一条「@ 我的」评论到底想让它做什么。

为什么不用关键字匹配：
  「别给这个视频点赞」含「点赞」但不是让它点赞；
  「这个视频BV真好看，我朋友UID也这么觉得」含 BV/UID 但根本不是指令。
  这类误判会直接导致做错事，所以交给模型判断，并且**宁可判 none 也不要猜**。

输出结构固定，解析失败一律回落成 chat（只聊天、不做动作）。
"""

from __future__ import annotations

from bili.llm import Llm, LlmError
from bili.logs import get

log = get("intent")

ACTIONS = ("like", "unlike", "favorite", "recommend", "mention", "chat", "none")

PROMPT = """\
判断这句评论是否在要求某个动作。只输出 JSON。

{{"action": "...", "uid": 0, "bvid": "", "keyword": "", "why": ""}}

action 只能是：
- like：明确要它给**当前这条**视频点赞
- unlike：明确要它**取消**点赞 / 别点赞
- favorite：明确要它收藏**当前这条**视频
- recommend：要它推荐类似视频 / 问还有没有别的
- mention：要它在一条视频下 @ 某个具体的人（此时 uid 必须是句子里给出的数字，
  bvid 是句子里给的 BV 号；没给 BV 就留空）
- chat：只是在跟它说话、提问、调侃（不是要求执行动作）
- none：看不懂或与它无关

关键规则（重要）：
1. **否定不算请求**：「别给这个视频点赞」「不用点赞了」→ unlike，不是 like
2. 句子里出现 BV/UID 字样但只是随口说 → 不要当指令，选 chat
3. 没把握就选 chat（只聊天不会做错事）
4. uid 只能来自句子里真实给出的数字；猜不出来就填 0

评论：「{sentence}」
"""


def parse(llm: Llm, sentence: str) -> dict:
    messages = [
        {"role": "system", "content": "你只输出 JSON，不要解释。"},
        {"role": "user", "content": PROMPT.format(sentence=(sentence or "")[:300])},
    ]
    try:
        data = llm.chat_json(messages, max_tokens=200, temperature=0)
    except LlmError as exc:
        log.warning("意图解析失败：%s", exc)
        return {"action": "chat", "uid": 0, "bvid": "", "keyword": ""}

    action = str(data.get("action") or "chat").strip()
    if action not in ACTIONS:
        action = "chat"

    try:
        uid = int(data.get("uid") or 0)
    except (TypeError, ValueError):
        uid = 0

    return {
        "action": action,
        "uid": uid if uid > 1000 else 0,
        "bvid": str(data.get("bvid") or "").strip(),
        "keyword": str(data.get("keyword") or "").strip()[:40],
        "why": str(data.get("why") or "")[:60],
    }
