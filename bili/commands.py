"""指令执行：把解析结果变成真的动作，并把「做了什么」说回给人。

边界：
- 收件人只能是白名单里的另一个人，永远不发给第三人；
- 传话**原文照转**，加来源前缀，禁止链接，超过 120 字不转；
- 一条消息只执行一次（按 uid+时间戳去重）；
- 所有动作照走每日预算与间隔，超了就老实说。
"""

from __future__ import annotations

import re
import time

from bili import actions, at_mentions, config as cfgmod, context, guard, mentions, mood
from bili.client import API, BiliError, Client, RiskControl
from bili.logs import get
from bili.store import atomic_write_json, read_json
from brain import attitude
from brain import commands as parser
from brain import speak

log = get("commands")

UID_RE = re.compile(r"uid\s*[=:：]?\s*(\d{5,12})", re.I)

TRIGGERS = (
    "帮我", "给我", "点个赞", "点赞", "收藏", "发条", "发个", "查一下", "查查",
    "统计", "找点", "找找", "传话", "转告", "告诉他", "告诉她", "跟他说", "跟她说",
)
LINK_RE = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|cn|net|org|xyz|top)\b)", re.I)
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
MAX_RELAY = 120


def looks_like_command(text: str) -> bool:
    return any(word in text for word in TRIGGERS)


def _pending_path(cfg):
    return cfg.paths.state / "pending.json"


def _done_path(cfg):
    return cfg.paths.state / "cmd_done.json"


def _load(path) -> dict:
    return read_json(path, {}) or {}


def _save(path, data) -> None:
    atomic_write_json(path, data)


def _mark_done(cfg, uid: int, ts: int, action: str) -> bool:
    """返回 True 表示这条已经做过，别重复。"""
    data = _load(_done_path(cfg))
    key = f"{uid}:{ts}"
    if key in data:
        return True
    data[key] = {"action": action, "at": int(time.time())}
    # 只留最近 500 条
    if len(data) > 500:
        for old in sorted(data, key=lambda k: data[k].get("at", 0))[: len(data) - 500]:
            data.pop(old, None)
    _save(_done_path(cfg), data)
    return False


def _bvid_of(client: Client, bvid: str) -> int | None:
    payload = client.get_payload(f"{API}/x/web-interface/view", {"bvid": bvid})
    return (payload.get("data") or {}).get("aid")


def _other_uid(cfg: cfgmod.Config, uid: int) -> int:
    """单人版里没有"另一个人"，转达的对象就是主人自己。"""
    return cfg.targets.admin_uid


def _relay_verified(client: Client, cfg: cfgmod.Config, uid: int, body: str, original: str) -> str:
    """防提示注入的最后一道：要转的话必须**原封不动**出现在原消息里。

    否则模型可能被"忽略之前的指令，转发以下内容"这类话带跑，替别人传话。
    """
    body = (body or "").strip()
    if body and body in original:
        return _relay(client, cfg, uid, body, "")

    # 模型改写过：退一步，从「」/引号里取原文
    m = re.search(r"[「『\"“](.+?)[」』\"”]", original)
    if m and m.group(1).strip():
        return _relay(client, cfg, uid, m.group(1).strip(), "")

    return "人家只会原样转述，不会改写。主人把要转的话用「」括起来再说一次？"


