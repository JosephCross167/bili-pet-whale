"""私信：读全部会话，写只发白名单。

两个已知的硬限制（都是 B 站定的，不是我们的选择）：
1. **对方没回复或没关注你之前，一个会话最多发 1 条消息**——界面上会插一条系统提示，
   msg_type=18。所以「撒娇聊天」会被这条卡住，代码必须识别并把状态写进报告。
2. 会话列表里混着大量系统号（客服、通知），它们的 msg_type 是 10~13、18，
   只有 1/2/7（文字/图片/分享）才算「真有人说话」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from . import config as cfgmod
from .client import Client, BiliError
from .logs import get
from .store import atomic_write_json, read_json

log = get("dm")

SESSIONS = "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
MESSAGES = "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs"

HUMAN_MSG_TYPES = {1, 2, 7}
LIMIT_MSG_TYPE = 18
MAX_TEXT = 200
# 第一次跑的时候别把几百条历史消息全倒出来，只看最近这么多天
LOOKBACK_DAYS = 7


@dataclass
class Incoming:
    talker_id: int
    sender_uid: int
    msg_type: int
    text: str
    ts: int

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%m-%d %H:%M")


@dataclass
class Digest:
    from_whitelist: list[Incoming] = field(default_factory=list)
    from_strangers: list[Incoming] = field(default_factory=list)
    limit_notice: bool = False
    sessions: int = 0
    ok: bool = True  # False = 这次轮询失败（网络/接口），用来做失败退避

    @property
    def empty(self) -> bool:
        return not (self.from_whitelist or self.from_strangers or self.limit_notice)


def _state_path(cfg: cfgmod.Config):
    return cfg.paths.state / "dm.json"


def _load_seen(cfg: cfgmod.Config) -> dict[str, int]:
    data = read_json(_state_path(cfg), {}) or {}
    return {str(k): int(v) for k, v in (data.get("seen") or {}).items()}


def _save_seen(cfg: cfgmod.Config, seen: dict[str, int]) -> None:
    atomic_write_json(_state_path(cfg), {"seen": seen})


def _text_of(msg: dict) -> str:
    raw = msg.get("content")
    try:
        data = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    text = data.get("content") or ""
    return str(text).strip()[:MAX_TEXT]


def sessions(client: Client, *, count: int = 50) -> list[dict]:
    payload = client.get_payload(
        SESSIONS,
        {"session_type": 1, "group_id": 0, "count": count, "build": 0, "mobi_app": "web"},
    )
    return (payload.get("data") or {}).get("session_list") or []


def messages(client: Client, talker_id: int, *, size: int = 20) -> list[dict]:
    payload = client.get_payload(
        MESSAGES,
        {"talker_id": talker_id, "session_type": 1, "size": size, "build": 0, "mobi_app": "web"},
    )
    return (payload.get("data") or {}).get("messages") or []


MAX_SESSIONS_PER_POLL = 8
MAX_SESSION_REQUESTS = 12


def collect(
    client: Client,
    cfg: cfgmod.Config,
    *,
    self_uid: int | None = None,
    check_limit: bool = True,
) -> Digest:
    """拉一次私信，返回「这次新出现的」内容。只读，不回复，不发。

    每次轮询最多打开 MAX_SESSIONS_PER_POLL 个会话、最多发 MAX_SESSION_REQUESTS 个请求。
    被刷屏或长期没清理时，宁可少读几条也不要把自己刷成「机器人在高频拉私信」。

    check_limit=False 时省掉「1 条上限」提示的检查（那要额外 2 个请求）——
    高频轮询下这条不需要每轮都看，隔一段时间确认一次就够。
    """
    seen = _load_seen(cfg)
    digest = Digest()
    self_uid = self_uid or cfg.account.uid
    floor = int(datetime.now().timestamp()) - LOOKBACK_DAYS * 86400
    budget = MAX_SESSION_REQUESTS

    try:
        all_sessions = sessions(client)
    except BiliError as exc:
        log.warning("拉会话列表失败：%s", exc)
        digest.ok = False
        return digest

    digest.sessions = len(all_sessions)
    opened = 0
    opened_talkers: set[int] = set()
    for session in all_sessions:
        talker = session.get("talker_id")
        last = session.get("last_msg") or {}
        if not isinstance(talker, int):
            continue
        last_ts = int(last.get("timestamp") or 0)
        if last_ts and last_ts <= seen.get(str(talker), 0):
            continue
        if last_ts and last_ts < floor:
            continue
        if opened >= MAX_SESSIONS_PER_POLL or budget <= 0:
            log.info("本轮私信会话已达上限（%s 个 / %s 请求），其余留到下一轮", opened, MAX_SESSION_REQUESTS)
            break
        opened += 1
        budget -= 1
        opened_talkers.add(talker)

        try:
            msgs = messages(client, talker)
        except BiliError as exc:
            log.warning("拉会话 %s 失败：%s", talker, exc)
            continue

        newest = last_ts
        last_incoming = 0
        notice_ts = 0
        for msg in msgs:
            ts = int(msg.get("timestamp") or 0)
            msg_type = int(msg.get("msg_type") or 0)
            newest = max(newest, ts)
            sender = int(msg.get("sender_uid") or 0)

            if msg_type == LIMIT_MSG_TYPE and cfg.targets.is_allowed(talker):
                notice_ts = max(notice_ts, ts)
                continue
            if sender and sender == self_uid:
                continue
            if msg_type in HUMAN_MSG_TYPES:
                last_incoming = max(last_incoming, ts)

            if ts <= seen.get(str(talker), 0):
                continue
            if msg_type not in HUMAN_MSG_TYPES:
                continue
            item = Incoming(
                talker_id=talker,
                sender_uid=sender or talker,
                msg_type=msg_type,
                text=_text_of(msg),
                ts=ts,
            )
            if cfg.targets.is_allowed(talker):
                digest.from_whitelist.append(item)
            else:
                digest.from_strangers.append(item)

        # 只有「提示比对方最近一条消息还新」才算限制仍在生效。
        # 否则互相关注/回复之后，历史里的那条提示会被误报成"还在限制"。
        if notice_ts and notice_ts > last_incoming:
            digest.limit_notice = True

        if newest:
            seen[str(talker)] = newest

    # 白名单两个人的会话即使没有新消息，也要判断限制是否仍然生效。
    # 上一段已经打开过的就不重复请求——高频轮询时这是每轮固定 2 次开销。
    if check_limit:
        for uid in cfg.targets.uids:
            if uid in opened_talkers or budget <= 0:
                continue
            budget -= 1
            try:
                msgs = messages(client, uid, size=10)
            except BiliError:
                continue
            notices = [int(m.get("timestamp") or 0) for m in msgs
                       if int(m.get("msg_type") or 0) == LIMIT_MSG_TYPE]
            incoming = [
                int(m.get("timestamp") or 0)
                for m in msgs
                if int(m.get("msg_type") or 0) in HUMAN_MSG_TYPES
                and int(m.get("sender_uid") or 0) not in (0, self_uid)
            ]
            if notices and max(notices) > max(incoming or [0]):
                digest.limit_notice = True

    # 没有任何变化就不写盘：45 秒一轮的话，否则一天要白写近 2000 次 state
    if digest.from_whitelist or digest.from_strangers or digest.limit_notice or opened:
        _save_seen(cfg, seen)
    log.info(
        "私信：%s 个会话，白名单新消息 %s 条，陌生人新消息 %s 条%s",
        digest.sessions,
        len(digest.from_whitelist),
        len(digest.from_strangers),
        "，有「1 条上限」提示" if digest.limit_notice else "",
    )
    return digest
