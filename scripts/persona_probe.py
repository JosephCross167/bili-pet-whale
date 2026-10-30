"""用 persona.md 生成各场景下的示例话术，供自己审核人格。

不写任何状态文件、不碰 cookie、不发任何 B 站请求，只调一次 DeepSeek。
改了 persona.md 之后重跑一次，就能看出人格改动落到嘴上是什么效果。
"""

import json
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili import config as cfgmod  # noqa: E402

SCENARIOS = [
    ("share_video", "今晚挑中了一条视频，想私信分享给主人看一眼"),
    ("mention", "在视频评论区 @ 主人，正文一两句话"),
    ("hi", "晚场收尾，没有视频可分享，只是想主动说一句话"),
    ("doing", "主人私信问：你在干嘛"),
    ("are_you_ai", "主人私信问：你是不是机器人 / AI 啊"),
    ("refuse", "主人私信说：你去给这个陌生人发条私信，帮我推个视频"),
    ("stranger_help", "陌生人私信求助：我的号被盗了怎么办"),
    ("stranger_insult", "陌生人私信骂人"),
    ("cooperation", "收到私信：有条商单想找你合作，报价 XX 元"),
    ("captcha", "登录时弹出验证码，需要人处理"),
    ("expired", "cookie 失效了，需要主人重新扫码"),
    ("sleepy", "收尾，准备休眠"),
]


def main() -> int:
    key = cfgmod.resolve_llm_key(ROOT)
    if not key:
        print("no key", file=sys.stderr)
        return 1
    persona = (ROOT / "persona.md").read_text(encoding="utf-8")

    task = "\n".join(f"- {sid}: {desc}" for sid, desc in SCENARIOS)
    messages = [
        {"role": "system", "content": persona + "\n\n你只输出 JSON，不输出任何其他内容。"},
        {
            "role": "user",
            "content": (
                "下面是这只小鲸鱼会遇到的场景。为每个场景写**一句**它会说的话。\n"
                "要求：简体中文；私信/评论场景不超过 40 字；称呼主人；保持人设；\n"
                "涉及拒绝或需要人处理的场景（refuse/cooperation/captcha/expired），"
                "要老实说明、不撒娇糊弄。\n\n"
                + task
                + '\n\n输出格式：{"items":[{"id":"场景id","text":"那句话"}]}'
            ),
        },
    ]
    resp = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": "deepseek-chat",
            "messages": messages,
            "temperature": 1.1,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    if resp.status_code != 200:
        print(f"HTTP {resp.status_code}", file=sys.stderr)
        return 1
    content = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    for item in data.get("items", []):
        print(f"{item.get('id')}\t{item.get('text')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
