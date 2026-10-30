"""用口味画像给视频打分（含同义词聚类）。

规则：
  1. 画像里的 tag 先按别名表聚类（同一个兴趣的不同叫法算一组）
  2. 一条视频命中同一组里的任意别名，**该组只计一次**
  3. 组权重 = 组内各 tag 权重之和
  4. 人工调整（state/taste_overrides.json）作用在**组**上：删除 / 乘系数 / 设定百分比

这样不同 IP 之间可比，也不会因为「标签打得全」而虚高。
"""

from __future__ import annotations

import re

from bili import aliases, taste
from bili.video import Video

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _split(text: str) -> list[str]:
    return [_norm(word) for word in WORD_RE.findall(str(text or ""))]


def video_tokens(video: Video) -> list[str]:
    """把视频拆成词条：标签、分区、UP 名。（标题不参与打分，bigram 噪声太大）"""
    tokens: list[str] = []
    for tag in getattr(video, "tags", None) or []:
        tokens.append(_norm(tag))
    tokens += _split(getattr(video, "tname", "") or "")
    tokens += _split(getattr(video, "up_name", "") or "")

    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _overrides(cfg, uid: int | None) -> dict:
    """人工调整：state/taste_overrides.json 里按 uid 存，和自动生成的画像分开。"""
    if cfg is None or uid is None:
        return {}
    from bili.store import read_json

    data = read_json(cfg.paths.state / "taste_overrides.json", {}) or {}
    return data.get(str(uid)) or {}


def cluster_weights(profile: dict, mapping: dict[str, str], overrides: dict | None = None) -> dict[str, float]:
    """把 tag 权重聚成组权重，再套人工调整。"""
    weights = profile.get("tokens") or {}
    # 先按小写合并：画像里可能同时有同一个 tag 的多种大小写i，
    # 直接塞进 dict 会互相覆盖（不是相加），所以这里显式累加。
    merged: dict[str, float] = {}
    for tag, weight in weights.items():
        key = _norm(tag)
        merged[key] = merged.get(key, 0.0) + float(weight)
    grouped = aliases.group(merged, mapping)

    overrides = overrides or {}
    for name in overrides.get("remove") or []:
        grouped.pop(name, None)
    for name, factor in (overrides.get("scale") or {}).items():
        if name in grouped:
            grouped[name] *= float(factor)
    for name, delta in (overrides.get("add_percent") or {}).items():
        if name in grouped:
            grouped[name] = max(0.0, grouped[name] + float(delta) / 100.0)
    for name, percent in (overrides.get("set_percent") or {}).items():
        grouped[name] = float(percent) / 100.0

    return grouped


def score(profile: dict, video: Video, *, cfg=None, uid: int | None = None) -> float:
    """匹配分（×100 的百分比尺度）。画像缺失时返回 0。"""
    if not (profile.get("tokens") or {}):
        return 0.0

    mapping = aliases.load(cfg) if cfg is not None else {}
    grouped = cluster_weights(profile, mapping, _overrides(cfg, uid))

    hit = 0.0
    counted: set[str] = set()
    for token in video_tokens(video):
        name = mapping.get(token.lower(), token)
        if name in counted or name not in grouped:
            continue
        counted.add(name)
        hit += grouped[name]
    return round(hit * 100, 2)


def matched(profile: dict, video: Video, *, cfg=None, uid: int | None = None, limit: int = 6) -> list[str]:
    """命中了哪些组（按权重从高到低），日报里用来解释「为什么推荐这条」。"""
    mapping = aliases.load(cfg) if cfg is not None else {}
    grouped = cluster_weights(profile, mapping, _overrides(cfg, uid))

    hits: list[tuple[str, float]] = []
    counted: set[str] = set()
    for token in video_tokens(video):
        name = mapping.get(token.lower(), token)
        if name in counted or name not in grouped:
            continue
        counted.add(name)
        hits.append((name, grouped[name]))
    hits.sort(key=lambda item: -item[1])
    return [name for name, _ in hits[:limit]]


# 这些组太泛，不能用来判断「这是谁的口味」——否则一条 AI 视频带个「搞笑」就算你们的了
GENERIC_GROUPS = {
    "可爱", "搞笑", "动画", "二次元", "娱乐", "抽象", "必剪创作",
    "动画短片", "动画种草激励计划", "原创", "翻唱",
}


def specific_score(profile: dict, video: Video, *, cfg, uid: int) -> float:
    """只统计「有辨识度」的兴趣组，用于判断这条视频算谁的口味。"""
    mapping = aliases.load(cfg)
    grouped = cluster_weights(profile, mapping, _overrides(cfg, uid))

    hit = 0.0
    counted: set[str] = set()
    for token in video_tokens(video):
        name = mapping.get(token.lower(), token)
        if name in counted or name in GENERIC_GROUPS or name not in grouped:
            continue
        counted.add(name)
        hit += grouped[name]
    return round(hit * 100, 2)


def classify(cfg, video) -> dict:
    """这条视频算谁的口味。

    返回 {"kind": "ours"|"self"|"neutral", "admin_score": x, "self_score": y, "match": 最大那个}
    ours = 命中管理员画像（>= min_score_ours）→ 只 @，不做点赞收藏评论
    its_own = 都不像 → 当它自己刷到的，正常互动
    """
    from bili import taste

    pa = taste.load(cfg, cfg.targets.admin_uid)
    sa = specific_score(pa, video, cfg=cfg, uid=cfg.targets.admin_uid)
    self_m = self_score(cfg, video)

    if sa >= cfg.taste.min_score_ours:
        kind = "ours"
    elif self_m >= cfg.taste.min_score_self:
        kind = "self"
    else:
        kind = "neutral"

    return {
        "kind": kind,
        "admin_score": sa,
        "match": sa if kind == "ours" else 0.0,
        "self_score": self_m,
    }


def self_score(cfg, video) -> float:
    """它自己觉得多有意思（DeepSeek/大肥鱼/整活这类，手工设的）。"""
    from bili.store import read_json

    data = read_json(cfg.paths.state / "self_taste.json", {}) or {}
    tokens = data.get("tokens") or {}
    if not tokens:
        return 0.0
    weights = {str(k).lower(): float(v) / 100.0 for k, v in tokens.items()}
    mapping = aliases.load(cfg)

    hit = 0.0
    counted: set[str] = set()
    for token in video_tokens(video):
        name = mapping.get(token.lower(), token)
        if name in counted or name not in weights:
            continue
        counted.add(name)
        hit += weights[name]
    return round(hit * 100, 2)
