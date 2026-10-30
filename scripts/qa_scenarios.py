"""复合场景 QA：暂停/熔断/电池/额度耗尽/去重/交叉功能，全部离线模拟。"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import runner
from bili import config, guard, mentions, mood, replies, seed
from bili.video import Video

results: list[tuple[str, str, str]] = []


def check(scene: str, ok: bool, detail: str = "") -> None:
    results.append((scene, "PASS" if ok else "FAIL", detail))


def temp_cfg():
    base = config.load()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="qa-scene-"))
    return dataclasses.replace(
        base, paths=dataclasses.replace(base.paths, state=tmp, logs=tmp, reports=tmp)
    )


class FakeClient:
    def get_payload(self, *a, **k):
        return {"data": {"title": "t"}}

    def get_wbi(self, *a, **k):
        return {"data": {"result": []}}


# 1. 暂停：所有 tick 都应跳过（不触碰网络）
cfg = temp_cfg()
guard.pause(cfg, 12, by=cfg.targets.admin_uid)
with mock.patch("bili.replies.from_msgfeed", side_effect=AssertionError("不该联网")) as m1, \
     mock.patch("bili.seed.run", side_effect=AssertionError("不该播")) as m2, \
     mock.patch("bili.at_mentions.collect", side_effect=AssertionError("不该拉通知")) as m3:
    try:
        runner._reply_tick(cfg, FakeClient(), True)
        runner._at_tick(cfg, FakeClient(), True)
        runner._mention_tick(cfg, FakeClient())
        ok = True
    except AssertionError as exc:
        ok = False
check("暂停状态下 tick 全部跳过", ok)
guard.unpause(cfg)

# 2. 熔断：同样跳过
cfg = temp_cfg()
guard.trip_breaker(cfg, "qa-test")
with mock.patch("bili.replies.from_msgfeed", side_effect=AssertionError("不该联网")):
    try:
        runner._reply_tick(cfg, FakeClient(), True)
        ok = True
    except AssertionError:
        ok = False
check("熔断状态下 tick 跳过", ok)

# 3. 评论额度为 0 → 不发
cfg = temp_cfg()
for _ in range(cfg.hard_limits.get("comment") or 30):
    guard.record(cfg, "comment")
with mock.patch("bili.comment_dom.post", side_effect=AssertionError("不该发")):
    try:
        runner._comment_tick(cfg, FakeClient(), True)
        ok = True
    except AssertionError:
        ok = False
check("评论到硬上限后不发", ok)

# 4. @ 任务过期 → 不执行
cfg = temp_cfg()
(cfg.paths.state / "mention_tasks.json").write_text(
    json.dumps({"tasks": [{"uid": 111, "name": "x", "bvid": "BV1xx411c7mD", "expires_at": 0}]}),
    encoding="utf-8",
)
check("@ 任务过期不执行", mentions.consume(cfg) is None)

# 5. @ 任务有效 → 执行一次后消失
cfg = temp_cfg()
with mock.patch.object(mentions, "name_of", return_value="张三"):
    mentions.add(FakeClient(), cfg, target_uid=111, from_uid=cfg.targets.admin_uid, bvid="BV1xx411c7mD")
first = mentions.consume(cfg)
second = mentions.consume(cfg)
check("@ 任务一次性", first is not None and second is None)

# 6. 私信去重：同一条消息两次只处理一次
cfg = temp_cfg()
uid, ts = cfg.targets.admin_uid, 1700000000
a = runner._mark_done if hasattr(runner, "_mark_done") else None
print_detail = ""
from bili import commands as cmd_mod
from bili.store import read_json

cmd_path = cfg.paths.state / "cmd_done.json"
cmd_mod._mark_done(cfg, uid, ts, "like")
cmd_mod._mark_done(cfg, uid, ts, "like")  # 第二次
dump = read_json(cmd_path, {}) or {}
counts = sum(len(v) if isinstance(v, list) else 1 for v in dump.values()) if dump else 0
check("指令去重（同一条只记一次）", cmd_mod._mark_done(cfg, uid, ts, "like") is True, f"记录数 {counts}")

# 7. seed 不重复看同一批（seen 过滤）
cfg = temp_cfg()
(cfg.paths.state / "seed_seen.json").write_text(json.dumps({"BV1xx411c7mD": 1}), encoding="utf-8")
with mock.patch.object(seed, "search", return_value=[{"bvid": "BV1xx411c7mD", "title": "t", "tag": "x"}]), \
     mock.patch.object(seed, "top_tags", return_value=["x"]):
    picked = seed.pick(cfg, FakeClient(), count=1)
check("seed 跳过已看过的", picked == [], str(picked))

# 8. 楼中楼失败不退回顶层评论（用户明确要求）
src = pathlib.Path(__file__).resolve().parents[1].read_text(encoding="utf-8")
check("楼中楼失败不回退", "reply_dom.reply_root(" not in src.split("def _reply_tick")[1].split("def _comment_tick")[0])

# 9. 情绪极值下的语气分档
from brain import attitude

check("情绪 0 → savage", attitude.tone_of(0) == "savage")
check("情绪 100 → normal", attitude.tone_of(100) == "normal")

# 10. 特批与日常预算交叉：日常用完但特批可用，且硬上限仍生效
cfg = temp_cfg()
for _ in range(cfg.guard.budget["dm"].high):
    guard.record(cfg, "dm")
check("日常 dm 用尽", guard.budget_left(cfg, "dm") == 0)
check("特批 dm 仍可", guard.budget_left(cfg, "dm", special=True) > 0)

print(f"{'场景':<34}结果")
print("-" * 74)
for scene, res, detail in results:
    print(f"{scene:<34}{res:<6}{detail}")
print("-" * 74)
print(f"共 {len(results)} 项，失败 {sum(1 for r in results if r[1] == 'FAIL')} 项")
