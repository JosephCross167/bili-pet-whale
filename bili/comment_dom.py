"""用浏览器（本机 Edge）发评论。

为什么不用 API：B 站评论请求自带 `dm_img_*`（播放器画面指纹），客户端拼不出来，
服务端会把它置为「仅自己可见」。走真实前端，这些参数由 B 站自己的 JS 生成。

流程：打开视频 → 真播 N 秒（360P）→ 滚到评论区 → 点输入框 → 打字（可选 @）
     → 可选勾「同时转发到我的动态」→ 点「发布」→ 从响应取 rpid。
失败时截图存 state/comment_fail_*.png，不重试。
"""

from __future__ import annotations

import os
import pathlib
import time
from dataclasses import dataclass

from . import config as cfgmod
from .logs import get
from .store import read_json

# 沙箱/系统临时目录不可写时，Playwright 需要在自己的目录里建 artifacts
_PW_TMP = pathlib.Path(__file__).resolve().parents[1] / "state" / "pw-tmp"
_PW_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(_PW_TMP)

log = get("comment_dom")

MAX_TEXT = 60


@dataclass
class PostResult:
    ok: bool = False
    rpid: int | None = None
    reason: str = ""
    screenshot: str = ""
    mentioned: bool = False


def _cookies(cfg: cfgmod.Config) -> list[dict]:
    saved = read_json(cfg.paths.state / "cookies.json", {}) or {}
    return [
        {"name": name, "value": str(value), "domain": ".bilibili.com", "path": "/"}
        for name, value in (saved.get("cookies") or {}).items()
    ]


def _clean(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:MAX_TEXT]


