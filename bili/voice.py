"""发言编排：这一波发几条、发给谁、之间隔多久。

默认节奏：每个时间段的**头尾各一波**，一波里私信和 @ 都可以有多条，
但两条之间必须隔一会儿。总量受每日预算和熔断约束，超了就安静地少说两句。

这里只做「发什么」和「发几条」，具体文字在 `brain/speak.py`，写操作在 `bili/actions.py`。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from bili import actions, coin_dom, comment_dom, commands, config as cfgmod, context, guard, mentions, replies, taste
from bili.client import BiliError, Client, RiskControl
from bili.llm import Llm
from bili.logs import get
from bili.store import atomic_write_json, read_json
from bili.video import Video, top_comments, transcript
from brain import comment as comment_brain
from brain import profile as profile_mod
from brain import score as score_mod, speak

log = get("voice")

import re

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# @ 谁：固定找管理员
MENTION_TARGET = "admin"


@dataclass
class Said:
    kind: str  # dm / mention
    target: str
    text: str
    ok: bool
    detail: str = ""


def _names(client: Client, cfg: cfgmod.Config) -> dict[int, str]:
    """昵称每天查一次并缓存。

    常驻模式下每次轮询都查一遍等于给自己加 2 次固定请求，没必要——
    昵称改了第二天也会刷新，而 @ 写错名字顶多是一条不生效的提及。
    """
    path = cfg.paths.state / "names.json"
    today = guard.now_in(cfg).date().isoformat()
    data = read_json(path, {}) or {}
    if data.get("date") == today and data.get("names"):
        return {int(k): str(v) for k, v in data["names"].items()}

    names: dict[int, str] = {}
    for uid in cfg.targets.uids:
        try:
            payload = client.get_payload(
                "https://api.bilibili.com/x/web-interface/card", {"mid": uid}
            )
        except BiliError as exc:
            log.warning("查昵称失败 uid=%s：%s", uid, exc)
            continue
        card = (payload.get("data") or {}).get("card") or {}
        if card.get("name"):
            names[uid] = card["name"]

    if names:
        atomic_write_json(path, {"date": today, "names": {str(k): v for k, v in names.items()}})
    return names


def _gap(cfg: cfgmod.Config) -> None:
    if not cfg.speak.enabled:
        return
    seconds = random.randint(cfg.speak.gap_low, cfg.speak.gap_high)
    log.info("等 %s 秒再发下一条", seconds)
    time.sleep(seconds)


# 全局「上一次发言」时间：头和尾是两波，但间隔要求是跨波的。
# 之前只在批内 sleep，导致尾巴的第一条紧跟着头部的最后一条发出去（实测只隔 8 秒）。
_last_sent: float | None = None


def _before_send(cfg: cfgmod.Config) -> None:
    """两条发送之间至少隔 gap_seconds —— 但只补「还剩多少」。

    之前是无条件睡满 60~150 秒，导致隔了 8 小时再回一条也要先干等两分钟。
    """
    global _last_sent
    if _last_sent is not None and cfg.speak.enabled:
        gap = random.uniform(cfg.speak.gap_low, cfg.speak.gap_high)
        remain = gap - (time.monotonic() - _last_sent)
        if remain > 0:
            log.info("等 %.0f 秒再发下一条（距上次发送不足间隔）", remain)
            time.sleep(remain)
    _last_sent = time.monotonic()


def _silence_factor(cfg: cfgmod.Config) -> float:
    """连续没人理 → 越说越少；到 silence_zero_days 干脆不分享。"""
    days = guard.silence_days(cfg)
    if cfg.speak.silence_zero_days > 0 and days >= cfg.speak.silence_zero_days:
        return 0.0
    if cfg.speak.silence_half_days > 0 and days >= cfg.speak.silence_half_days:
        return 0.5
    return 1.0


def _taste_score(cfg: cfgmod.Config, uid: int, video) -> float | None:
    """这条视频多像这个人的口味。关闭或画像缺失时返回 None（表示不参与判断）。"""
    if not cfg.taste.enabled or video is None:
        return None
    profile = taste.load(cfg, uid)
    if not profile.get("tokens"):
        return None
    return profile_mod.score(profile, video, cfg=cfg, uid=uid)


def _share_chance(cfg: cfgmod.Config) -> float:
    return cfg.speak.share_chance * _silence_factor(cfg)


def _left(cfg: cfgmod.Config, kind: str) -> int:
    return guard.budget_left(cfg, kind)


def _remember(cfg: cfgmod.Config, uid: int, user_text: str, pet_text: str) -> None:
    """把一问一答写进**这个人自己**的上下文；两个人互不可见。"""
    if user_text:
        context.append(cfg, uid, "user", user_text)
    if pet_text:
        context.append(cfg, uid, "pet", pet_text)


def head(client: Client, cfg: cfgmod.Config, llm: Llm, digest, *, allow_chat: bool = True) -> list[Said]:
    """开头一波：先回私信；没有可回的，就主动说一句（如果额度还在）。

    会话轮询时用 allow_chat=False —— 否则每轮没消息都会主动"冒个泡"，变成刷屏。
    """
    said: list[Said] = []
    if not cfg.speak.enabled:
        return said

    if digest.from_whitelist:
        guard.touch_interaction(cfg)  # 有人说话 = 不沉默

    names = _names(client, cfg)
    budget = cfg.speak.per_batch

    for item in digest.from_whitelist[: cfg.speak.reply_per_run]:
        if budget <= 0:
            break
        who = names.get(item.talker_id, "主人")

        # 带 BV 号的消息优先当「让我看这条视频」处理，不走指令解析
        has_bv = bool(BV_RE.search(item.text or ""))
        if has_bv:
            text = None
        else:
            # 先看是不是指令（点赞/收藏/传话/统计/找视频）。是就说执行结果，不是就正常聊天。
            text = commands.handle(client, cfg, llm, item.talker_id, item.text, item.ts)
        # 指令 = 你们点名要的事，不计入日常私信额度（仍受硬上限约束）
        is_command = text is not None
        if not is_command and _left(cfg, "dm") <= 0:
            break
        if text is None:
            history = context.render(cfg, item.talker_id)
            # 私信里出现 BV 号 → 先取内容摘要，这样它就能真的"看过"再回答
            summary = ""
            match = BV_RE.search(item.text)
            if match:
                try:
                    summary = transcript(client, match.group(0))
                    if summary:
                        log.info("私信里读了 %s：摘要 %s 字", match.group(0), len(summary))
                except Exception as exc:  # noqa: BLE001
                    log.warning("读视频摘要失败：%s", type(exc).__name__)
            text = speak.reply(llm, cfg, who, item.text, history, transcript=summary)
        if not text:
            continue
        # 指令和聊天都要进上下文——之前只记指令，所以它记不住普通聊天
        _remember(cfg, item.talker_id, item.text, text)
        _before_send(cfg)
        try:
            actions.send_dm(client, cfg, item.talker_id, text, confirm=True)
        except RiskControl:
            guard.trip_breaker(cfg, "回私信触发风控")
            said.append(Said("dm", who, text, False, "风控"))
            return said
        except (BiliError, actions.ActionRefused) as exc:
            said.append(Said("dm", who, text, False, type(exc).__name__))
            continue
        guard.record(cfg, "dm", special=is_command)
        if not is_command:
            budget -= 1
        said.append(Said("dm", who, text, True))

    if allow_chat and not said and budget > 0 and _left(cfg, "dm") > 0:
        # 主动打招呼：一天最多早晚各一条，其余轮次一律不开口。
        # 限额是上限，不是"今天必须说满几句"——但每天重启几次也不该重复问好。
        if guard.is_paused(cfg):
            return said
        hour = guard.now_in(cfg).hour
        band = "am" if 7 <= hour < 11 else ("pm" if 19 <= hour < 23 else "")
        if not band:
            return said
        if guard.daily(cfg)["counts"].get(f"greet_{band}", 0) > 0:
            return said
        if random.random() > cfg.speak.chat_chance:
            return said
        text = speak.chat(llm, cfg, hour=hour)
        if text:
            _before_send(cfg)
            try:
                actions.send_dm(client, cfg, cfg.targets.admin_uid, text, confirm=True)
                guard.bump(cfg, "dm")
                guard.bump(cfg, f"greet_{band}")
                _remember(cfg, cfg.targets.admin_uid, "", text)
                said.append(Said("dm", "主人", text, True))
            except (BiliError, RiskControl, actions.ActionRefused) as exc:
                said.append(Said("dm", "主人", text, False, type(exc).__name__))
    return said


def tail(client: Client, cfg: cfgmod.Config, llm: Llm, picks) -> list[Said]:
    """结尾一波：分享一条视频（私信）+ @ 一条视频。两条之间隔开。"""
    said: list[Said] = []
    if not cfg.speak.enabled or not picks:
        return said

    videos: list[Video] = [p.video for p in picks]
    if guard.is_paused(cfg):
        log.info("主人在暂停期，跳过分享")
        return said
    names = _names(client, cfg)
    admin_name = names.get(cfg.targets.admin_uid)

    share_video = videos[0]
    score, why = score_mod.quality(llm, cfg, share_video)
    if score < cfg.speak.min_share_score:
        log.info("分享打分 %s/%s 不及格，这轮不分享：%s", score, 20, why)
        return said
    admin_match = _taste_score(cfg, cfg.targets.admin_uid, share_video)
    if admin_match is not None and admin_match < cfg.taste.min_score_admin:
        log.info("给主人的那条匹配度 %.2f 不够（阈值 %.2f），不分享", admin_match, cfg.taste.min_score_admin)
        return said
    if _left(cfg, "dm") > 0 and random.random() <= _share_chance(cfg):
        text = speak.share(llm, cfg, share_video)
        if text:
            _before_send(cfg)
            try:
                actions.send_dm(client, cfg, cfg.targets.admin_uid, text, confirm=True)
                guard.bump(cfg, "dm")
                guard.bump(cfg, "share_admin")
                detail = f"匹配 {admin_match:.1f}" if admin_match is not None else ""
                said.append(Said("dm", "主人", text, True, detail))
            except RiskControl:
                guard.trip_breaker(cfg, "分享视频触发风控")
                return said
            except (BiliError, actions.ActionRefused) as exc:
                said.append(Said("dm", "主人", text, False, type(exc).__name__))

    # 评论：走浏览器 DOM（API 那条路已验证永远影子隐藏）。骰子决定发不发。
    said.extend(_maybe_comment(client, cfg, llm, picks, names))

    return said


def _maybe_comment(client: Client, cfg: cfgmod.Config, llm: Llm, picks, names) -> list[Said]:
    """发一条评论。规则：骰子决定发不发；每天 ≤10；@ 每人 ≤2；带 @ 的不同步动态。"""
    said: list[Said] = []
    if guard.is_paused(cfg) or _left(cfg, "comment") <= 0:
        return said
    if random.random() > cfg.speak.comment_chance:
        log.info("评论骰子没中，这轮不评论")
        return said

    video = next((p.video for p in picks if p.video.aid), None)
    if not video or not video.aid:
        return said

    # 先看这条视频像谁的口味：
    #   像你们的 → 只 @ 那个人（相当于"看到你会喜欢的，转给你"），不做别的互动
    #   像它自己的 → 正常评论（不带 @）
    try:
        cls = profile_mod.classify(cfg, video)
    except Exception:  # noqa: BLE001 - 分类失败就按老逻辑走
        cls = {"kind": "self", "admin_score": 0.0, "match": 0.0}

    if cls["kind"] == "neutral" and cfg.taste.enabled:
        log.info("这条既不像你们也不像它自己的口味，不评论：%s", video.title[:20])
        return said

    mention_uid = None
    mention_name = None

    if cls["kind"] == "ours" and cfg.taste.enabled:
        target_uid = cfg.targets.admin_uid
        name = names.get(target_uid)
        if name and guard.at_today(cfg, target_uid) < cfg.speak.at_per_person:
            mention_uid, mention_name = target_uid, name
    else:
        # 它自己的口味：偶尔 @ 一下你们（保持原有的概率），其余正常评论
        candidates = [
            uid
            for uid in (cfg.targets.admin_uid,)
            if names.get(uid) and guard.at_today(cfg, uid) < cfg.speak.at_per_person
        ]
        if candidates and random.random() < 0.2:
            mention_uid = random.choice(candidates)
            mention_name = names[mention_uid]

    if mention_name:
        # @ 的评论：正文只放客套话，不带评价（这是之前定死的规则）
        text = speak.mention(mention_name).split(" ", 1)[-1]
        sync = False
        mention_arg = mention_name
    else:
        comments = top_comments(client, video.aid, count=8)
        # 尽量拿到内容摘要（AI 总结/字幕），拿不到就退回标题+标签
        try:
            summary = transcript(client, video.bvid)
        except Exception:  # noqa: BLE001
            summary = ""
        try:
            _, draft = comment_brain.draft_for(llm, video, comments, transcript=summary)
        except Exception as exc:  # noqa: BLE001 - 草稿失败就不发，别硬凑
            log.warning("评论草稿失败：%s", type(exc).__name__)
            draft = ""
        text = (draft or "").strip()
        sync = bool(cfg.speak.sync_dynamic and _left(cfg, "dynamic") > 0)
        mention_arg = None

    if not text:
        return said

    result = comment_dom.post(
        cfg,
        bvid=video.bvid,
        message=text,
        mention=mention_arg,
        sync_dynamic=sync,
        watch_seconds=cfg.watch.seconds if cfg.watch.enabled else 0,
    )
    if not result.ok:
        said.append(Said("comment", video.bvid, text, False, result.reason))
        return said

    guard.bump(cfg, "comment")
    if result.rpid:
        try:
            replies.record_comment(
                cfg, rpid=int(result.rpid), aid=int(video.aid), bvid=video.bvid,
                text=text, mentioned=mention_name or "",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("记录评论失败：%s", type(exc).__name__)
    if mention_uid:
        guard.bump_at(cfg, mention_uid)
    if sync:
        guard.bump(cfg, "dynamic")

    detail = []
    if mention_name:
        detail.append(f"@{mention_name}")
    if sync:
        detail.append("已同步动态")
    if result.rpid:
        detail.append(f"rpid={result.rpid}")
    said.append(Said("comment", video.bvid, text, True, "，".join(detail)))
    return said


def _engage_gap(cfg: cfgmod.Config) -> None:
    low, high = cfg.actions.engage_gap_low, cfg.actions.engage_gap_high
    if high <= 0:
        return
    seconds = random.randint(low, high)
    time.sleep(seconds)


def engage(client: Client, cfg: cfgmod.Config, picks, *, likes: int = 2) -> list[Said]:
    """点赞 / 投币 / 收藏。别高频连点，每条之间隔 20~45 秒。"""
    said: list[Said] = []
    if not cfg.speak.enabled or not picks:
        return said

    videos = [p.video for p in picks if p.video.aid]
    if not videos:
        return said
    if guard.is_paused(cfg):
        log.info("主人在暂停期，跳过点赞/收藏")
        return said

    # 第一条留给分享，不重复点；其余按细筛顺序点几个赞。
    # 每条都单独掷骰子：今天只点一个、或者一个都不点，都是正常的。
    for video in videos[1 : 1 + likes]:
        if _left(cfg, "like") <= 0:
            break
        # 只有「它自己的口味」才互动：
        #   你们口味 → 只 @（见 _maybe_comment）
        #   中立内容 → 不点赞（人也不会给完全无感的东西点赞）
        try:
            kind = profile_mod.classify(cfg, video)["kind"]
            if kind != "self":
                log.info("这条是 %s，跳过点赞：%s", kind, video.title[:20])
                continue
        except Exception:
            pass
        if random.random() > cfg.actions.like_chance:
            continue
        _engage_gap(cfg)
        try:
            actions.like(client, cfg, video.aid, confirm=True)
        except RiskControl:
            guard.trip_breaker(cfg, "点赞触发风控")
            return said
        except (BiliError, actions.ActionRefused) as exc:
            said.append(Said("like", video.bvid, "点赞", False, type(exc).__name__))
            continue
        guard.bump(cfg, "like")
        said.append(Said("like", video.bvid, video.title[:24], True))

    top = videos[0]
    if _left(cfg, "coin") > 0:
        _engage_gap(cfg)
        try:
            result = coin_dom.coin(cfg, top.bvid, count=1)
            if result.ok:
                # B 站弹窗默认投 2 枚（我改不动它的选项），所以一次记 2 枚
                guard.bump(cfg, "coin")
                guard.bump(cfg, "coin")
                said.append(Said("coin", top.bvid, top.title[:24], True, "2 枚"))
            else:
                said.append(Said("coin", top.bvid, top.title[:24], False, result.reason))
        except Exception as exc:  # noqa: BLE001 - 浏览器问题不该毁掉一轮
            said.append(Said("coin", top.bvid, top.title[:24], False, type(exc).__name__))

    if _left(cfg, "favorite") > 0 and random.random() <= cfg.actions.favorite_chance:
        _engage_gap(cfg)
        try:
            actions.favorite(client, cfg, top.aid, confirm=True)
            guard.bump(cfg, "favorite")
            said.append(Said("favorite", cfg.actions.fav_folder, top.title[:24], True))
        except RiskControl:
            guard.trip_breaker(cfg, "收藏触发风控")
        except (BiliError, actions.ActionRefused) as exc:
            said.append(Said("favorite", cfg.actions.fav_folder, top.title[:24], False, str(exc)[:40]))

    return said
