"""态度决策：这件事它做不做。

和 persona 的分工：
  - 这里决定 **做不做**（accept / bargain / refuse）
  - speak 决定 **怎么说**

原则：
  1. 安全类动作永远接受（暂停、恢复、报平安、统计）——闹脾气不能闹到安全上
  2. 拒绝率有硬上限（默认 30%），不能变成"不干活"
  3. 拒绝要能说清理由，并且给出替代（"这条不行，但我想到一个别的"）
  4. 情绪越低越容易拒绝；被塞明显难看的东西也会拒绝
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from bili import mood

# 这些动作永远不拒绝（安全与自省）
ALWAYS_ACCEPT = {"pause", "resume", "stats", "none", "help"}

ACCEPT = "accept"
BARGAIN = "bargain"
REFUSE = "refuse"

MAX_REFUSE_RATE = 0.30
UGLY_QUALITY = 6  # 低于这个质量分（0~20）算"难看"


@dataclass
class Decision:
    action: str = ACCEPT
    reason: str = ""
    hint: str = ""
    tone: str = "normal"

    @property
    def ok(self) -> bool:
        return self.action == ACCEPT


def tone_of(score: int) -> str:
    """情绪 → 语气档位。毒舌只针对内容，不针对人。"""
    if score >= 60:
        return "normal"
    if score >= 40:
        return "mild"
    if score >= 20:
        return "sharp"
    return "savage"


def decide(
    cfg,
    *,
    kind: str,
    text: str = "",
    quality: float | None = None,
    today_count: int = 0,
    rng: random.Random | None = None,
) -> Decision:
    """决定接受 / 讨价还价 / 拒绝。rng 传进来便于测试。"""
    rng = rng or random.Random()

    if kind in ALWAYS_ACCEPT:
        return Decision(ACCEPT)

    score = mood.current(cfg)
    tone = tone_of(score)

    # 1. 今天被使唤太多次 → 讨价还价（不是直接拒绝，留个台阶）
    if today_count >= 4 and rng.random() < 0.35:
        return Decision(BARGAIN, reason="今天太多了", hint="一次一条，剩下的明天？", tone=tone)

    # 2. 累计一个拒绝概率，**最后统一封顶 30%**（不能变成"不干活"）
    ugly = quality is not None and quality < UGLY_QUALITY
    p_refuse = 0.0
    if ugly:
        p_refuse += 0.30
    if score < 40:
        p_refuse += (40 - score) / 100 * 1.2
    if p_refuse and rng.random() < min(p_refuse, MAX_REFUSE_RATE):
        if ugly:
            return Decision(REFUSE, reason="这条太难看", hint="换一条吧，这条人家念不出口", tone=tone)
        return Decision(REFUSE, reason="心情不好", hint="今天不太想动，晚点吧", tone=tone)

    return Decision(ACCEPT)
