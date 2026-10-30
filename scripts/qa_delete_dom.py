"""用浏览器 DOM 删除自己发的评论（API 删除会"假成功"）。

用法：
    python scripts/qa_delete_dom.py <BV号> <评论正文片段> [更多片段...]

例：
    python scripts/qa_delete_dom.py BV1xx411c7mD "测试评论"
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = ROOT / "state" / "pw-tmp"
TMP.mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(TMP)
sys.path.insert(0, str(ROOT))

from bili import config
from bili.store import read_json

if len(sys.argv) < 3:
    print(__doc__)
    raise SystemExit(1)

BV = sys.argv[1]
NEEDLES = sys.argv[2:]

cfg = config.load()
cookies = (read_json(cfg.paths.state / "cookies.json", {}) or {}).get("cookies") or {}

from playwright.sync_api import sync_playwright

CLICK_DELETE = """
(needle) => {
  const deep = (el) => {
    let out = el.textContent || '';
    const rec = (n) => { n.querySelectorAll('*').forEach(c => { if (c.shadowRoot) { out += ' ' + (c.shadowRoot.textContent || ''); rec(c.shadowRoot); } }); };
    if (el.shadowRoot) { out += ' ' + (el.shadowRoot.textContent || ''); rec(el.shadowRoot); }
    rec(el);
    return out;
  };
  const threads = [];
  const scan = (r) => { r.querySelectorAll('*').forEach(el => { if (el.tagName === 'BILI-COMMENT-THREAD-RENDERER') threads.push(el); if (el.shadowRoot) scan(el.shadowRoot); }); };
  scan(document);
  const t = threads.find(x => deep(x).includes(needle));
  if (!t) return 'no_thread';

  const menus = [];
  const walk = (r) => { r.querySelectorAll('*').forEach(el => { if (el.tagName === 'BILI-COMMENT-MENU') menus.push(el); if (el.shadowRoot) walk(el.shadowRoot); }); };
  if (t.shadowRoot) walk(t.shadowRoot);
  if (!menus.length) return 'no_menu';

  const menu = menus[0];
  t.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
  const items = menu.shadowRoot ? Array.from(menu.shadowRoot.querySelectorAll('li')) : [];
  const del = items.find(li => (li.innerText || '').trim() === '删除');
  if (!del) return 'no_delete_item';
  del.click();
  return 'clicked';
}
"""

CONFIRM = """
() => {
  const words = ['确定', '确认'];
  let hit = null;
  const scan = (r) => {
    r.querySelectorAll('*').forEach(el => {
      const t = (el.innerText || '').trim();
      if (!hit && words.includes(t) && el.children.length === 0) hit = el;
      if (el.shadowRoot) scan(el.shadowRoot);
    });
  };
  scan(document);
  if (!hit) return false;
  hit.click();
  return true;
}
"""

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=["--mute-audio"])
    ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 900})
    ctx.add_cookies(
        [{"name": k, "value": str(v), "domain": ".bilibili.com", "path": "/"} for k, v in cookies.items()]
    )
    page = ctx.new_page()
    page.goto(f"https://www.bilibili.com/video/{BV}/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    page.evaluate("() => document.querySelector('bili-comments')?.scrollIntoView({block:'center'})")
    page.wait_for_timeout(2500)

    for needle in NEEDLES:
        print(f"--- {needle} ---")
        r = page.evaluate(CLICK_DELETE, needle)
        print("点删除:", r)
        if r != "clicked":
            continue
        page.wait_for_timeout(1500)
        try:
            loc = page.get_by_text("确定", exact=True)
            if loc.count():
                loc.last.click(timeout=5000)
                print("确认: clicked")
        except Exception:  # noqa: BLE001
            print("确认: failed")
        page.wait_for_timeout(2500)

    browser.close()
