"""情绪/拒绝的干跑预览：不发任何东西，只看它会怎么决定、怎么说话。

用法：python scripts/attitude_dry.py
"""

import dataclasses
import json
import pathlib
import random
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bili import config, mood
from bili.llm import Llm, LlmError
from brain import attitude, speak

SCENARIOS = [
    ("正常请求（点赞）", dict(kind="like", today_count=1), 60),
    ("塞一条难看的视频", dict(kind="share", quality=3, today_count=1), 55),
    ("今天第 9 次使唤", dict(kind="like", today_count=9), 45),
    ("情绪很低（15）", dict(kind="share", quality=15, today_count=2), 15),
    ("安全命令（暂停）", dict(kind="pause"), 5),
]


def main() -> int:
    base = config.load()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mood-demo-"))
    cfg = dataclasses.replace(
        base, paths=dataclasses.replace(base.paths, state=tmp, logs=tmp, reports=tmp)
    )

    try:
        llm = Llm(cfg)
    except LlmError as exc:
        print("没有可用模型，只打印决策：", exc)
        llm = None

    for title, kwargs, score in SCENARIOS:
        (tmp / "mood.json").write_text(
            json.dumps({"score": score, "events": []}), encoding="utf-8"
        )
        rng = random.Random(42)
        counts = {attitude.ACCEPT: 0, attitude.BARGAIN: 0, attitude.REFUSE: 0}
        sample = None
        for _ in range(200):
            decision = attitude.decide(cfg, rng=rng, **kwargs)
            counts[decision.action] += 1
            if decision.action != attitude.ACCEPT and sample is None:
                sample = decision

        total = 200
        print(f"\n== {title} ==（情绪 {score}）")
        print(
            "  接受 {:.0%} / 讨价还价 {:.0%} / 拒绝 {:.0%}".format(
                counts[attitude.ACCEPT] / total,
                counts[attitude.BARGAIN] / total,
                counts[attitude.REFUSE] / total,
            )
        )
        if sample is None:
            print("  （从来不拒绝）")
            continue
        if llm is None:
            print(f"  决策：{sample.action}，理由：{sample.reason}")
            continue
        try:
            if sample.action == attitude.REFUSE:
                line = speak.refuse(llm, cfg, who="主人", reason=sample.reason, hint=sample.hint, tone=sample.tone)
            else:
                line = speak.bargain(llm, cfg, who="主人", reason=sample.reason, hint=sample.hint, tone=sample.tone)
            print(f"  [{sample.tone}] 它会说：「{line}」")
        except Exception as exc:  # noqa: BLE001
            print(f"  生成失败：{type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
