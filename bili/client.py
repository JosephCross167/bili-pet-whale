"""HTTP 底座：会话、限速、重试、风控码识别。

P0 只用得到免签名接口（登录二维码、nav），wbi 签名等 P1 拉推荐流时再补进来，
但错误分类现在就要定死：412 / -352 / -799 是「当天停手」信号，不能被重试逻辑吃掉。
"""

from __future__ import annotations

import random
import time
from typing import Any

import requests

from . import quota
from .logs import get
from .wbi import WbiSigner

log = get("client")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

API = "https://api.bilibili.com"

RISK_CODES = {
    -352: "风控校验失败 (-352)",
    -412: "请求被拦截 (-412)",
    -401: "请求被拦截 (-401)",
    -799: "请求过于频繁 (-799)",
}


class BiliError(RuntimeError):
    """接口层面的失败。"""


class RiskControl(BiliError):
    """被风控盯上了：当天必须停手，写 ALERT。"""


class NotLoggedIn(BiliError):
    """cookie 缺失或已失效。"""


class NetworkProblem(BiliError):
    """网络/劫持/服务端异常。"""


class Client:
    def __init__(
        self,
        cookies: dict[str, str] | None = None,
        *,
        timeout: float = 15.0,
        min_interval: float = 0.8,
        max_interval: float = 1.8,
        seed: int | None = None,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        if cookies:
            self.session.cookies.update(cookies)
        self.timeout = timeout
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._rng = random.Random(seed)
        self._last_call = 0.0
        self._signer: WbiSigner | None = None

    # ---- cookie -----------------------------------------------------------
    def cookie_dict(self) -> dict[str, str]:
        return {c.name: c.value for c in self.session.cookies}

    @property
    def csrf(self) -> str:
        return self.session.cookies.get("bili_jct", "")

    # ---- 节流 -------------------------------------------------------------
    def _pace(self) -> None:
        wait = self._last_call + self._rng.uniform(self._min_interval, self._max_interval)
        delay = wait - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last_call = time.monotonic()

    # ---- 请求 -------------------------------------------------------------
    def get(self, url: str, params: dict[str, Any] | None = None, *, retries: int = 2):
        last_error: str = "未知错误"
        for attempt in range(retries + 1):
            quota.bump("read")
            self._pace()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}"
                if attempt == retries:
                    raise NetworkProblem(f"网络请求失败：{last_error}") from None
                time.sleep(1.0 + attempt * 2 + self._rng.uniform(0, 1))
                continue

            if resp.status_code == 412:
                raise RiskControl("HTTP 412，被风控拦截")
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                if attempt == retries:
                    raise NetworkProblem(f"服务端错误 {last_error}")
                time.sleep(1.0 + attempt * 2)
                continue
            if resp.status_code != 200:
                raise BiliError(f"HTTP {resp.status_code}")
            return resp
        raise NetworkProblem(f"重试耗尽：{last_error}")

    def get_payload(self, url: str, params: dict[str, Any] | None = None, *, retries: int = 2) -> dict:
        resp = self.get(url, params, retries=retries)
        try:
            payload = resp.json()
        except ValueError:
            # 校园网认证页、运营商劫持都会走到这里
            raise NetworkProblem("返回的不是 JSON（可能是认证页/劫持页）") from None
        code = payload.get("code")
        if code == -101:
            raise NotLoggedIn("未登录（cookie 缺失或已过期）")
        if code in RISK_CODES:
            raise RiskControl(RISK_CODES[code])
        if code != 0:
            raise BiliError(f"接口返回 code={code} message={payload.get('message')}")
        return payload


    # ---- 身份 / 签名 ------------------------------------------------------
    def nav(self) -> dict:
        """nav 的语义是「我是谁 + 拿 wbi key」，未登录时它返回 code=-101 但我们仍要 data，
        所以这里不走 get_payload 的 code 检查。"""
        resp = self.get(f"{API}/x/web-interface/nav", retries=2)
        try:
            payload = resp.json()
        except ValueError:
            raise NetworkProblem("nav 返回的不是 JSON（疑似认证页劫持）") from None
        return payload.get("data") or {}

    def signer(self) -> WbiSigner:
        if self._signer is None or self._signer.expired:
            signer = WbiSigner.from_nav(self.nav())
            if signer is None:
                raise NetworkProblem("nav 没返回 wbi key，无法签名")
            self._signer = signer
        return self._signer

    def get_wbi(self, url: str, params: dict[str, Any] | None = None, *, retries: int = 2) -> dict:
        return self.get_payload(url, self.signer().sign(params), retries=retries)


def network_ok(timeout: float = 8.0) -> tuple[bool, str]:
    """网络预检：能不能拿到一个合法的 B 站 JSON。

    刻意不要求登录状态 —— 这里只判断「网通不通、有没有被劫持」。
    """
    try:
        resp = requests.get(
            f"{API}/x/web-interface/nav",
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, f"连不上 B 站：{type(exc).__name__}"
    if resp.status_code != 200:
        return False, f"B 站返回 HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except ValueError:
        return False, "返回的不是 JSON（疑似认证页劫持）"
    if "code" not in payload:
        return False, "返回结构不对（疑似认证页劫持）"
    return True, "ok"