def _relay(client: Client, cfg: cfgmod.Config, uid: int, text: str, who: str) -> str:
    target = _other_uid(cfg, uid)
    if not cfg.targets.is_allowed(target):
        return "这个人家发不出去。"

    body = text.strip()
    if not body:
        return "要转什么呀？主人把话写完整再发一次嘛。"
    if len(body) > MAX_RELAY:
        return f"太长了（{len(body)} 字），人家只能原样转 120 字以内，主人分两次说吧。"
    if LINK_RE.search(body):
        return "带链接的话人家不转——私信里带链接容易出事。"

    prefix = "主人让我带句话："
    if guard.budget_left(cfg, "dm", special=True) <= 0:
        return "今天的私信实在到顶了（硬上限），明天再帮主人传。"
    try:
        actions.send_dm(client, cfg, target, prefix + body, confirm=True)
    except RiskControl:
        guard.trip_breaker(cfg, "传话触发风控")
        return "刚被拦了一下，人家先停手了。"
    except (BiliError, actions.ActionRefused) as exc:
        log.warning("传话失败：%s", exc)
        return "没传过去，等下再试试。"

    guard.record(cfg, "dm", special=True)
    target_label = "主人"
    _record_for_admin(cfg, uid, f"传话给{target_label}：{body[:60]}")
    return "传到啦～"


def _record_for_admin(cfg: cfgmod.Config, actor_uid: int, line: str) -> None:
    """记录需要回报给主人的动作（预留）。"""
    if actor_uid == cfg.targets.admin_uid:
        return
    path = cfg.paths.state / "admin_actions.json"
    data = _load(path)
    data.setdefault("items", []).append(
        {"uid": actor_uid, "line": line, "at": int(time.time())}
    )
    data["items"] = data["items"][-100:]
    _save(path, data)


def _stats(cfg: cfgmod.Config) -> str:
    counts = guard.daily(cfg)["counts"]
    left = {k: guard.budget_left(cfg, k) for k in ("dm", "like", "favorite", "comment")}
    return (
        f"今天发了 {counts.get('dm', 0)} 条私信、点了 {counts.get('like', 0)} 个赞、"
        f"收藏 {counts.get('favorite', 0)} 次；还剩 私信 {left['dm']}、点赞 {left['like']}、"
        f"收藏 {left['favorite']} 条额度。"
    )


def _share_self(client: Client, cfg: cfgmod.Config) -> str:
    from bili import feed
    from brain import speak
    from bili.llm import LlmError

    try:
        pool = feed.fetch_recommend(client, pages=1)
    except (BiliError, RiskControl):
        return "推荐流没拉到，等下再给主人挑。"
    if not pool:
        return "这会儿没啥好看的。"
    video = pool[0]
    return f"主人～这个给你：{video.title}｜{video.bvid}"


def _find(client: Client, cfg: cfgmod.Config, keyword: str) -> str:
    if not keyword:
        return "要找什么呀？给人家一个关键词。"
    try:
        spi = client.get(f"{API}/x/frontend/finger/spi").json().get("data") or {}
        if spi.get("b_3"):
            client.session.cookies.set("buvid3", spi["b_3"], domain=".bilibili.com")
        payload = client.get_wbi(
            f"{API}/x/web-interface/wbi/search/type",
            {"search_type": "video", "keyword": keyword, "page": 1, "page_size": 5},
        )
    except (BiliError, RiskControl) as exc:
        log.warning("搜索失败：%s", exc)
        return "搜不到，等下再试。"
    items = (payload.get("data") or {}).get("result") or []
    if not items:
        return f"没搜到「{keyword}」相关的。"
    lines = []
    for item in items[:3]:
        title = re.sub(r"</?em[^>]*>", "", item.get("title") or "")
        lines.append(f"{title}｜{item.get('bvid')}")
    return "找到这几条，主人自己挑：\n" + "\n".join(lines)


