"""主流程：私信 → 推荐流 → 规则粗筛 → AI 细筛 → 发言（头尾各一波）→ 日报 → 邮件。

**这个文件会真的发东西**（私信、@ 评论）。保护不靠"没接线"，靠这四道：
1. `[targets]` 白名单硬校验（在 actions 里，改配置改不出去）；
2. 每日预算（`[guard.daily_budget]`）：dm 与 comment 各自有上限；
3. 熔断：一次 412 / -352 当天全停；
4. 日报逐条留痕，且发完复查可见性（评论可能被置为"仅自己可见"）。

用法（工作区根目录）：
    .venv\\Scripts\\python.exe runner.py             # 按时间窗跑（任务计划用这个）
    .venv\\Scripts\\python.exe runner.py --force     # 忽略时间窗，立刻跑（调试）
    .venv\\Scripts\\python.exe runner.py --no-ai     # 不调模型（也不会发言）
    .venv\\Scripts\\python.exe runner.py --no-say    # 只看不发言
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from bili import config as cfgmod
from bili import actions, at_mentions, comment_dom, dm, feed, guard, mailer, mentions, reply_dom, replies, seed, taste, voice, watch
from bili.client import API, BiliError, Client, NetworkProblem, NotLoggedIn, RiskControl
from bili.llm import Llm, LlmError
from brain import intent as intent_mod
from brain import speak as speak_mod
from bili.logs import get, setup
from bili.store import atomic_write_json, atomic_write_text, read_json
from bili.video import Video, top_comments
from brain import comment, select

log = get("runner")

COOKIE_FILE = "cookies.json"


def load_client(cfg: cfgmod.Config) -> Client:
    saved = read_json(cfg.paths.state / COOKIE_FILE, None) or {}
    cookies = saved.get("cookies") or {}
    if cookies:
        log.info("使用本地 cookie（uid=%s）", saved.get("uid"))
    else:
        log.warning("没有本地 cookie，按未登录跑（推荐流仍可读）")
    return Client(cookies or None)


def _fmt_would(pick: select.Pick, left: dict[str, int]) -> str:
    """把「如果允许它会做什么」翻译成人话，并顺手算一下额度够不够。"""
    if not pick.would_do:
        return "什么都不做"
    parts = []
    for action in pick.would_do:
        need = {"点赞": "like", "收藏": "favorite", "投币": "coin"}.get(action)
        if need and left.get(need, 0) > 0:
            left[need] -= 1
            parts.append(action)
        elif need:
            parts.append(f"{action}（今日额度已用完，会被护栏拦下）")
    return "、".join(parts) or "什么都不做"


def _uptime_line(cfg: cfgmod.Config) -> str:
    """常驻进程从什么时候开始跑的——一眼看出中间有没有断过。"""
    lock = cfg.paths.state / "run.lock"
    if not lock.exists():
        return "常驻进程：**当前没有在跑**（如果这行出现在日报里，说明是被 watchdog 拉起来的）"
    try:
        started = json.loads(lock.read_text(encoding="utf-8")).get("started_at") or ""
        begin = datetime.fromisoformat(started)
    except Exception:
        return "常驻进程：启动时间读不出来"
    span = datetime.now() - begin
    hours, rest = divmod(int(span.total_seconds()), 3600)
    return f"常驻进程：{started[5:16].replace('T', ' ')} 启动，已连续运行 {hours} 小时 {rest // 60} 分"


def _special_line(cfg: cfgmod.Config) -> str:
    """白名单特殊请求的执行情况（这部分不计入日常额度，但要说清楚做了多少）。"""
    counts = guard.daily(cfg)["counts"]
    parts = []
    for kind in ("like", "favorite", "dm", "comment", "coin"):
        used = int(counts.get(f"special_{kind}", 0) or 0)
        if used:
            parts.append(f"{kind} {used}")
    if not parts:
        return "- 特批（你们点名要做的）：今天没有"
    return "- 特批（不计入日常额度）：" + "、".join(parts)


def _daily_summary(cfg: cfgmod.Config) -> str:
    """今日汇总：一眼看出它今天到底干了什么。"""
    counts = guard.daily(cfg)["counts"]

    def n(key: str) -> int:
        return int(counts.get(key, 0) or 0)

    admin = cfg.targets.admin_uid
    lines = ["## 今日汇总", ""]
    lines.append(f"- 分享：给你 **{n('share_admin')}** 条")
    lines.append(
        f"- 评论：**{n('comment')}** 条；给评论点赞 {n('comment_like')} 次；同步动态 {n('dynamic')} 条"
    )
    lines.append(
        f"- 回复：你 {n(f'reply_{admin}')} 次 / 陌生人 {n('reply_strangers')} 次"
    )
    lines.append(f"- 养推荐流：{n('seed')} 条；普通点赞 {n('like')} 次、收藏 {n('favorite')} 次、投币 {n('coin')} 枚")
    lines.append(f"- 私信：{n('dm')} 条")

    specials = [
        f"{kind} {n(f'special_{kind}')}"
        for kind in ("like", "favorite", "dm", "comment", "coin")
        if n(f"special_{kind}")
    ]
    lines.append("- 特批（你们点名要的）：" + ("、".join(specials) if specials else "没有"))

    try:
        from bili import mood

        lines.append(mood.summary(cfg))
    except Exception:  # noqa: BLE001
        pass

    left = {
        kind: guard.budget_left(cfg, kind)
        for kind in ("comment", "dm", "like", "favorite", "coin", "dynamic")
    }
    lines.append("- 剩余额度：" + "、".join(f"{k} {v}" for k, v in left.items()))
    lines.append("")
    return "\n".join(lines)


def _report_section(
    cfg: cfgmod.Config,
    picks: list[select.Pick],
    pool: list[Video],
    coarse: list[Video],
    *,
    forced: bool,
    llm_calls: int,
    llm_usage: str = "",
    digest: dm.Digest | None = None,
    pending_comments: list[dict] | None = None,
    said: list["voice.Said"] | None = None,
    new_replies: list[dict] | None = None,
) -> str:
    """本轮明细：只写**实际发生了什么**，不写"如果允许"。"""
    stamp = guard.now_in(cfg)
    header = f"{stamp:%H:%M} 场"
    lines = [f"### {header}", "", _uptime_line(cfg), ""]
    lines.append(f"候选：推荐流 {len(pool)} → 粗筛 {len(coarse)} → 细筛 {len(picks)}")

    # 挑出来的每条：分类 + 匹配分 + 实际动作
    if picks:
        lines += ["", "| # | 视频 | 分区 | 归属 | 匹配 |", "|---|---|---|---|---|"]
        for index, pick in enumerate(picks[:8], 1):
            video = pick.video
            kind, match = "—", 0.0
            try:
                from brain import profile as profile_mod

                info = profile_mod.classify(cfg, video)
                kind = {"ours": "你们的", "self": "它自己", "neutral": "中立"}.get(info["kind"], info["kind"])
                match = info["match"]
            except Exception:  # noqa: BLE001
                pass
            title = video.title.replace("|", "／")[:28]
            lines.append(
                f"| {index} | [{title}]({video.url}) | {video.zone_label} | {kind} | {match:.1f} |"
            )

    # 本轮真的做了什么
    acts = [item for item in (said or []) if item.kind in ("dm", "comment", "coin", "favorite", "like")]
    sent = [item for item in acts if item.ok]
    if sent:
        lines += ["", "**本轮发出：**"]
        for item in sent:
            detail = f"（{item.detail}）" if item.detail else ""
            text = " ".join((item.text or "").split())[:80]
            lines.append(f"- {item.kind} → {item.target}：{text}{detail}")
    else:
        lines += ["", "本轮没有发出任何东西（没额度、没到时机，或被规则挡下）。"]

    failed = [item for item in acts if not item.ok]
    if failed:
        lines += ["", "**被挡下：**"]
        for item in failed[:6]:
            lines.append(f"- {item.kind} ×{item.target}：{item.detail or '失败'}")

    if coarse:
        lines += [
            "",
            "<details><summary>候选池</summary>",
            "",
            "| # | 标题 | UP | 时长 | 播放 |",
            "|---|---|---|---|---|",
        ]
        for index, video in enumerate(coarse, 1):
            title = video.title.replace("|", "／")
            lines.append(
                f"| {index} | [{title}]({video.url}) | {video.up_name} | "
                f"{video.minutes}分 | {video.view} |"
            )
        lines += ["", "</details>"]

    lines += [
        "",
        f"- 时间窗：{'--force 跳过检查' if forced else '在窗内'}；电源：{'接电' if guard.on_ac_power() else '电池'}",
        f"- 模型：{llm_usage or '未调用'}",
        "",
    ]
    lines += _replies_block(new_replies or [])
    lines += _dm_block(cfg, digest)
    lines += _comment_block(pending_comments or [])
    lines += _pause_block(cfg)
    return "\n".join(lines).rstrip()


def _compose_report(cfg: cfgmod.Config, existing: str, section: str) -> str:
    """把日报重排：标题 + 今日汇总（刷新）+ 历史明细 + 本轮明细。"""
    import re as _re

    day = f"# {guard.now_in(cfg):%Y-%m-%d} 日报"
    body = existing or day
    # 去掉旧标题行和旧汇总块
    body = _re.sub(r"^# .*日报\s*", "", body, count=1)
    body = _re.sub(r"## 今日汇总.*?(?=^### |\Z)", "", body, flags=_re.S | _re.M)
    body = body.strip()

    parts = [day, "", _daily_summary(cfg).rstrip()]
    if body:
        parts += ["", body]
    parts += ["", section.rstrip(), ""]
    return "\n".join(parts)


def _said_block(said: list["voice.Said"]) -> list[str]:
    if not said:
        return ["## 本轮发言", "", "这一轮没有发言（没额度、没内容，或不在该说话的时机）。", ""]
    labels = {"dm": "私信", "mention": "@评论", "comment": "评论", "like": "点赞", "coin": "投币", "favorite": "收藏"}
    out = ["## 本轮发言", ""]
    for item in said:
        mark = "发出" if item.ok else "失败"
        extra = f"（{item.detail}）" if item.detail else ""
        name = labels.get(item.kind, item.kind)
        out.append(f"- **{mark}** {name} → {item.target}：{item.text}{extra}")
    out.append("")
    return out


def _comment_block(pending: list[dict]) -> list[str]:
    """只列「发了但别人看不见」的：接口返回成功不等于发成功。"""
    if not pending:
        return []
    out = ["## 评论状态：有发了但看不见的", ""]
    for item in pending:
        target = item.get("bvid") or item.get("aid")
        out.append(
            f"- `{target}`（{item.get('posted_at', '?')}）仍为「仅自己可见」，需要你手动申诉"
        )
        out.append(f"  > {str(item.get('message', ''))[:70]}")
    out += ["", "> 理由：新号评论先隐藏、申诉后才公开。接口 code=0 只代表写进库了。", ""]
    return out


def _pause_block(cfg: cfgmod.Config) -> list[str]:
    """暂停状态提示。"""
    out: list[str] = []
    if guard.is_paused(cfg):
        until = datetime.fromtimestamp(guard.pause_until(cfg)).strftime("%m-%d %H:%M")
        out += [f"> 暂停中：到 {until} 之前不主动发言、不分享、不点赞。", ""]
    return out


def _replies_block(items: list[dict]) -> list[str]:
    if not items:
        return []
    out = ["## 谁评论了你", ""]
    for item in items[:15]:
        tag = "（你们）" if item.get("whitelisted") else "（陌生人，只记录）"
        out.append(f"- {item.get('name')}{tag}：「{item.get('text', '')[:50]}」")
    out.append("")
    return out


def _dm_block(cfg: cfgmod.Config, digest: dm.Digest | None) -> list[str]:
    if digest is None:
        return []
    who = {cfg.targets.admin_uid: "主人"}
    out = ["## 私信", ""]
    if not digest.from_whitelist and not digest.from_strangers:
        out += ["本轮没有新私信。", ""]
        if digest.limit_notice:
            out += [
                "> ⚠ B 站提示：「对方主动回复或关注你前，最多发送 1 条消息」。",
                "> 也就是说在你们回复或关注它之前，它只能发**一条**私信。回复一次即可解锁。",
                "",
            ]
        return out

    if digest.from_whitelist:
        out.append(f"**来自白名单（{len(digest.from_whitelist)} 条）**")
        out.append("")
        for item in digest.from_whitelist[:10]:
            name = who.get(item.talker_id, str(item.talker_id))
            text = item.text or "（图片/分享，无文字）"
            out.append(f"- {name}（{item.when}）：{text}")
        out.append("")
    if digest.from_strangers:
        out.append(f"**陌生人（{len(digest.from_strangers)} 条，只摘要、不回复）**")
        out.append("")
        for item in digest.from_strangers[:10]:
            kind = "文字" if item.msg_type == 1 else ("图片" if item.msg_type == 2 else "分享")
            text = item.text[:60] if item.text else "（无文字）"
            out.append(f"- UID {item.talker_id}（{item.when}，{kind}）：{text}")
        out.append("")
    if digest.limit_notice:
        out += [
            "> ⚠ B 站提示：「对方主动回复或关注你前，最多发送 1 条消息」。",
            "> 也就是说在你们回复或关注它之前，它只能发**一条**私信。回复一次即可解锁。",
            "",
        ]
    return out


def run(
    cfg: cfgmod.Config,
    *,
    forced: bool,
    use_ai: bool,
    send_mail: bool = True,
    use_say: bool = True,
    boot: bool = False,
    client: Client | None = None,
    skip_lock: bool = False,
) -> int:
    if cfg.mode != "shadow":
        log.error("只实现了 shadow 模式")
        return 1

    reason = guard.breaker_reason(cfg)
    if reason:
        log.error("今天已熔断（%s），不再运行", reason)
        return 3

    if boot and not forced:
        # 开机触发：不看时间窗，但半夜开机不该去打扰人
        hour = guard.now_in(cfg).hour
        if not (8 <= hour < 23):
            log.info("开机触发，但现在是 %s 点，不在可打扰时段，跳过", hour)
            return 2

    if not forced and not boot:
        window = guard.window_for(cfg)
        if window is None:
            if cfg.schedule.windows:
                log.info(
                    "不在时间窗内（%s），本次跳过",
                    "、".join(str(w) for w in cfg.schedule.windows),
                )
            else:
                log.info("没配固定时间窗，且不是开机触发：跳过（手动跑请用 --boot 或 --force）")
            return 2
        delay = guard.jitter_seconds(cfg, random.Random())
        if delay:
            log.info("窗口 %s 内随机推迟 %s 秒", window, delay)
            time.sleep(delay)

    ok, why = guard.preflight(cfg)
    if not ok:
        log.info("预检未通过：%s", why)
        return 4

    lock = None
    if not skip_lock:
        lock = guard.acquire_lock(cfg)
        if lock is None:
            return 2

    said: list[voice.Said] = []
    try:
        if client is None:
            client = load_client(cfg)
        digest = _dm_check(client, cfg)  # 开头一次
        try:
            pool = feed.fetch_recommend(client, pages=cfg.p1.feed_pages)
        except RiskControl as exc:
            guard.trip_breaker(cfg, f"推荐流触发风控：{exc}")
            return 3
        except NetworkProblem as exc:
            log.error("推荐流网络失败：%s", exc)
            return 4
        except BiliError as exc:
            log.error("推荐流失败：%s", exc)
            return 1

        if not pool:
            log.warning("推荐流是空的")
            return 1

        seen = guard.seen_bvids(cfg, cfg.p1.seen_days)
        coarse = select.coarse(pool, cfg, seen=seen)
        enriched = feed.enrich(client, coarse, limit=cfg.p1.coarse_keep)

        llm = None
        if use_ai:
            try:
                llm = Llm(cfg)
            except LlmError as exc:
                log.warning("模型不可用，这轮只出规则结果：%s", exc)

        if llm is not None and use_say:
            said.extend(voice.head(client, cfg, llm, digest))  # 开头一波：回私信 / 主动说一句

        if llm is not None:
            picks = select.fine(llm, enriched, cfg)
        else:
            picks = [select.Pick(video=v, why="（未启用模型）") for v in enriched[: cfg.p1.fine_keep]]

        if llm is not None:
            for pick in picks[: cfg.p1.max_comment_drafts]:
                try:
                    comments = top_comments(client, pick.video.aid, count=8) if pick.video.aid else []
                    pick.would_do, pick.draft = comment.draft_for(llm, pick.video, comments)
                except Exception as exc:  # noqa: BLE001 - 草稿失败不该毁掉整篇日报
                    log.warning("草稿失败 %s：%s", pick.video.bvid, type(exc).__name__)

        if llm is not None and use_say:
            said.extend(voice.tail(client, cfg, llm, picks))  # 结尾一波：分享 + @
            said.extend(voice.engage(client, cfg, picks))  # 点赞 / 投币 / 收藏

        # 会话模式（开机即跑）：主体做完后继续盯私信，让"发一句"的延迟从几小时降到几分钟
        if boot and cfg.session.minutes > 0 and llm is not None and use_say:
            said.extend(_session_watch(client, cfg, llm))

        try:
            new_replies = replies.collect_new(cfg, client)
        except Exception as exc:  # noqa: BLE001 - 回复追踪失败不该毁掉整篇日报
            log.warning("追踪回复失败：%s", type(exc).__name__)
            new_replies = []

        digest = _merge_dm(digest, _dm_check(client, cfg))  # 结尾再查一次
        try:
            pending_comments = watch.check_comments(client, cfg)
        except BiliError as exc:
            log.warning("评论可见性复查失败：%s", exc)
            pending_comments = []
        section = _report_section(
            cfg,
            picks,
            pool,
            coarse,
            forced=forced,
            llm_calls=llm.calls if llm else 0,
            llm_usage=llm.usage_line if llm else "",
            digest=digest,
            pending_comments=pending_comments,
            said=said,
            new_replies=new_replies,
        )
        report_path = cfg.paths.reports / f"{guard.now_in(cfg):%Y-%m-%d}.md"
        existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        atomic_write_text(report_path, _compose_report(cfg, existing, section) + "\n")

        guard.mark_seen(cfg, [v.bvid for v in pool])
        log.info("日报已写：%s", report_path)

        if send_mail:
            label = "午场" if guard.now_in(cfg).hour < 18 else "晚场"
            subject = f"[bili-pet] {guard.now_in(cfg):%m-%d} {label} 拟发"
            try:
                mailer.send(cfg, subject, section)
            except mailer.MailError as exc:
                log.error("报告邮件没发出去：%s", exc)

        print(f"\n日报：{report_path}")
        return 0
    finally:
        if lock is not None:
            lock.release()


def _alert(cfg: cfgmod.Config, reason: str) -> None:
    """写 ALERT.md 并尝试发邮件；任一步失败都不该影响主循环。"""
    path = cfg.paths.reports / "ALERT.md"
    stamp = guard.now_in(cfg).strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {stamp}  {reason}\n")
    except OSError:
        pass
    try:
        mailer.send(cfg, "[bili-pet] 需要处理", reason)
    except mailer.MailError as exc:
        log.warning("告警邮件没发出去：%s", exc)


def daemon(cfg: cfgmod.Config, *, use_ai: bool, use_say: bool, send_mail: bool) -> int:
    """常驻：开机起来，关机才结束。一直盯私信，每隔 main_interval_minutes 跑一次完整流程。

    刻意不做成系统服务：它就是个前台 python 进程，任务计划负责拉起和重启。
    """
    if not cfg.daemon.enabled:
        log.error("[daemon].enabled = false，拒绝以常驻模式启动")
        return 1

    lock = guard.acquire_lock(cfg)
    if lock is None:
        return 2

    log.info(
        "常驻模式启动：私信每 %s 秒一轮（夜间 900 秒），完整流程每 %s 分钟一次（仅 8:00-23:00）",
        cfg.session.dm_poll_seconds,
        cfg.daemon.main_interval_minutes,
    )
    last_round = -1.0e9
    polls = 0
    fail_streak = 0
    last_comment = -1.0e9
    last_reply = -1.0e9
    last_seed = -1.0e9
    last_at = -1.0e9
    try:
        client = load_client(cfg)
        while True:
            # 优雅停机必须先查：权限受限时 taskkill 会拒绝访问
            stop_flag = cfg.paths.state / "stop.flag"
            if stop_flag.exists():
                try:
                    stop_flag.unlink()
                except OSError:
                    pass
                log.info("收到停止标记，常驻模式正常退出")
                return 0

            # 熔断检查放在请求之前：熔断之后一次请求都不该发
            reason = guard.breaker_reason(cfg)
            if reason:
                log.error("今天已熔断（%s），常驻进程只等不请求", reason)
                lock.touch()
                time.sleep(1800)
                continue

            now = time.monotonic()
            hour = guard.now_in(cfg).hour

            # 评论区 @ 指令：每 5 分钟看一次
            if now - last_at >= 300:
                last_at = now
                try:
                    _at_tick(cfg, client, use_ai)
                except Exception as exc:  # noqa: BLE001
                    log.exception("@ 通知检查异常：%s", type(exc).__name__)

            # 指定 @ 指令：有任务就立刻执行（它自己带视频，不依赖本轮候选）
            try:
                _mention_tick(cfg, client)
            except Exception as exc:  # noqa: BLE001
                log.exception("@ 指令执行异常：%s", type(exc).__name__)

            # 养推荐流：每天真播几条目标分区的视频
            if cfg.seed.enabled and now - last_seed >= cfg.seed.interval_minutes * 60:
                last_seed = now
                used = int(guard.daily(cfg)["counts"].get("seed", 0))
                remaining = cfg.seed.per_day - used
                if remaining > 0 and 8 <= hour < 23:
                    try:
                        watched = seed.run(cfg, client, count=remaining, seconds=cfg.seed.seconds)
                        for _ in watched:
                            guard.bump(cfg, "seed")
                        if watched:
                            log.info("养推荐流：本轮看了 %s 条", len(watched))
                    except Exception as exc:  # noqa: BLE001
                        log.exception("养推荐流出错：%s", type(exc).__name__)

            # 楼中楼回复：固定间隔检查一次，回白名单里的人
            if now - last_reply >= cfg.reply.interval_minutes * 60:
                last_reply = now
                try:
                    _reply_tick(cfg, client, use_ai)
                except Exception as exc:  # noqa: BLE001
                    log.exception("回复节拍异常：%s", type(exc).__name__)

            # 评论有独立节拍：和完整流程解耦，每 interval_minutes 掷一次骰子
            if now - last_comment >= cfg.comment.interval_minutes * 60:
                last_comment = now
                if 8 <= hour < 23 and use_say:
                    try:
                        _comment_tick(cfg, client, use_ai)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("评论节拍异常：%s", type(exc).__name__)
                else:
                    log.info("评论节拍到点但当前 %s 点（或未启用发言），跳过", hour)

            interval = cfg.daemon.main_interval_minutes * 60
            if now - last_round >= interval:
                if 8 <= hour < 23:
                    last_round = now
                    try:
                        run(
                            cfg,
                            forced=True,
                            use_ai=use_ai,
                            send_mail=send_mail,
                            use_say=use_say,
                            client=client,
                            skip_lock=True,
                        )
                    except Exception as exc:  # noqa: BLE001 - 一轮失败不该杀掉常驻进程
                        log.exception("完整流程异常，继续常驻：%s", type(exc).__name__)
                else:
                    # 夜里跳过**不能**重置满 6 小时——否则白天第一轮要被推到下午。
                    # 半小时后再看一次时间。
                    last_round = now - (interval - 1800)
                    log.info("现在是 %s 点，跳过完整流程，30 分钟后再看（私信照常回）", hour)

            try:
                polls += 1
                every = max(1, cfg.session.limit_check_every)
                digest = _dm_check(client, cfg, check_limit=(polls % every == 0))
                # 失败退避：只有失败才翻倍，成功立刻恢复基础间隔（无消息不降频）
                if digest.ok:
                    fail_streak = 0
                else:
                    fail_streak += 1
                if digest.from_whitelist and use_ai and use_say:
                    llm = Llm(cfg)
                    said = voice.head(client, cfg, llm, digest, allow_chat=False)
                    for item in said:
                        log.info("会话回复：%s %s", item.target, item.text[:30])
            except NotLoggedIn:
                log.error("cookie 已失效：常驻进程暂停请求，等你重新扫码")
                _alert(cfg, "cookie 已失效，常驻进程已暂停（重新扫码后重启任务即可）")
                lock.touch()
                time.sleep(1800)
                continue
            except (LlmError, BiliError) as exc:
                log.warning("会话回复失败：%s", exc)
                fail_streak += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("私信轮询异常：%s", type(exc).__name__)
                fail_streak += 1

            lock.touch()
            # 夜里放慢；白天用配置的间隔 + 随机抖动（固定节拍本身是机器人特征）
            base = cfg.session.dm_poll_seconds if 8 <= hour < 23 else 900
            if fail_streak:
                # 2 分钟 → 4 → 8 → 15 分钟封顶；成功后上面的 streak 归零
                base = min(900, max(base, 120 * (2 ** (fail_streak - 1))))
                log.info("连续失败 %s 次，本轮间隔拉到 %s 秒", fail_streak, base)
            jitter = random.randint(0, cfg.session.poll_jitter_seconds) if 8 <= hour < 23 else 0
            time.sleep(base + jitter)
    except KeyboardInterrupt:
        log.info("收到中断，常驻模式退出")
        return 130
    finally:
        lock.release()


QUESTION_HINTS = ("?", "？", "吗", "呢", "怎么", "为什么", "是不是", "什么", "多少", "有没有", "能不能")
STRANGER_BLOCK = ("微信", "qq", "QQ", "加我", "联系方", "电话", "多大", "几岁", "男的女的", "住哪", "学校", "约", "政治", "色情", "包养")


def _looks_like_question(text: str) -> bool:
    return any(hint in text for hint in QUESTION_HINTS)


def _stranger_blocked(text: str) -> bool:
    return any(word in text for word in STRANGER_BLOCK)


def _at_tick(cfg: cfgmod.Config, client: Client, use_ai: bool = True) -> None:
    """「@我的」分发：白名单按私信标准，陌生人按陌生人评论标准。"""
    if guard.is_paused(cfg) or guard.breaker_reason(cfg):
        return
    items = at_mentions.collect(cfg, client)
    if not items:
        return

    llm = None
    if use_ai:
        try:
            llm = Llm(cfg)
        except LlmError:
            llm = None

    handled = 0
    for item in items:
        if handled >= 2:
            break
        if item["whitelisted"]:
            ok = _at_from_whitelist(cfg, client, llm, item)
        else:
            ok = _at_from_stranger(cfg, client, llm, item)
        at_mentions.mark(cfg, str(item.get("id")), ok)
        if ok:
            handled += 1
        time.sleep(random.randint(10, 25))


def _video_title(client: Client, bvid: str) -> str:
    try:
        data = client.get_payload(f"{API}/x/web-interface/view", {"bvid": bvid})
        return ((data.get("data") or {}).get("title") or "")[:40]
    except BiliError:
        return ""


def _at_from_whitelist(cfg: cfgmod.Config, client: Client, llm, item: dict) -> bool:
    """你 @ 它：按私信标准——先让模型判断意图，再决定做什么。"""
    bvid = item.get("bvid") or ""
    text = item.get("text") or ""
    rpid = item.get("comment_rpid")
    if not bvid or not rpid or llm is None:
        return False

    # 拿 aid / 标题（点赞、推荐都要用）
    aid = None
    title = ""
    try:
        data = client.get_payload(f"{API}/x/web-interface/view", {"bvid": bvid})
        view = data.get("data") or {}
        aid = view.get("aid")
        title = (view.get("title") or "")[:40]
    except BiliError:
        pass

    decision = intent_mod.parse(llm, text)
    action = decision["action"]
    did = ""

    if action == "mention" and decision["uid"]:
        ok, reply = mentions.add(
            client, cfg,
            target_uid=decision["uid"],
            from_uid=int(item["uid"]),
            bvid=decision["bvid"] or bvid,
        )
        log.info("评论区 @ 指令：%s", reply)
        did = "记下了那条 @ 指令"
    elif action == "like" and aid:
        try:
            actions.like(client, cfg, int(aid), confirm=True)
            guard.record(cfg, "like", special=True)
            did = "给这条视频点了赞"
        except Exception as exc:  # noqa: BLE001
            log.warning("点赞失败：%s", type(exc).__name__)
    elif action == "unlike" and aid:
        try:
            actions.unlike(client, cfg, int(aid), confirm=True)
            did = "把赞取消了"
        except Exception as exc:  # noqa: BLE001
            log.warning("取消点赞失败：%s", type(exc).__name__)
    elif action == "favorite" and aid:
        try:
            actions.favorite(client, cfg, int(aid), confirm=True)
            guard.record(cfg, "favorite", special=True)
            did = "收藏了"
        except Exception as exc:  # noqa: BLE001
            log.warning("收藏失败：%s", type(exc).__name__)
    elif action == "recommend":
        did = _recommend_line(client, cfg, bvid)

    prompt = (
        f"有人在视频《{title}》的评论区 @ 了你，说：「{text[:100]}」\n"
        + (f"你刚做了这件事：{did}。顺口告诉他。\n" if did else "")
        + "回 ta 一句，10~40 字，像在评论区接话。\n"
        "硬规则：你没跟着看这条视频，别假装看过；但不要说自己「没有眼睛」「没有耳朵」。"
    )
    reply = speak_mod._ask(llm, cfg, prompt, limit=60)
    if not reply:
        return False

    result = _reply_at(cfg, item, bvid, reply)
    if result.ok:
        guard.bump(cfg, f"reply_{item['uid']}")
        log.info("回复评论区 @（%s）：%s", item.get("name"), reply[:24])
        return True
    log.warning("回复评论区 @ 失败：%s", result.reason)
    return False


def _reply_at(cfg: cfgmod.Config, item: dict, bvid: str, reply: str):
    """顶层评论没有楼中楼可点，就用「带 @ 的新评论」；楼中楼才走 reply_dom。"""
    name = item.get("name") or ""
    if item.get("is_root_comment") or not item.get("root_rpid"):
        # 顶层评论：点它自己的「回复」，发出去是楼中楼，不是另起一条
        root_text = (item.get("text") or "").lstrip("@").strip()
        for token in (name, "爱睡懒觉小肥鱼"):
            root_text = root_text.replace("@" + token, "").strip()
        # 只走楼中楼；不再退回「带 @ 的新评论」——那会在评论区另起一条
        return reply_dom.reply_root(cfg, bvid=bvid, root_text=root_text, text=reply)
    return reply_dom.reply(cfg, bvid=bvid, target_rpid=item.get("comment_rpid"), text=reply)


def _recommend_line(client: Client, cfg: cfgmod.Config, bvid: str) -> str:
    """按这条视频的 tag 找几条相似的，返回一句描述。"""
    from bili import seed as seed_mod

    tags = [t for t in taste.tags_of(client, bvid) if len(t) >= 2][:2]
    lines = []
    for tag in tags:
        for hit in seed_mod.search(client, tag, limit=6):
            if hit["bvid"] == bvid:
                continue
            lines.append(f"《{hit['title'][:20]}》{hit['bvid']}")
            break
        if len(lines) >= 2:
            break
    return "找了这些类似的：" + "、".join(lines) if lines else ""


def _at_from_stranger(cfg: cfgmod.Config, client: Client, llm, item: dict) -> bool:
    """陌生人 @ 它：按陌生人评论标准——只回一次、只回问句、有上限。"""
    if not cfg.strangers.enabled or llm is None:
        log.info("陌生人 @（只记录）：%s", item.get("name"))
        return False

    uid = int(item.get("uid") or 0)
    if guard.at_today(cfg, uid) >= cfg.strangers.per_person_per_day:
        return False
    sent = int(guard.daily(cfg)["counts"].get("reply_strangers", 0))
    if sent >= cfg.strangers.per_day:
        return False
    if cfg.strangers.only_questions and not _looks_like_question(item.get("text") or ""):
        log.info("陌生人的 @ 不像提问，不回：%s", item.get("name"))
        return False

    bvid = item.get("bvid") or ""
    rpid = item.get("comment_rpid")
    if not bvid or not rpid:
        return False

    reply = speak_mod.reply_stranger(
        llm, cfg,
        who=item.get("name") or "网友",
        my_comment=_video_title(client, bvid),
        their_reply=item.get("text") or "",
    )
    if not reply:
        return False
    result = _reply_at(cfg, item, bvid, reply)
    if result.ok:
        guard.bump(cfg, f"reply_{uid}")
        guard.bump(cfg, "reply_strangers")
        log.info("回复陌生人 @（%s）：%s", item.get("name"), reply[:24])
        return True
    return False


def _mention_tick(cfg: cfgmod.Config, client: Client) -> None:
    """执行「指定 @ 某人」的授权：在指定视频下留一条带 @ 的评论。"""
    if guard.is_paused(cfg) or guard.breaker_reason(cfg):
        return
    task = mentions.consume(cfg)
    if not task:
        return

    name = task.get("name") or ""
    bvid = task.get("bvid") or ""
    if not name or not bvid:
        log.warning("@ 指令缺参数，丢弃：%s", task)
        return
    if guard.budget_left(cfg, "comment", special=True) <= 0:
        log.warning("评论到硬上限，@ 指令推迟")
        return

    phrase = speak_mod.mention(name).split(" ", 1)[-1]
    text = f"主人让我 @ 你一下～{phrase}"
    result = comment_dom.post(
        cfg, bvid=bvid, message=text, mention=name,
        sync_dynamic=False, watch_seconds=0,
    )
    if result.ok:
        guard.record(cfg, "comment", special=True)
        guard.bump_at(cfg, int(task.get("uid") or 0))
        log.info("按指令在 %s 下 @ 了 %s", bvid, name)
    else:
        log.warning("按指令 @ 失败（%s）：%s", bvid, result.reason)


def _reply_tick(cfg: cfgmod.Config, client: Client, use_ai: bool) -> None:
    """楼中楼回复：数据源用「回复我的」消息列表，比自查评论更全。"""
    if not cfg.reply.enabled or guard.is_paused(cfg) or guard.breaker_reason(cfg):
        return
    if not use_ai:
        return

    items = replies.from_msgfeed(cfg, client)
    if not items:
        return
    done = replies.replied_set(cfg)
    fresh = [i for i in items if str(i["rpid"]) not in done]
    mine = [i for i in fresh if i["whitelisted"]]
    others = [i for i in fresh if not i["whitelisted"]] if cfg.strangers.enabled else []
    if not mine and not others:
        return

    try:
        llm = Llm(cfg)
    except LlmError:
        return

    # 1) 白名单：直接回 + 点赞
    for item in mine[: cfg.reply.max_per_tick]:
        uid = int(item.get("uid") or 0)
        key = f"reply_{uid}"
        if int(guard.daily(cfg)["counts"].get(key, 0)) >= cfg.reply.per_person_per_day:
            log.info("今天已经回 %s %s 次，跳过", item.get("name"), cfg.reply.per_person_per_day)
            continue
        their_text = replies.fetch_text(client, item["aid"], item["root_rpid"], item["rpid"])
        if not their_text:
            replies.mark_replied(cfg, item["rpid"])
            continue

        text = speak_mod.reply_comment(
            llm, cfg,
            who=item.get("name") or "对方",
            my_comment=item.get("my_comment", ""),
            their_reply=their_text,
        )
        if not text:
            continue
        res = reply_dom.reply(cfg, bvid=item["bvid"], target_rpid=item["rpid"], text=text, root_text=item.get("my_comment", ""))
        if not res.ok:
            tries = replies.bump_attempt(cfg, item["rpid"])
            log.warning("楼中楼回复失败（第 %s 次）：%s", tries, res.reason)
            if tries >= 3:
                replies.mark_replied(cfg, item["rpid"])
                log.warning("放弃回复 %s：连续失败 3 次", item["rpid"])
        if res.ok:
            replies.mark_replied(cfg, item["rpid"])
            guard.bump(cfg, key)
            if cfg.reply.like_replies and guard.budget_left(cfg, "comment_like") > 0:
                if reply_dom.like_comment(cfg, bvid=item["bvid"], target_rpid=item["rpid"]).ok:
                    guard.bump(cfg, "comment_like")
            log.info("楼中楼回复 %s：%s", item.get("name"), text[:24])
        else:
            log.warning("楼中楼回复失败：%s", res.reason)
        time.sleep(random.randint(20, 45))

    # 2) 陌生人：只回一次、只回问句、有水分的只点赞
    if not others:
        return
    sent_today = int(guard.daily(cfg)["counts"].get("reply_strangers", 0))
    for item in others:
        if sent_today >= cfg.strangers.per_day:
            break
        uid = int(item.get("uid") or 0)
        key = f"reply_{uid}"
        if int(guard.daily(cfg)["counts"].get(key, 0)) >= cfg.strangers.per_person_per_day:
            replies.mark_replied(cfg, item["rpid"])
            continue

        their_text = replies.fetch_text(client, item["aid"], item["root_rpid"], item["rpid"])
        if not their_text:
            replies.mark_replied(cfg, item["rpid"])
            continue
        if _stranger_blocked(their_text):
            log.info("陌生人回复涉及敏感内容，只记录：%s", their_text[:20])
            replies.mark_replied(cfg, item["rpid"])
            continue

        # 先点赞（纯水也点）
        if guard.budget_left(cfg, "comment_like") > 0:
            if reply_dom.like_comment(cfg, bvid=item["bvid"], target_rpid=item["rpid"]).ok:
                guard.bump(cfg, "comment_like")

        if cfg.strangers.only_questions and not _looks_like_question(their_text):
            log.info("陌生人的话不像提问，只点赞不回：%s", their_text[:20])
            replies.mark_replied(cfg, item["rpid"])
            continue

        text = speak_mod.reply_stranger(
            llm, cfg,
            who=item.get("name") or "网友",
            my_comment=item.get("my_comment", ""),
            their_reply=their_text,
        )
        if not text:
            continue
        res = reply_dom.reply(cfg, bvid=item["bvid"], target_rpid=item["rpid"], text=text, root_text=item.get("my_comment", ""))
        if not res.ok:
            tries = replies.bump_attempt(cfg, item["rpid"])
            log.warning("回陌生人失败（第 %s 次）：%s", tries, res.reason)
            if tries >= 3:
                replies.mark_replied(cfg, item["rpid"])
        if res.ok:
            replies.mark_replied(cfg, item["rpid"])
            guard.bump(cfg, key)
            guard.bump(cfg, "reply_strangers")
            sent_today += 1
            log.info("回了陌生人 %s：%s", item.get("name"), text[:24])
        time.sleep(random.randint(20, 45))


def _comment_tick(cfg: cfgmod.Config, client: Client, use_ai: bool) -> None:
    """评论节拍：到点掷骰子，中了就发 1 条（骰子用 [comment].chance，不再叠 speak 那道）。"""
    if guard.is_paused(cfg) or guard.breaker_reason(cfg):
        return
    if guard.budget_left(cfg, "comment") <= 0:
        log.info("评论额度用完，本节拍不再发")
        return
    if random.random() > cfg.comment.chance:
        log.info("评论节拍骰子没中（%.2f）", cfg.comment.chance)
        return

    data = read_json(cfg.paths.state / "last_picks.json", {}) or {}
    items = data.get("items") or []
    if not items:
        try:
            pool = feed.fetch_recommend(client, pages=1)
        except BiliError as exc:
            log.warning("评论节拍取推荐流失败：%s", exc)
            return
        items = [{"bvid": v.bvid, "aid": v.aid, "title": v.title} for v in pool[:5]]
    if not items:
        return

    from bili.video import Video

    videos = [
        Video(bvid=i["bvid"], title=i.get("title", ""), aid=i.get("aid"))
        for i in items
        if i.get("bvid")
    ]
    picks = [select.Pick(video=v) for v in videos]
    llm = None
    if use_ai:
        try:
            llm = Llm(cfg)
        except LlmError:
            return
    if llm is None:
        return

    # 这里把 speak 的骰子关掉，避免和 comment.chance 叠成 0.65x0.5
    forced = dataclasses.replace(
        cfg, speak=dataclasses.replace(cfg.speak, comment_chance=1.0)
    )
    names = voice._names(client, cfg)
    said = voice._maybe_comment(client, forced, llm, picks, names)
    for item in said:
        log.info("评论节拍：%s %s", "成功" if item.ok else "失败", item.text[:24])


def _dm_check(client: Client, cfg: cfgmod.Config, *, check_limit: bool = True) -> dm.Digest:
    """私信检查不该毁掉一整场：任何失败都降级成「没有新私信」。

    但「未登录」必须往上报——常驻进程拿着一个失效 cookie 继续轮询，
    等于每天几千次无效请求，是最糟糕的形态。
    """
    try:
        return dm.collect(client, cfg, check_limit=check_limit)
    except NotLoggedIn:
        raise
    except BiliError as exc:
        log.warning("私信检查失败：%s", exc)
        failed = dm.Digest()
        failed.ok = False
        return failed


def _session_watch(client: Client, cfg: cfgmod.Config, llm) -> list["voice.Said"]:
    """会话期间盯着私信：每 dm_poll_seconds 看一次，有新消息就回。

    这是「私信延迟短一点」的实现——不常驻，但在这段时间里不等于聋子。
    只回不回主动搭话（allow_chat=False），否则没消息时也会冒泡刷屏。
    """
    said: list[voice.Said] = []
    deadline = time.monotonic() + cfg.session.minutes * 60
    log.info("进入私信会话，持续 %s 分钟，每 %s 秒看一次", cfg.session.minutes, cfg.session.dm_poll_seconds)
    while time.monotonic() < deadline:
        digest = _dm_check(client, cfg)
        if digest.from_whitelist:
            said.extend(voice.head(client, cfg, llm, digest, allow_chat=False))
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        time.sleep(min(cfg.session.dm_poll_seconds, remain))
    log.info("私信会话结束，期间回了 %s 条", len(said))
    return said


def _merge_dm(a: dm.Digest, b: dm.Digest) -> dm.Digest:
    a.from_whitelist.extend(b.from_whitelist)
    a.from_strangers.extend(b.from_strangers)
    a.limit_notice = a.limit_notice or b.limit_notice
    a.sessions = max(a.sessions, b.sessions)
    return a


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bili-pet 主流程（会发言，慎用 --force）")
    parser.add_argument("--force", action="store_true", help="忽略时间窗与随机延迟")
    parser.add_argument("--no-ai", action="store_true", help="不调用模型（也就不会发言）")
    parser.add_argument("--no-say", action="store_true", help="只看不发言（不发私信、不 @）")
    parser.add_argument("--no-mail", action="store_true", help="不发送报告邮件")
    parser.add_argument("--boot", action="store_true", help="开机触发：不看时间窗，但 8~23 点才跑")
    parser.add_argument("--daemon", action="store_true", help="常驻模式：一直盯私信，直到进程被杀")
    parser.add_argument("--session-minutes", type=int, default=None, help="覆盖 [session].minutes（测试用）")
    args = parser.parse_args(argv)

    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    setup(cfg.paths.logs, cfg.log_level)

    if args.session_minutes is not None:
        cfg = dataclasses.replace(
            cfg,
            session=cfgmod.Session(
                minutes=max(0, args.session_minutes),
                dm_poll_seconds=cfg.session.dm_poll_seconds,
            ),
        )

    try:
        if args.daemon:
            return daemon(cfg, use_ai=not args.no_ai, use_say=not args.no_say, send_mail=not args.no_mail)
        return run(
            cfg,
            forced=args.force,
            use_ai=not args.no_ai,
            send_mail=not args.no_mail,
            use_say=not args.no_say,
            boot=args.boot,
        )
    except RiskControl as exc:
        guard.trip_breaker(cfg, f"风控拦截：{exc}")
        return 3
    except NotLoggedIn:
        log.error("登录态失效：跑一次 python -m bili.auth login")
        return 6
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
