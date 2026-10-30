"""把一句私信解析成结构化指令。

刻意不做成"什么都能理解"：动作是白名单，解析不出来就 action=none，
让上层回到普通聊天。宁可少做一件事，也不要猜着做。
"""

from __future__ import annotations

from bili.llm import Llm, LlmError
from bili.logs import get

log = get("commands")

ACTIONS = ("like", "favorite", "share", "stats", "find", "relay", "pause", "resume", "none")

PROMPT = """\
把下面这句话解析成 JSON。只输出 JSON，不要解释。

{{"action": "...", "bvid": "", "keyword": "", "text": "", "target": ""}}

action 只能是这几个：
- like / favorite：要给某个视频点赞/收藏。bvid 填 BV 号；句子里没给就留空（上层会追问）
- share：让发一条视频看看
- stats：问它今天做了什么、用了多少额度
- find：让找视频。keyword 填搜索词
- relay：让把一段话转达给主人。text 是**要转达的原话，一字不改**
- pause：让它安静/别发/停一会儿
- resume：让它恢复/可以说话了
- none：其它一切（闲聊、撒娇、提问）

规则：
- **否定不算请求**：「别点赞」「不用收藏了」→ action=none（不要执行反操作）
- text 里不要改写、不要加标点、不要总结，原样搬运
- 没有把握就选 none

句子：「{sentence}」
"""


def parse(llm: Llm, sentence: str) -> dict:
    messages = [
        {"role": "system", "content": "你只输出 JSON。"},
        {"role": "user", "content": PROMPT.format(sentence=sentence[:300])},
    ]
    try:
        data = llm.chat_json(messages, max_tokens=200, temperature=0)
    except LlmError as exc:
        log.warning("指令解析失败：%s", exc)
        return {"action": "none"}
    action = str(data.get("action") or "none").strip()
    if action not in ACTIONS:
        action = "none"
    return {
        "action": action,
        "bvid": str(data.get("bvid") or "").strip(),
        "keyword": str(data.get("keyword") or "").strip()[:40],
        "text": str(data.get("text") or "").strip(),
        "target": str(data.get("target") or "").strip().lower(),
    }
