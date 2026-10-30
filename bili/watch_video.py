"""真的「看」一条视频：用本机 Edge 无头打开、压到最低画质、静音播放若干秒。

为什么要有：B 站评论/观看记录验的是「你是不是在播放器里看过」——那些 dm_img_* 指纹
只能由真实播放器产生。伪造它们等于刷量（红线），真播则只是「这个号看了 45 秒」。

流量控制：用 page.route 把播放地址请求里的 qn 改写成 16（360P），
这样即使播放器想拉 1080P，拿到的也只有低码率流。
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

# 沙箱不让写系统临时目录，Playwright 要在这里建 artifacts 目录
PW_TMP = pathlib.Path(__file__).resolve().parents[1] / "state" / "pw-tmp"
PW_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(PW_TMP)

from . import config as cfgmod
from .logs import get
from .store import read_json

log = get("watch")

LOW_QUALITY = 16  # 360P


@dataclass
class WatchResult:
    ok: bool
    played_seconds: float = 0.0
    heartbeats: int = 0
    title: str = ""
    reason: str = ""


def _cookies(cfg: cfgmod.Config) -> list[dict]:
    saved = read_json(cfg.paths.state / "cookies.json", {}) or {}
    return [
        {"name": name, "value": str(value), "domain": ".bilibili.com", "path": "/"}
        for name, value in (saved.get("cookies") or {}).items()
    ]


def watch(cfg: cfgmod.Config, bvid: str, *, seconds: int = 45) -> WatchResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return WatchResult(False, reason="playwright 未安装")

    heartbeats: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel="msedge",
                headless=True,
                args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                locale="zh-CN",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
                ),
            )
            context.add_cookies(_cookies(cfg))

            # 把 playurl 请求的清晰度压到最低，省流量
            def _downgrade(route, request):
                url = request.url
                if "playurl" in url and "qn=" in url:
                    import re

                    url = re.sub(r"qn=\d+", f"qn={LOW_QUALITY}", url)
                route.continue_(url=url)

            context.route("**/playurl**", _downgrade)

            page = context.new_page()
            page.on(
                "request",
                lambda r: heartbeats.append(r.url) if "heartbeat" in r.url else None,
            )
            page.goto(
                f"https://www.bilibili.com/video/{bvid}/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(3000)
            title = page.title()[:80]

            has_video = page.evaluate("() => !!document.querySelector('video')")
            if not has_video:
                browser.close()
                return WatchResult(False, title=title, reason="页面上没有 video 元素")

            page.evaluate(
                "() => { const v = document.querySelector('video'); v.muted = true; v.play(); }"
            )
            waited = 0.0
            while waited < seconds:
                page.wait_for_timeout(2000)
                waited += 2
                current = page.evaluate(
                    "() => { const v = document.querySelector('video'); return v ? v.currentTime : 0; }"
                )
                if current >= seconds:
                    break

            played = float(
                page.evaluate(
                    "() => { const v = document.querySelector('video'); return v ? v.currentTime : 0; }"
                )
                or 0
            )
            browser.close()
            log.info("看过 %s：播放 %.1f 秒，心跳 %s 次", bvid, played, len(heartbeats))
            return WatchResult(True, played_seconds=played, heartbeats=len(heartbeats), title=title)
    except Exception as exc:  # noqa: BLE001 - 浏览器问题不该毁掉一整轮
        log.warning("看视频失败 %s：%s", bvid, type(exc).__name__)
        return WatchResult(False, reason=f"{type(exc).__name__}")