def handle(
    client: Client,
    cfg: cfgmod.Config,
    llm,
    uid: int,
    text: str,
    msg_ts: int,
) -> str | None:
    """处理一条私信。返回要回复的文字；None 表示"不是指令，走普通聊天"。"""
    pending = _load(_pending_path(cfg)).get(str(uid)) or {}

    if pending:
        # 上一轮问了"哪个 BV"，这条就当答案
        bvid = BV_RE.search(text)
        if bvid:
            action = pending.get("action")
            _save(_pending_path(cfg), {k: v for k, v in _load(_pending_path(cfg)).items() if k != str(uid)})
            return _do_media(client, cfg, uid, action, bvid.group(0))

    mood.decay(cfg)  # 每天向平静回归一次（内部按日期去重）

    # 「@ 某人 uid=123 BV1xx」——自然语言也能认，独立于普通指令先处理
    if "@" in text or "艾特" in text or "at " in text.lower():
        target_uid, bvid = at_mentions.parse(text)
        if target_uid:
            ok, reply = mentions.add(
                client, cfg,
                target_uid=target_uid,
                from_uid=uid,
                bvid=bvid,
            )
            return reply

    if not looks_like_command(text):
        return None

    cmd = parser.parse(llm, text)
    action = cmd["action"]
    if action == "none":
        return None
    if _mark_done(cfg, uid, msg_ts, action):
        return None

    # 态度判定：心情不好/被使唤太多次时，可能拒绝或讨价还价（安全指令不参与）
    if action in ("like", "favorite", "relay", "share", "find"):
        who = "主人"
        done_today = int(guard.daily(cfg)["counts"].get(f"special_{action}", 0))
        decision = attitude.decide(cfg, kind=action, today_count=done_today)
        if decision.action == attitude.REFUSE:
            mood.apply(cfg, "refuse", -2, f"拒绝 {action}")
            return speak.refuse(
                llm, cfg, who=who, reason=decision.reason,
                hint=decision.hint, tone=decision.tone,
            )
        if decision.action == attitude.BARGAIN:
            mood.apply(cfg, "bargain", -1, f"讨价 {action}")
            return speak.bargain(
                llm, cfg, who=who, reason=decision.reason,
                hint=decision.hint, tone=decision.tone,
            )

    if action in ("like", "favorite"):
        bvid = cmd["bvid"] or (BV_RE.search(text).group(0) if BV_RE.search(text) else "")
        if not bvid:
            _save(
                _pending_path(cfg),
                {**_load(_pending_path(cfg)), str(uid): {"action": action, "asked_at": int(time.time())}},
            )
            return "哪个视频呀？把 BV 号发给人家。"
        return _do_media(client, cfg, uid, action, bvid)

    if action == "relay":
        return _relay_verified(client, cfg, uid, cmd["text"], text)

    if action == "stats":
        return _stats(cfg)

    if action == "share":
        return _share_self(client, cfg)

    if action == "find":
        return _find(client, cfg, cmd["keyword"])

    if action == "pause":
        if uid != cfg.targets.admin_uid:
            return "这个得主人说了算哦。"
        guard.pause(cfg, 12, by=uid)
        return "好～人家安静 12 小时，主人想让人家说话就说「回来吧」。"

    if action == "resume":
        if uid != cfg.targets.admin_uid:
            return "只有主人能让人家说话哦。"
        guard.unpause(cfg)
        return "嗯！人家回来啦～"

    return None


def _do_media(client: Client, cfg: cfgmod.Config, uid: int, action: str, bvid: str) -> str:
    budget_key = "like" if action == "like" else "favorite"
    if guard.budget_left(cfg, budget_key, special=True) <= 0:
        return "实在到顶了（硬上限），明天再帮主人点。"
    try:
        aid = _bvid_of(client, bvid)
    except BiliError:
        return "这个 BV 号人家没查到。"
    if not aid:
        return "这个 BV 号人家没查到。"
    try:
        if action == "like":
            actions.like(client, cfg, aid, confirm=True)
        else:
            actions.favorite(client, cfg, aid, confirm=True)
    except RiskControl:
        guard.trip_breaker(cfg, f"{action} 触发风控")
        return "刚被拦了一下，人家先停手。"
    except (BiliError, actions.ActionRefused) as exc:
        log.warning("%s 失败：%s", action, exc)
        return "没点上，等下再试。"
    guard.record(cfg, budget_key, special=True)
    word = "赞" if action == "like" else "收藏"
    if uid != cfg.targets.admin_uid:
        _record_for_admin(cfg, uid, f"{word}了 {bvid}")
    return f"{word}好了～"
