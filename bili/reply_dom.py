"""评论区的两个动作：楼中楼回复、给某条评论点赞。

踩过的坑（都写在这里，免得以后再摸）：
1. 评论区是三层嵌套 shadow DOM —— 文字要用递归 deep text 才看得到；
2. 列表虚拟滚动 —— 元素离开视口会被卸载，别乱滚；
3. `scrollIntoView` 有时不生效 —— 点击要用元素句柄交给 Playwright（它会自动滚 + 真鼠标）；
4. 桌面端「回复」按钮是图标，**没有文字**，必须按 `#reply > button` / `#like > button` 定位；
5. **用 `#reply<回复的 rpid>` 打开页面，B 站会自动展开那个楼并渲染出这条回复** —— 这是最可靠的入口，
   不用再翻页、点「点击查看」。
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from . import config as cfgmod
from .logs import get
from .store import read_json

_PW_TMP = pathlib.Path(__file__).resolve().parents[1] / "state" / "pw-tmp"
_PW_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(_PW_TMP)

log = get("reply_dom")

MAX_TEXT = 60

# 从 data-rpid 找到「这条回复」的宿主，再按 id 取按钮
FIND_IN_REPLY = """
(args) => {
  const [targetRpid, want] = args;
  const hits = [];
  const scan = (root) => {
    root.querySelectorAll('[data-rpid]').forEach(el => {
      if (el.getAttribute('data-rpid') === String(targetRpid)) hits.push(el);
    });
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) scan(el.shadowRoot); });
  };
  scan(document);
  if (!hits.length) return {status: 'no_rpid'};

  let node = hits[0];
  let host = null;
  while (node) {
    if (node.tagName === 'BILI-COMMENT-REPLY-RENDERER') { host = node; break; }
    node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
  }
  if (!host) return {status: 'no_host'};

  const deepFind = (root, id) => {
    let found = null;
    const walk = (r) => {
      r.querySelectorAll('*').forEach(el => {
        if (!found && el.id === id) found = el.querySelector('button') || el;
        if (!found && el.shadowRoot) walk(el.shadowRoot);
      });
    };
    if (root.shadowRoot) walk(root.shadowRoot);
    if (!found) walk(root);
    return found;
  };

  if (want === 'editor') {
    // 点开回复后出现的编辑器 —— 它在文档里（不一定在回复宿主内），取最后一个可见的
    let editor = null;
    const walkEditors = (r) => {
      r.querySelectorAll('[contenteditable="true"]').forEach(el => {
        if (el.offsetWidth || el.offsetHeight) editor = el;
      });
      r.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walkEditors(el.shadowRoot); });
    };
    walkEditors(document);
    return editor ? {status: 'ok', el: editor} : {status: 'no_editor'};
  }
  if (want === 'send') {
    let btn = null;
    const walkSend = (r) => {
      r.querySelectorAll('*').forEach(el => {
        if ((el.innerText || '').trim() === '发布' && el.children.length === 0
            && (el.offsetWidth || el.offsetHeight)) btn = el;
        if (el.shadowRoot) walkSend(el.shadowRoot);
      });
    };
    walkSend(document);
    return btn ? {status: 'ok', el: btn} : {status: 'no_send'};
  }

  const el = deepFind(host, want);
  return el ? {status: 'ok', el} : {status: 'no_' + want};
}
"""


EXPAND_EL = """
(rootText) => {
  const deep = (el) => {
    let out = el.textContent || '';
    const rec = (node) => {
      node.querySelectorAll('*').forEach(ch => {
        if (ch.shadowRoot) { out += ' ' + (ch.shadowRoot.textContent || ''); rec(ch.shadowRoot); }
      });
    };
    if (el.shadowRoot) { out += ' ' + (el.shadowRoot.textContent || ''); rec(el.shadowRoot); }
    rec(el);
    return out;
  };
  const threads = [];
  const scan = (root) => {
    root.querySelectorAll('*').forEach(el => {
      if (el.tagName === 'BILI-COMMENT-THREAD-RENDERER') threads.push(el);
      if (el.shadowRoot) scan(el.shadowRoot);
    });
  };
  scan(document);

  for (const t of threads) {
    if (!deep(t).includes(rootText)) continue;
    const nodes = [];
    const collect = (root) => {
      root.querySelectorAll('*').forEach(el => {
        nodes.push(el);
        if (el.shadowRoot) collect(el.shadowRoot);
      });
    };
    collect(t);
    if (t.shadowRoot) collect(t.shadowRoot);
    const link = nodes.find(e => /共\\d+条回复|点击查看/.test((e.innerText || '').trim()));
    if (!link) return null;
    return link.querySelector ? (link.querySelector('button') || link) : link;
  }
  return null;
}
"""


def _expand_thread(page, root_text: str) -> str:
    """展开某条评论的楼中楼。找不到就返回状态。"""
    if not root_text:
        return "no_text"
    needle = " ".join(root_text.split())[:18]
    for _ in range(8):
        handle = page.evaluate_handle(EXPAND_EL, needle)
        element = handle.as_element()
        if element is not None:
            try:
                element.scroll_into_view_if_needed(timeout=5000)
                element.click(timeout=5000)
                return "clicked"
            except Exception as exc:  # noqa: BLE001
                return f"error:{type(exc).__name__}"
        page.mouse.wheel(0, 700)
        page.wait_for_timeout(800)
    return "not_found"

@dataclass
class ReplyResult:
    ok: bool = False
    rpid: int | None = None
    reason: str = ""
    screenshot: str = ""


def _cookies(cfg: cfgmod.Config) -> list[dict]:
    saved = read_json(cfg.paths.state / "cookies.json", {}) or {}
    return [
        {"name": name, "value": str(value), "domain": ".bilibili.com", "path": "/"}
        for name, value in (saved.get("cookies") or {}).items()
    ]


def _locate(page, rpid, want) -> str:
    """返回状态；元素本身通过 page.evaluate_handle 拿不到时用坐标兜底。"""
    return page.evaluate(FIND_IN_REPLY, [str(rpid), want]).get("status", "error")


def _click_handle(page, rpid, want) -> str:
    handle = page.evaluate_handle(FIND_IN_REPLY, [str(rpid), want])
    result = handle.as_element()
    if result is None:
        # 返回的是 {status, el} 对象
        try:
            status = handle.get_property("status").json_value()
        except Exception:  # noqa: BLE001
            status = "unknown"
        if status != "ok":
            return status
        try:
            element = handle.get_property("el").as_element()
        except Exception:  # noqa: BLE001
            element = None
        if element is None:
            return "no_element"
    else:
        element = result
    try:
        element.scroll_into_view_if_needed(timeout=6000)
        element.click(timeout=6000)
        return "clicked"
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}"


def _type_into(page, rpid, text) -> str:
    handle = page.evaluate_handle(FIND_IN_REPLY, [str(rpid), "editor"])
    obj = handle.as_element()
    if obj is None:
        try:
            element = handle.get_property("el").as_element()
        except Exception:  # noqa: BLE001
            element = None
    else:
        element = obj
    if element is None:
        return "no_editor"
    try:
        element.click(timeout=5000)
        page.wait_for_timeout(300)
        page.keyboard.type(text, delay=35)
        return "typed"
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}"


def reply(cfg: cfgmod.Config, *, bvid: str, target_rpid: int | str, text: str, root_text: str = "") -> ReplyResult:
    text = " ".join(text.split())[:MAX_TEXT]
    if not text:
        return ReplyResult(reason="正文为空")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ReplyResult(reason="playwright 未安装")

    shot = cfg.paths.state / "reply_last.png"
    posted: dict = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True, args=["--mute-audio"])
            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
            ctx.add_cookies(_cookies(cfg))
            page = ctx.new_page()

            def _on_response(r):
                if "reply/add" not in r.url:
                    return
                posted["status"] = r.status
                try:
                    posted["rpid"] = (r.json().get("data") or {}).get("rpid")
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", _on_response)
            page.goto(
                f"https://www.bilibili.com/video/{bvid}/#reply{target_rpid}",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(8000)
            # 评论区懒加载：先滚到评论区，给组件一点时间渲染出目标回复
            try:
                page.evaluate("() => document.querySelector('bili-comments')?.scrollIntoView({block:'center'})")
            except Exception:
                pass
            page.wait_for_timeout(3000)

            opened = _click_handle(page, target_rpid, "reply")
            if opened != "clicked" and root_text:
                # 这个楼可能是折叠的：先用根评论文本展开，再试一次
                if _expand_thread(page, root_text) == "clicked":
                    page.wait_for_timeout(2500)
                    opened = _click_handle(page, target_rpid, "reply")
            if opened != "clicked":
                page.screenshot(path=str(shot))
                browser.close()
                return ReplyResult(reason=f"点不到回复按钮（{opened}）", screenshot=str(shot))
            page.wait_for_timeout(1200)

            typed = _type_into(page, target_rpid, text)
            if typed != "typed":
                page.screenshot(path=str(shot))
                browser.close()
                return ReplyResult(reason=f"输入失败（{typed}）", screenshot=str(shot))
            page.wait_for_timeout(600)

            sent = _click_handle(page, target_rpid, "send")
            page.wait_for_timeout(3500)
            page.screenshot(path=str(shot))
            browser.close()

        if sent != "clicked":
            return ReplyResult(reason=f"点不到发布（{sent}）", screenshot=str(shot))
        if posted.get("status") != 200:
            return ReplyResult(reason=f"发布请求 HTTP {posted.get('status')}", screenshot=str(shot))
        log.info("楼中楼已回 %s", bvid)
        return ReplyResult(ok=True, screenshot=str(shot))
    except Exception as exc:  # noqa: BLE001
        log.warning("楼中楼回复失败 %s：%s", bvid, type(exc).__name__)
        return ReplyResult(reason=f"{type(exc).__name__}")


def like_comment(cfg: cfgmod.Config, *, bvid: str, target_rpid: int | str) -> ReplyResult:
    """给某条评论点赞（点 #like 里的按钮）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ReplyResult(reason="playwright 未安装")

    shot = cfg.paths.state / "like_last.png"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True, args=["--mute-audio"])
            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
            ctx.add_cookies(_cookies(cfg))
            page = ctx.new_page()
            page.goto(
                f"https://www.bilibili.com/video/{bvid}/#reply{target_rpid}",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(8000)
            clicked = _click_handle(page, target_rpid, "like")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(shot))
            browser.close()
        if clicked != "clicked":
            return ReplyResult(reason=f"点不到点赞（{clicked}）", screenshot=str(shot))
        log.info("已给评论点赞 %s#%s", bvid, target_rpid)
        return ReplyResult(ok=True, screenshot=str(shot))
    except Exception as exc:  # noqa: BLE001
        log.warning("评论点赞失败：%s", type(exc).__name__)
        return ReplyResult(reason=f"{type(exc).__name__}")