def post(
    cfg: cfgmod.Config,
    *,
    bvid: str,
    message: str,
    mention: str | None = None,
    sync_dynamic: bool = False,
    watch_seconds: int = 30,
) -> PostResult:
    text = _clean(message)
    if not text:
        return PostResult(reason="正文为空")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return PostResult(reason="playwright 未安装")

    captured: dict = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True, args=["--mute-audio"])
            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
            ctx.add_cookies(_cookies(cfg))
            page = ctx.new_page()
            def _on_response(r):
                if "reply/add" not in r.url:
                    return
                captured["status"] = r.status
                captured["url"] = r.url
                try:
                    body = r.json()
                    captured["rpid"] = (body.get("data") or {}).get("rpid")
                except Exception:
                    pass

            page.on("response", _on_response)

            page.goto(f"https://www.bilibili.com/video/{bvid}/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)

            # 真播一小段：既留观看记录，也让页面处于正常播放状态
            if watch_seconds > 0:
                page.evaluate("() => { const v=document.querySelector('video'); if (v) { v.muted=true; v.play(); } }")
                page.wait_for_timeout(1200)
                try:
                    page.evaluate("() => window.player?.requestQuality?.(16)")
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(watch_seconds * 1000)

            # 滚到评论区，等懒加载
            page.evaluate("() => document.querySelector('bili-comments')?.scrollIntoView({block:'center'})")
            page.wait_for_timeout(1000)
            for _ in range(4):
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(700)
            page.wait_for_timeout(2500)

            editors = page.locator("bili-comments div.brt-editor")
            target = None
            for i in range(editors.count()):
                if editors.nth(i).is_visible():
                    target = editors.nth(i)
                    break
            if target is None:
                shot = cfg.paths.state / f"comment_fail_{int(time.time())}.png"
                page.screenshot(path=str(shot))
                browser.close()
                return PostResult(reason="找不到评论输入框", screenshot=str(shot))

            target.scroll_into_view_if_needed()
            target.click()
            page.wait_for_timeout(600)

            mentioned = False
            if mention:
                page.keyboard.type("@" + mention, delay=40)
                page.wait_for_timeout(1200)
                # @ 候选框：回车选第一个
                page.keyboard.press("Enter")
                page.wait_for_timeout(600)
                mentioned = True
                page.keyboard.type(" " + text, delay=35)
            else:
                page.keyboard.type(text, delay=35)
            page.wait_for_timeout(1000)

            if sync_dynamic:
                try:
                    box = page.get_by_text("同时转发到我的动态", exact=False).first
                    if box.count() and box.is_visible():
                        box.click()
                        page.wait_for_timeout(400)
                except Exception as exc:  # noqa: BLE001
                    log.warning("勾选同步动态失败：%s", type(exc).__name__)

            clicked = False
            for sel in ("bili-comments >> text=发布", "bili-comments .bui-area.bui-button-blue"):
                try:
                    btn = page.locator(sel).first
                    if btn.count() and btn.is_visible():
                        btn.click()
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue

            if not clicked:
                shot = cfg.paths.state / f"comment_fail_{int(time.time())}.png"
                page.screenshot(path=str(shot))
                browser.close()
                return PostResult(reason="找不到发布按钮", screenshot=str(shot))

            page.wait_for_timeout(5000)
            shot = cfg.paths.state / "comment_last.png"
            page.screenshot(path=str(shot))
            browser.close()

        status = captured.get("status")
        if status != 200:
            return PostResult(reason=f"发布请求 HTTP {status}", screenshot=str(shot))

        log.info("评论已发 %s：%s（rpid=%s，HTTP %s）", bvid, text[:20], captured.get("rpid"), captured.get("status"))
        return PostResult(ok=True, rpid=captured.get("rpid"), screenshot=str(shot), mentioned=mentioned)
    except Exception as exc:  # noqa: BLE001 - 浏览器问题不该毁掉一整轮
        log.warning("DOM 发评论失败 %s：%s", bvid, type(exc).__name__)
        return PostResult(reason=f"{type(exc).__name__}")


# ---------------- 删除评论（DOM）----------------
# 为什么不用 API：POST /x/v2/reply/action 返回 {"code":0} 但评论还在（B 站"假成功"）。
# 只能走页面菜单：⋯ → 删除 → 确定。

DELETE_JS = """
(args) => {
  const [needle, myUid] = args;

  const deep = (el) => {
    let out = el.textContent || '';
    const rec = (n) => {
      n.querySelectorAll('*').forEach(c => {
        if (c.shadowRoot) { out += ' ' + (c.shadowRoot.textContent || ''); rec(c.shadowRoot); }
      });
    };
    if (el.shadowRoot) { out += ' ' + (el.shadowRoot.textContent || ''); rec(el.shadowRoot); }
    rec(el);
    return out;
  };

  const threads = [];
  const scan = (r) => {
    r.querySelectorAll('*').forEach(el => {
      if (el.tagName === 'BILI-COMMENT-THREAD-RENDERER') threads.push(el);
      if (el.shadowRoot) scan(el.shadowRoot);
    });
  };
  scan(document);

  for (const t of threads) {
    const text = deep(t);
    if (!text.includes(needle)) continue;
    // 只删自己的：优先看头像链接里的 uid，找不到属性才退回文本判断
    const profileEls = [];
    const scanUid = (r) => {
      r.querySelectorAll('[data-user-profile-id]').forEach(el => profileEls.push(el.getAttribute('data-user-profile-id')));
      r.querySelectorAll('*').forEach(el => { if (el.shadowRoot) scanUid(el.shadowRoot); });
    };
    if (t.shadowRoot) scanUid(t.shadowRoot);
    if (profileEls.length && !profileEls.includes(String(myUid))) continue;
    if (!profileEls.length && !text.includes(String(myUid))) continue;

    const menus = [];
    const walk = (r) => {
      r.querySelectorAll('*').forEach(el => {
        if (el.tagName === 'BILI-COMMENT-MENU') menus.push(el);
        if (el.shadowRoot) walk(el.shadowRoot);
      });
    };
    if (t.shadowRoot) walk(t.shadowRoot);
    if (!menus.length) return 'no_menu';

    const menu = menus[0];
    const items = menu.shadowRoot ? Array.from(menu.shadowRoot.querySelectorAll('li')) : [];
    const del = items.find(li => (li.innerText || '').trim() === '删除');
    if (!del) return 'no_delete_item';
    del.click();
    return 'clicked';
  }
  return 'no_thread';
}
"""

DELETE_CONFIRM_JS = """
() => {
  const cands = [];
  const scan = (r) => {
    r.querySelectorAll('button, [role=button], .bui-button, div, span').forEach(el => {
      const t = (el.innerText || '').trim();
      if (t === '确定' || t === '确认') {
        const isButton = el.tagName === 'BUTTON' || el.getAttribute('role') === 'button'
          || (el.className || '').toString().includes('button');
        cands.push({ el, isButton });
      }
      if (el.shadowRoot) scan(el.shadowRoot);
    });
  };
  scan(document);
  if (!cands.length) return 'no_confirm';
  const buttons = cands.filter(c => c.isButton);
  (buttons.length ? buttons[buttons.length - 1] : cands[cands.length - 1]).el.click();
  return 'confirmed';
}
"""


@dataclass
class DeleteResult:
    ok: bool = False
    reason: str = ""


def delete_comment(cfg: cfgmod.Config, *, bvid: str, text_hint: str) -> DeleteResult:
    """删掉自己在某条视频下发的一条评论（按正文片段定位）。

    只删 uid 等于本账号的评论；找不到就不动。
    """
    needle = " ".join((text_hint or "").split())[:20]
    if not needle:
        return DeleteResult(reason="需要正文片段来定位")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return DeleteResult(reason="playwright 未安装")

    shot = cfg.paths.state / "delete_last.png"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True, args=["--mute-audio"])
            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
            ctx.add_cookies(_cookies(cfg))
            page = ctx.new_page()
            page.goto(f"https://www.bilibili.com/video/{bvid}/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            page.evaluate("() => document.querySelector('bili-comments')?.scrollIntoView({block:'center'})")
            page.wait_for_timeout(2500)

            clicked = "no_thread"
            for _ in range(6):
                clicked = page.evaluate(DELETE_JS, [needle, cfg.account.uid])
                if clicked == "clicked":
                    break
                page.mouse.wheel(0, 700)
                page.wait_for_timeout(900)

            if clicked != "clicked":
                page.screenshot(path=str(shot))
                browser.close()
                return DeleteResult(reason=f"没找到可删的评论（{clicked}）")

            # 确认弹窗在 iframe 里，JS 直接扫不到，必须用 Playwright 的文本定位
            confirmed = "no_confirm"
            for _ in range(20):
                page.wait_for_timeout(300)
                try:
                    loc = page.get_by_text("确定", exact=True)
                    if loc.count():
                        loc.last.click(timeout=3000)
                        confirmed = "confirmed"
                        break
                except Exception:  # noqa: BLE001
                    continue
            page.wait_for_timeout(2500)
            page.screenshot(path=str(shot))
            browser.close()

        if confirmed != "confirmed":
            return DeleteResult(reason="没点到确认按钮")
        log.info("已删除评论 %s（%s）", bvid, needle)
        return DeleteResult(ok=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("删除评论失败 %s", bvid)
        return DeleteResult(reason=f"{type(exc).__name__}")
