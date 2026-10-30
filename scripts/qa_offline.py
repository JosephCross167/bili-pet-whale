"""离线 QA：不联网，覆盖阈值/边界/特殊判定。输出结构化结果。"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import random
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bili import aliases, config, guard, mentions, mood, taste
from bili.video import Video
from brain import attitude, intent, profile as profile_mod

results: list[tuple[str, str, str]] = []  # (模块, 用例, 结果)


def check(module: str, case: str, ok: bool, detail: str = "") -> None:
    results.append((module, case, ("PASS" if ok else "FAIL") + (f" | {detail}" if detail else "")))


def temp_cfg():
    base = config.load()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="qa-"))
    return dataclasses.replace(
        base, paths=dataclasses.replace(base.paths, state=tmp, logs=tmp, reports=tmp)
    )


# ---------------- guard：预算 / 硬上限 / 特批 ----------------
cfg = temp_cfg()
check("guard", "初始额度=上限", guard.budget_left(cfg, "like") == cfg.guard.budget["like"].high)
for _ in range(cfg.guard.budget["like"].high):
    guard.record(cfg, "like")
check("guard", "用满后=0", guard.budget_left(cfg, "like") == 0)
check("guard", "用满后特批仍可用", guard.budget_left(cfg, "like", special=True) > 0)
hard = cfg.hard_limits.get("like")
for _ in range(hard):
    guard.record(cfg, "like", special=True)
check("guard", "特批也被硬上限卡住", guard.budget_left(cfg, "like", special=True) == 0)
for _ in range(5):
    guard.record(cfg, "comment")
check("guard", "计数不会为负", guard.budget_left(cfg, "comment") >= 0)

# ---------------- mood：极值 / 衰减 ----------------
cfg = temp_cfg()
check("mood", "初始 50", mood.current(cfg) == 50)
for _ in range(20):
    mood.apply(cfg, "x", -10)
check("mood", "下限 0", mood.current(cfg) == 0)
for _ in range(20):
    mood.apply(cfg, "x", 10)
check("mood", "上限 100", mood.current(cfg) == 100)
mood.apply(cfg, "x", -60)
first = mood.decay(cfg)
second = mood.decay(cfg)
check("mood", "同一天只衰减一次", first == second, f"{first}/{second}")
check("mood", "衰减朝 50", 40 <= first <= 60, str(first))

# ---------------- attitude：阈值边界 / 拒绝率上限 ----------------
cfg = temp_cfg()
check("attitude", "安全动作永不拒绝", all(
    attitude.decide(cfg, kind=k, rng=random.Random(1)).action == attitude.ACCEPT
    for k in ("pause", "resume", "stats", "help")
))
rates = {}
for score in (0, 19, 20, 39, 40, 59, 60, 100):
    (cfg.paths.state / "mood.json").write_text(json.dumps({"score": score, "events": []}), encoding="utf-8")
    rng = random.Random(7)
    n = 5000
    refusals = sum(1 for _ in range(n) if attitude.decide(cfg, kind="like", rng=rng).action == attitude.REFUSE)
    rates[score] = refusals / n
check("attitude", "拒绝率封顶 30%", max(rates.values()) <= 0.32, json.dumps(rates))
check("attitude", "情绪高不拒绝", rates[60] == 0 and rates[100] == 0)
check("attitude", "情绪越低越容易拒", rates[0] >= rates[39])
check("attitude", "语气分档边界", attitude.tone_of(60) == "normal" and attitude.tone_of(59) == "mild"
      and attitude.tone_of(39) == "sharp" and attitude.tone_of(19) == "savage")

# ---------------- aliases / cluster：大小写 / 合并 ----------------
cfg = temp_cfg()
mapping = aliases.load(cfg)
merged = aliases.group({"示例游戏": 0.01, "EXAMPLE GAME": 0.005, "eg": 0.004}, mapping)
check("aliases", "同义合并", abs(merged.get("示例游戏", 0) - 0.019) < 1e-9, str(merged.get("示例游戏")))
check("aliases", "大小写不敏感", "示例游戏" in mapping and mapping["示例游戏"] == "示例游戏")

# ---------------- profile：分类边界 ----------------
cfg = temp_cfg()
blank = {"tokens": {}}
v = Video(bvid="BV1xx411c7mD", title="t", tags=["明日方舟"])
check("profile", "空画像得 0 分", profile_mod.score(blank, v, cfg=cfg) == 0)
check("profile", "空画像分类不崩", profile_mod.classify(cfg, v)["kind"] in ("ours", "self", "neutral"))
cfg_dir = cfg.paths.state
(cfg_dir / "self_taste.json").write_text(json.dumps({"tokens": {"deepseek": 50}}), encoding="utf-8")
v2 = Video(bvid="BV1xx411c7mD", title="t", tags=["deepseek"])
k = profile_mod.classify(cfg, v2)
check("profile", "自画像命中→self", k["kind"] == "self", str(k))
v3 = Video(bvid="BV1xx411c7mD", title="t", tags=["完全不相关的东西"])
check("profile", "全不命中→neutral", profile_mod.classify(cfg, v3)["kind"] == "neutral")

# ---------------- mentions：边界 ----------------
cfg = temp_cfg()
with mock.patch.object(mentions, "name_of", return_value="张三"):
    ok, _ = mentions.add(None.__class__ and type("C", (), {"get_payload": lambda s, u, p: {"data": {"title": "t"}}})(),
                         cfg, target_uid=123456, from_uid=cfg.targets.admin_uid)
check("mentions", "缺 BV 拒绝", not ok)
class FakeClient:
    def get_payload(self, url, params=None):
        return {"data": {"title": "测试视频"}}
with mock.patch.object(mentions, "name_of", return_value="张三"):
    ok1, _ = mentions.add(FakeClient(), cfg, target_uid=111111, from_uid=cfg.targets.admin_uid, bvid="BV1xx411c7mD")
    ok2, _ = mentions.add(FakeClient(), cfg, target_uid=111111, from_uid=cfg.targets.admin_uid, bvid="BV1xx411c7mD")
    ok3, _ = mentions.add(FakeClient(), cfg, target_uid=222222, from_uid=cfg.targets.admin_uid, bvid="BV1xx411c7mD")
    ok4, _ = mentions.add(FakeClient(), cfg, target_uid=333333, from_uid=cfg.targets.admin_uid, bvid="BV1xx411c7mD")
check("mentions", "重复 UID 拒绝", ok1 and not ok2)
check("mentions", "每日 2 条上限", ok1 and ok3 and not ok4)
(cfg.paths.state / "mention_tasks.json").write_text(
    json.dumps({"tasks": [{"uid": 1, "name": "x", "bvid": "BV1xx411c7mD", "expires_at": 0}]}), encoding="utf-8")
check("mentions", "过期任务不消费", mentions.consume(cfg) is None)

# ---------------- at_mentions：解析边界 ----------------
cases = [
    ("在这个视频BV1xx411c7mD@一下一个人，uid是123456", (123456, "BV1xx411c7mD")),
    ("uid=123456 BV1xx411c7mD", (123456, "BV1xx411c7mD")),
    ("帮我在这个视频下@一下 123456", (123456, "")),
    ("这个视频BV真好看，我朋友UID也这么觉得", (None, "")),
    ("uid=123 太短", (None, "")),
]
for text, expect in cases:
    got = __import__("bili.at_mentions", fromlist=["parse"]).parse(text)
    check("at_mentions", f"解析「{text[:14]}」", got == expect, f"{got} vs {expect}")

# ---------------- taste：抽样规划 ----------------
plan = taste.sample_plan([{"id": 1, "media_count": 3, "title": "小"}], 1000)
check("taste", "极小收藏夹至少抽 3", plan and plan[0][1] == 3)
plan2 = taste.sample_plan([{"id": 1, "media_count": 5000, "title": "大"}], 1000)
check("taste", "大收藏夹不超过总量", plan2 and plan2[0][1] == 1000, str(plan2))
check("taste", "空收藏夹返回空", taste.sample_plan([], 100) == [])

# ---------------- 输出 ----------------
print(f"{'模块':<12}{'用例':<28}结果")
print("-" * 78)
for module, case, res in results:
    print(f"{module:<12}{case:<28}{res}")
fails = [r for r in results if r[2].startswith("FAIL")]
print("-" * 78)
print(f"共 {len(results)} 项，失败 {len(fails)} 项")
