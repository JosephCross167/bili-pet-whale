"""同义词聚类：把「本质是同一个 IP」的 tag 合成一组，打分时只算一次。

为什么需要：一条视频可能同时带同一个兴趣的好几种叫法，
现在每个都加一次分，等于**标签打得越全分越高**——那是命名习惯，不是口味。

聚类之后：一条视频命中同一个 IP 的任意别名，**该 IP 只计一次**。

别名表来源：
  1. 手工维护（默认表在下面，够用）
  2. 自动检测：下次重建画像时会记录 tag 共现次数，共现率 > 70% 的自动归为一组
  3. 人工覆盖：state/aliases.json 可以改/加
"""

from __future__ import annotations

import json

from . import config as cfgmod
from .logs import get
from .store import read_json

log = get("aliases")

# 默认别名表：值是一个组的成员，第一个是组的名字
DEFAULT_CLUSTERS = {
    # 示例：把同一个兴趣的不同叫法归成一组，打分时算作一个 tag。
    # 想用自己的，照着加就行；留空也能跑，只是不合并同义词。
    "示例游戏": ["示例游戏", "example game", "EG"],
    "示例企划": ["示例企划", "example project"],
}



def _path(cfg: cfgmod.Config):
    return cfg.paths.state / "aliases.json"


def load(cfg: cfgmod.Config) -> dict[str, str]:
    """返回 tag → 组名 的映射。state/aliases.json 里的会覆盖默认表。"""
    clusters = dict(DEFAULT_CLUSTERS)
    custom = read_json(_path(cfg), {}) or {}
    if isinstance(custom.get("clusters"), dict):
        clusters.update({k: v for k, v in custom["clusters"].items() if isinstance(v, list)})
    elif isinstance(custom, dict):
        clusters.update({k: v for k, v in custom.items() if isinstance(v, list)})

    mapping: dict[str, str] = {}
    for name, members in clusters.items():
        for member in members:
            mapping[str(member).strip().lower()] = name
    return mapping


def cluster_of(cfg: cfgmod.Config, tag: str) -> str | None:
    return load(cfg).get(str(tag).strip().lower())


def group(weights: dict[str, float], mapping: dict[str, str]) -> dict[str, float]:
    """把 tag 权重按组归并：组内求和，未归类的每个 tag 自己一组。"""
    grouped: dict[str, float] = {}
    for tag, weight in weights.items():
        name = mapping.get(str(tag).strip().lower(), tag)
        grouped[name] = grouped.get(name, 0.0) + float(weight)
    return grouped