FIND_ROOT_THREAD = """
(rootText) => {
  const deep = (el) => {
    let out = el.textContent || '';
    const rec = (node) => {
      node.querySelectorAll('*').forEach(ch => {
        if (ch.shadowRoot) { out += ' ' + (ch.shadowRoot.textContent || ''); rec(ch.shadowRoot); }
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
    if (!deep(t).includes(rootText)) continue;
    const nodes = [];
    const collect = (r) => {
      r.querySelectorAll('*').forEach(el => {
        nodes.push(el);
        if (el.shadowRoot) collect(el.shadowRoot);
      });
    };
    collect(t);
    if (t.shadowRoot) collect(t.shadowRoot);
    const btn = nodes.find(
      e => (e.innerText || '').trim() === '回复' && e.children.length === 0
    );
    if (btn) return {status: 'ok', el: btn};
    return {status: 'no_button'};
  }
  return {status: 'no_thread'};
}
"""


def reply_root(cfg: cfgmod.Config, *, bvid: str, root_text: str, text: str) -> ReplyResult:
    """回顶层评论：点它自己的「回复」，发出去就是楼中楼，而不是另起一条。"""
    text = " ".join(text.split())[:MAX_TEXT]
    needle = " ".join((root_text or "").split())[:16]
    if not text:
        return ReplyResult(reason="正文为空")
    if not needle:
        return ReplyResult(reason="缺少定位用的原文")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ReplyResult(reason="playwright 未安装")

    shot = cfg.paths.state / "reply_last.png"
    posted: dict = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True, args=["--mute-audio"])
            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
            ctx.add_cookies(_cookies(cfg))
            page = ctx.new_page()

            def _on_response(r):
                if "reply/add" not in r.url:
                    return
                posted["status"] = r.status
                try:
                    posted["rpid"] = (r.json().get("data") or {}).get("rpid")
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", _on_response)
            page.goto(f"https://www.bilibili.com/video/{bvid}/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4500)
            page.evaluate("() => document.querySelector('bili-comments')?.scrollIntoView({block:'center'})")
            page.wait_for_timeout(1500)

            clicked = False
            for _ in range(10):
                handle = page.evaluate_handle(FIND_ROOT_THREAD, needle)
                el = handle.get_property("el").as_element()
                if el is not None:
                    try:
                        el.scroll_into_view_if_needed(timeout=5000)
                        el.click(timeout=5000)
                        clicked = True
                        break
                    except Exception:  # noqa: BLE001
                        pass
                page.mouse.wheel(0, 700)
                page.wait_for_timeout(900)

            if not clicked:
                page.screenshot(path=str(shot))
                browser.close()
                return ReplyResult(reason="没找到那条评论的回复按钮", screenshot=str(shot))

            page.wait_for_timeout(1500)
            editor = page.evaluate_handle(EDITOR_EL)
            edit_el = editor.as_element()
            if edit_el is None:
                page.screenshot(path=str(shot))
                browser.close()
                return ReplyResult(reason="点开回复后没出现编辑器", screenshot=str(shot))

            edit_el.click(timeout=5000)
            page.keyboard.type(text, delay=35)
            page.wait_for_timeout(700)

            send = page.evaluate_handle(SEND_EL)
            send_el = send.as_element()
            if send_el is not None:
                send_el.click(timeout=5000)

            page.wait_for_timeout(3000)
            page.screenshot(path=str(shot))
            browser.close()

        if posted.get("status") != 200:
            return ReplyResult(reason=f"发布请求 HTTP {posted.get('status')}", screenshot=str(shot))
        log.info("楼中楼已回（顶层评论）%s rpid=%s", bvid, posted.get("rpid"))
        return ReplyResult(ok=True, rpid=posted.get("rpid"), screenshot=str(shot))
    except Exception as exc:  # noqa: BLE001
        log.warning("楼中楼（顶层）失败 %s：%s", bvid, type(exc).__name__)
        return ReplyResult(reason=f"{type(exc).__name__}")


EDITOR_EL = """
() => {
  let found = null;
  const scan = (root) => {
    root.querySelectorAll('[contenteditable="true"]').forEach(el => {
      if (el.offsetWidth || el.offsetHeight) found = el;
    });
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) scan(el.shadowRoot); });
  };
  scan(document);
  return found;
}
"""

SEND_EL = """
() => {
  let btn = null;
  const scan = (root) => {
    root.querySelectorAll('*').forEach(el => {
      if ((el.innerText || '').trim() === '发布' && el.children.length === 0
          && (el.offsetWidth || el.offsetHeight)) btn = el;
      if (el.shadowRoot) scan(el.shadowRoot);
    });
  };
  scan(document);
  return btn;
}
"""
