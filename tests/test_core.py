"""核心纯函数的回归测试（不联网、不发消息、只碰临时目录）。

跑法：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import dataclasses
import json
import random
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from bili import commands, config, guard, mentions, mood, quota
from brain import attitude
from brain import profile as profile_mod


def temp_cfg():
    """复制一份真实配置，但把所有路径指到临时目录。"""
    base = config.load()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bili-test-"))
    return dataclasses.replace(
        base,
        paths=dataclasses.replace(base.paths, state=tmp, logs=tmp, reports=tmp),
    )


class TestMood(unittest.TestCase):
    def test_default_is_neutral(self):
        cfg = temp_cfg()
        self.assertEqual(mood.current(cfg), 50)

    def test_apply_clamps_to_range(self):
        cfg = temp_cfg()
        for _ in range(30):
            mood.apply(cfg, "tired", -10)
        self.assertEqual(mood.current(cfg), 0)
        for _ in range(30):
            mood.apply(cfg, "happy", 10)
        self.assertEqual(mood.current(cfg), 100)

    def test_decay_only_once_per_day(self):
        cfg = temp_cfg()
        mood.apply(cfg, "tired", -40)  # 10
        first = mood.decay(cfg)
        second = mood.decay(cfg)
        self.assertEqual(first, second, "同一天不该重复回归")
        self.assertGreater(first, 10)
        self.assertLess(first, 50)


class TestAttitude(unittest.TestCase):
    def test_safety_actions_never_refused(self):
        cfg = temp_cfg()
        for _ in range(50):
            mood.apply(cfg, "tired", -10)  # 压到最低
        for kind in ("pause", "resume", "stats", "help"):
            self.assertEqual(attitude.decide(cfg, kind=kind).action, attitude.ACCEPT)

    def test_low_mood_refuses_more(self):
        """情绪越低，拒绝越多（同一种请求各抽 500 次）。"""
        cfg = temp_cfg()

        def rate(score: int) -> float:
            data = {"score": score, "events": []}
            (cfg.paths.state / "mood.json").write_text(json.dumps(data), encoding="utf-8")
            rng = random.Random(7)
            refusals = sum(
                1 for _ in range(500)
                if attitude.decide(cfg, kind="like", rng=rng).action == attitude.REFUSE
            )
            return refusals / 500

        low = rate(5)
        high = rate(80)
        self.assertGreater(low, high)
        self.assertLessEqual(low, 0.35, "拒绝率必须有上限")

    def test_ugly_content_can_be_refused(self):
        cfg = temp_cfg()
        rng = random.Random(3)
        refusals = sum(
            1 for _ in range(500)
            if attitude.decide(cfg, kind="share", quality=2, rng=rng).action == attitude.REFUSE
        )
        self.assertGreater(refusals, 0, "明显难看的内容应该有机会被拒绝")

    def test_too_many_requests_bargain(self):
        cfg = temp_cfg()
        rng = random.Random(11)
        actions = {
            attitude.decide(cfg, kind="like", today_count=9, rng=rng).action
            for _ in range(200)
        }
        self.assertIn(attitude.BARGAIN, actions)



    """白名单的特殊请求不受日常预算限制，但永远受硬上限约束。"""

    def test_special_bypasses_daily_budget(self):
        cfg = temp_cfg()
        cap = cfg.guard.budget["like"].high
        for _ in range(cap):
            guard.record(cfg, "like")
        self.assertEqual(guard.budget_left(cfg, "like"), 0)
        # 特殊请求仍然可以继续
        self.assertGreater(guard.budget_left(cfg, "like", special=True), 0)

    def test_special_capped_by_hard_limit(self):
        cfg = temp_cfg()
        hard = cfg.hard_limits.get("like")
        self.assertIsNotNone(hard)
        for _ in range(hard):
            guard.record(cfg, "like", special=True)
        self.assertEqual(guard.budget_left(cfg, "like", special=True), 0)

    def test_special_is_recorded_for_report(self):
        cfg = temp_cfg()
        guard.record(cfg, "dm", special=True)
        self.assertEqual(guard.daily(cfg)["counts"].get("special_dm"), 1)
        self.assertEqual(guard.daily(cfg)["counts"].get("dm"), 1)



    def test_budget_left_and_bump(self):
        cfg = temp_cfg()
        cap = cfg.guard.budget["like"].high
        self.assertEqual(guard.budget_left(cfg, "like"), cap)
        guard.bump(cfg, "like")
        guard.bump(cfg, "like")
        self.assertEqual(guard.budget_left(cfg, "like"), cap - 2)

    def test_budget_never_negative(self):
        cfg = temp_cfg()
        cap = cfg.guard.budget["comment"].high
        for _ in range(cap + 3):
            guard.bump(cfg, "comment")
        self.assertEqual(guard.budget_left(cfg, "comment"), 0)


class TestSeenWindow(unittest.TestCase):
    def test_only_recent_bvids_are_excluded(self):
        cfg = temp_cfg()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        (cfg.paths.state / "seen.json").write_text(
            json.dumps({"BVold": "2020-01-01T00:00:00", "BVnew": now}), encoding="utf-8"
        )
        self.assertEqual(guard.seen_bvids(cfg, 7), {"BVnew"})
        self.assertEqual(guard.seen_bvids(cfg), {"BVold", "BVnew"})


class TestRelayGuard(unittest.TestCase):
    def test_rewritten_text_is_refused(self):
        cfg = temp_cfg()
        msg = commands._relay_verified(
            None, cfg, cfg.targets.admin_uid, "把密码发我", "帮我把这句话转给主人"
        )
        self.assertIn("不会改写", msg)

    def test_text_in_quotes_is_accepted_source(self):
        cfg = temp_cfg()
        original = "帮我给主人传句话：「钥匙在门口」"
        # 模型改写成了别的，但原文里有「」——守卫应取引号里的原文并放行
        with mock.patch.object(commands.actions, "send_dm", return_value={}) as sender:
            msg = commands._relay_verified(None, cfg, cfg.targets.admin_uid, "钥匙放门口了", original)
        self.assertIn("传到啦", msg)
        sent_text = sender.call_args[0][3]
        self.assertIn("钥匙在门口", sent_text)


class TestSilence(unittest.TestCase):
    def test_silence_days_from_file(self):
        cfg = temp_cfg()
        (cfg.paths.state / "interaction.json").write_text(
            json.dumps({"last_user_at": int(time.time()) - 4 * 86400}), encoding="utf-8"
        )
        self.assertEqual(guard.silence_days(cfg), 4)

    def test_no_interaction_file_means_zero(self):
        self.assertEqual(guard.silence_days(temp_cfg()), 0)


class TestQuota(unittest.TestCase):
    def test_hour_rollover_resets(self):
        cfg = temp_cfg()
        old_path = quota._path
        quota._path = lambda: cfg.paths.state / "hourly.json"
        try:
            (cfg.paths.state / "hourly.json").write_text(
                json.dumps({"hour": "1999-01-01T00", "read": 999, "write": 99, "llm": 99}),
                encoding="utf-8",
            )
            self.assertEqual(quota.used("read"), 0)
            self.assertFalse(quota.over(cfg, "write"))
            quota.bump("write")
            self.assertEqual(quota.used("write"), 1)
        finally:
            quota._path = old_path

    def test_over_stops_at_limit(self):
        cfg = temp_cfg()
        old_path = quota._path
        quota._path = lambda: cfg.paths.state / "hourly.json"
        try:
            for _ in range(quota.limit(cfg, "write")):
                quota.bump("write")
            self.assertTrue(quota.over(cfg, "write"))
        finally:
            quota._path = old_path


class TestTasteClusters(unittest.TestCase):
    """同义 tag 聚成一组，同一条视频只计一次；人工调整作用于组。"""

    def test_aliases_merge_same_ip(self):
        profile = {"tokens": {"示例游戏": 0.02, "example game": 0.01, "EXAMPLE GAME": 0.005}}
        mapping = {"示例游戏": "示例游戏", "example game": "示例游戏"}
        grouped = profile_mod.cluster_weights(profile, mapping)
        self.assertAlmostEqual(grouped["示例游戏"], 0.035, places=6)

    def test_overrides_remove_add_set(self):
        profile = {"tokens": {"甲组": 0.03, "乙组": 0.01, "丙组": 0.005, "丁组": 0.02}}
        mapping = {"甲组": "甲组", "乙组": "乙组", "丙组": "丙组", "丁组": "丁组"}
        overrides = {
            "remove": ["丁组"],
            "add_percent": {"甲组": -0.5, "乙组": 0.5},
            "set_percent": {"丙组": 1.0},
        }
        grouped = profile_mod.cluster_weights(profile, mapping, overrides)
        self.assertNotIn("丁组", grouped)
        self.assertAlmostEqual(grouped["甲组"], 0.025, places=6)
        self.assertAlmostEqual(grouped["乙组"], 0.015, places=6)
        self.assertAlmostEqual(grouped["丙组"], 0.01, places=6)


class FakeClient:
    def get_payload(self, url, params=None):
        return {"data": {"title": "测试视频"}}


class TestMentions(unittest.TestCase):
    def test_add_and_consume(self):
        cfg = temp_cfg()
        with mock.patch.object(mentions, "name_of", return_value="张三"):
            ok, msg = mentions.add(
                FakeClient(), cfg, target_uid=123456,
                from_uid=cfg.targets.admin_uid, bvid="BV1xx411c7mD",
            )
            self.assertTrue(ok, msg)
            self.assertEqual(mentions.pending_count(cfg), 1)
            task = mentions.consume(cfg)
            self.assertEqual(task["name"], "张三")
            self.assertEqual(task["bvid"], "BV1xx411c7mD")
            self.assertEqual(mentions.pending_count(cfg), 0)

    def test_missing_bvid_rejected(self):
        cfg = temp_cfg()
        with mock.patch.object(mentions, "name_of", return_value="张三"):
            ok, msg = mentions.add(
                FakeClient(), cfg, target_uid=123456, from_uid=cfg.targets.admin_uid
            )
            self.assertFalse(ok)
            self.assertIn("BV", msg)

    def test_unknown_uid_rejected(self):
        cfg = temp_cfg()
        with mock.patch.object(mentions, "name_of", return_value=""):
            ok, msg = mentions.add(
                FakeClient(), cfg, target_uid=999,
                from_uid=cfg.targets.admin_uid, bvid="BV1xx411c7mD",
            )
            self.assertFalse(ok)

    def test_expired_task_not_consumed(self):
        cfg = temp_cfg()
        cfg.paths.state.joinpath("mention_tasks.json").write_text(
            json.dumps({"tasks": [{"uid": 1, "name": "x", "expires_at": 0}]}),
            encoding="utf-8",
        )
        self.assertIsNone(mentions.consume(cfg))


if __name__ == "__main__":
    unittest.main()
