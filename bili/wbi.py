"""wbi 签名。

B 站 2023 年起对多数 web 接口要求 w_rid + wts：把参数按 key 排序拼成查询串，
末尾接一段从 nav 返回的 img_key/sub_key 里按固定表打乱取出的 mixin key，再 md5。

这里刻意自己实现（约 40 行）：算法一变就要能当场改，第三方 SDK 只会把变化藏起来。
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import quote, urlencode, urlparse

_MIXIN_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
)


def _key_from_url(url: str) -> str:
    """https://i0.hdslb.com/bfs/wbi/<key>.png → <key>"""
    return urlparse(url).path.rsplit("/", 1)[-1].split(".", 1)[0]


def mixin_key(img_url: str, sub_url: str) -> str:
    raw = _key_from_url(img_url) + _key_from_url(sub_url)
    return "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]


class WbiSigner:
    def __init__(self, img_url: str, sub_url: str, *, ttl: int = 6 * 3600) -> None:
        self._img = img_url
        self._sub = sub_url
        self._key = mixin_key(img_url, sub_url)
        self._born = time.time()
        self._ttl = ttl

    @classmethod
    def from_nav(cls, nav_data: dict) -> "WbiSigner | None":
        wbi = (nav_data or {}).get("wbi_img") or {}
        img, sub = wbi.get("img_url"), wbi.get("sub_url")
        if not img or not sub:
            return None
        return cls(img, sub)

    @property
    def expired(self) -> bool:
        return time.time() - self._born > self._ttl

    def sign(self, params: dict | None = None, *, wts: int | None = None) -> dict:
        signed = {k: v for k, v in (params or {}).items() if v is not None}
        signed["wts"] = wts if wts is not None else int(time.time())
        # quote_plus 与 B 站前端一致；value 里带空格/中文时差异会直接导致 -352
        query = urlencode(
            {k: signed[k] for k in sorted(signed)},
            quote_via=quote,
            safe="",
        )
        signed["w_rid"] = hashlib.md5((query + self._key).encode()).hexdigest()
        return signed
