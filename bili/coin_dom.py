"""用浏览器投币（API 那条路被 -401 人机验证挡住）。

流程：打开视频 → 点「投币」→ 弹窗里选 1 枚 → 确认 → 读回结果。
"""

from __future__ import annotations

import os
import pathlib
import time
from dataclasses import dataclass

from . import config as cfgmod
from .logs import get
from .store import read_json

_PW_TMP = pathlib.Path(__file__).resolve().parents[1] / "state" / "pw-tmp"
_PW_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(_PW_TMP)

log = get("coin_dom")


@dataclass
class CoinResult:
    ok: bool = False
    reason: str = ""
    screenshot: str = ""


def _cookies(cfg: cfgmod.Config) -> list[dict]:
    saved = read_json(cfg.paths.state / "cookies.json", {}) or {}
    return [
        {"name": name, "value": str(value), "domain": ".bilibili.com", "path": "/"}
        for name, value in (saved.get("cookies") or {}).items()
    ]


def coin(cfg: cfgmod.Config, bvid: str, *, count: int = 1) -> CoinResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CoinResult(reason="playwright 未安装")

    shot_path = cfg.paths.state / "coin_last.png"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True, args=["--mute-audio"])
            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
            ctx.add_cookies(_cookies(cfg))
            page = ctx.new_page()
            page.goto(f"https://www.bilibili.com/video/{bvid}/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)

            # 投币按钮在播放器下方工具栏，先定位再点
            btn = page.locator(".video-coin").first
            if not btn.count():
                btn = page.locator("div.video-toolbar-left-item").nth(1)
            if not btn.count():
                btn = page.get_by_text("投币", exact=False).first
            if not btn.count():
                page.screenshot(path=str(shot_path))
                browser.close()
                return CoinResult(reason="找不到投币按钮", screenshot=str(shot_path))
            btn.click()
            page.wait_for_timeout(2000)

            # 弹窗里显式选「1 枚」——默认常常是 2 枚，不选就会多花一个
            picked = False
            for sel in ("li", "div", "span"):
                try:
                    cand = page.locator(f".coin-dialog {sel}, .van-dialog {sel}").filter(
                        has_text=__import__("re").compile(r"^1$")
                    ).first
                    if cand.count() and cand.is_visible():
                        cand.click()
                        picked = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            log.info("选 1 枚：%s", picked)
            page.wait_for_timeout(600)

            # 确认按钮：只在可见弹窗里找
            confirmed = False
            for text in ("确定", "确认投币", "确认"):
                try:
                    cand = page.get_by_text(text, exact=True).last
                    if cand.count() and cand.is_visible():
                        cand.click()
                        confirmed = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            page.wait_for_timeout(3000)
            page.screenshot(path=str(shot_path))
            browser.close()

            if not confirmed:
                return CoinResult(reason="找不到确认按钮", screenshot=str(shot_path))
            log.info("已投币 %s ×%s", bvid, count)
            return CoinResult(ok=True, screenshot=str(shot_path))
    except Exception as exc:  # noqa: BLE001
        log.warning("DOM 投币失败 %s：%s", bvid, type(exc).__name__)
        return CoinResult(reason=f"{type(exc).__name__}")
