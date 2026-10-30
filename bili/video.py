"""视频详情 / 评论采样 / 字幕。全部只读。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .client import API, BiliError, Client
from .logs import get

log = get("video")

# 常用分区名（接口只给 tid，不给名字；内置一份，够覆盖绝大多数推荐流）
ZONE_NAMES = {
    1: "动画", 3: "音乐", 4: "游戏", 5: "娱乐", 11: "电视剧", 13: "番剧",
    17: "单机游戏", 22: "手机游戏", 23: "网络游戏", 26: "音游", 27: "格斗游戏",
    28: "射击游戏", 29: "策略游戏", 30: "单机游戏", 31: "赛车游戏", 32: "体育游戏",
    33: "音乐游戏", 36: "科技", 47: "虚拟UP主", 48: "科学科普", 65: "网络游戏",
    71: "综艺", 75: "动物圈", 76: "美食", 85: "运动", 86: "汽车", 95: "数码",
    119: "鬼畜", 123: "时尚", 124: "舞蹈", 126: "手工", 127: "绘画", 128: "日常",
    129: "生活", 130: "出行", 138: "搞笑", 145: "汽车", 160: "生活", 161: "手工",
    163: "演讲", 167: "科普", 171: "赛事", 173: "财经", 174: "职场", 175: "情感",
    176: "家居", 181: "影视", 183: "影视杂谈", 186: "母婴", 187: "健康", 188: "数码",
    191: "资讯", 199: "热点", 201: "科学科普", 202: "社会", 203: "综合", 207: "科技",
    208: "机械", 209: "计算机技术", 210: "编程", 211: "美食", 212: "美食制作",
    217: "动物圈", 218: "喵星人", 219: "汪星人", 220: "大熊猫", 221: "野生动物",
    222: "爬宠", 223: "汽车", 234: "运动", 235: "篮球", 236: "足球", 237: "电竞",
}


@dataclass
class Video:
    bvid: str
    title: str
    up_mid: int = 0
    up_name: str = ""
    duration: int = 0
    pubdate: int = 0
    view: int = 0
    like: int = 0
    reply: int = 0
    aid: int | None = None
    cid: int | None = None
    tid: int | None = None
    tname: str = ""
    desc: str = ""
    rcmd_reason: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}"

    @property
    def minutes(self) -> float:
        return round(self.duration / 60, 1)

    @property
    def like_rate(self) -> float:
        return self.like / self.view if self.view else 0.0

    @property
    def zone_label(self) -> str:
        """view 接口常常不返回 tname，用内置 tid 表兜底。"""
        if self.tname:
            return self.tname
        if self.tid:
            return ZONE_NAMES.get(int(self.tid), f"分区#{self.tid}")
        return "未知分区"

    def brief(self) -> str:
        return (
            f"{self.bvid} | {self.title} | UP:{self.up_name} | {self.minutes}分钟 | "
            f"播放{self.view} 赞{self.like} | {self.tname or '未知分区'}"
        )


def detail(client: Client, bvid: str) -> Video:
    """补齐分区、简介这类推荐流里没有的字段。失败时抛 BiliError，由调用方决定跳过。"""
    # view/detail 才会带 Tags（view 接口现在不带 tag，也不带 tname）
    payload = client.get_payload(f"{API}/x/web-interface/view/detail", {"bvid": bvid})
    outer = payload.get("data") or {}
    data = outer.get("View") or (outer if outer.get("title") else {})
    tags = [
        str(item.get("tag_name"))
        for item in (outer.get("Tags") or [])
        if item.get("tag_name")
    ]
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    return Video(
        bvid=bvid,
        title=data.get("title") or "",
        aid=data.get("aid"),
        cid=data.get("cid"),
        up_mid=owner.get("mid") or 0,
        up_name=owner.get("name") or "",
        duration=int(data.get("duration") or 0),
        pubdate=int(data.get("pubdate") or 0),
        view=int(stat.get("view") or 0),
        like=int(stat.get("like") or 0),
        reply=int(stat.get("reply") or 0),
        tid=data.get("tid"),
        tname=data.get("tname") or "",
        desc=(data.get("desc") or "")[:300],
        tags=tags,
    )


def top_comments(client: Client, aid: int, *, count: int = 10) -> list[str]:
    """取热度最高的评论正文，用来判断「这个视频下面的人在聊什么」。"""
    try:
        payload = client.get_payload(
            f"{API}/x/v2/reply",
            {"type": 1, "oid": aid, "sort": 2, "ps": count, "pn": 1},
        )
    except BiliError as exc:
        log.warning("取评论失败 %s：%s", aid, exc)
        return []
    replies = (payload.get("data") or {}).get("replies") or []
    out = []
    for item in replies:
        message = ((item.get("content") or {}).get("message") or "").strip()
        if message:
            out.append(message)
    return out


def subtitle_text(client: Client, aid: int, cid: int, *, limit: int = 1200) -> str:
    """字幕多数要登录才有，取不到就返回空串 —— P1 不依赖它。"""
    try:
        payload = client.get_payload(f"{API}/x/player/v2", {"aid": aid, "cid": cid})
    except BiliError:
        return ""
    subs = (((payload.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
    if not subs:
        return ""
    url = subs[0].get("subtitle_url") or ""
    if url.startswith("//"):
        url = "https:" + url
    if not url:
        return ""
    try:
        body = client.session.get(url, timeout=client.timeout).text
        payload_json = json.loads(body)
    except Exception:  # noqa: BLE001 - 字幕是加分项，任何一种失败都不该影响主流程
        return ""
    lines = [seg.get("content", "") for seg in payload_json.get("body") or []]
    return "".join(lines)[:limit]


def transcript(client: Client, bvid: str, *, limit: int = 1500) -> str:
    """取视频内容文本。

    优先用 AI 总结接口（/x/web-interface/view/conclusion/get）：
    它返回 summary 或带时间戳的字幕片段，比 player/v2 的字幕稳定得多。
    实测约一半视频有字幕，其余返回空字符串。
    """
    try:
        info = client.get_payload(f"{API}/x/web-interface/view", {"bvid": bvid}).get("data") or {}
        cid = info.get("cid")
        up_mid = (info.get("owner") or {}).get("mid")
        payload = client.get_wbi(
            f"{API}/x/web-interface/view/conclusion/get",
            {"bvid": bvid, "cid": cid, "up_mid": up_mid},
        )
    except BiliError as exc:
        log.warning("取内容失败 %s：%s", bvid, exc)
        return ""

    result = (payload.get("data") or {}).get("model_result") or {}
    summary = (result.get("summary") or "").strip()
    if summary:
        return summary[:limit]

    pieces = []
    for part in result.get("subtitle") or []:
        for seg in part.get("part_subtitle") or []:
            content = (seg.get("content") or "").strip()
            if content:
                pieces.append(content)
    return " ".join(pieces)[:limit]
