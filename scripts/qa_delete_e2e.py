"""删除功能端到端验证：发评论 → 等索引 → 删除 → 复查。"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, r"D:\桌面\py\bili-pet")

from bili import comment_dom, config
from bili.client import API, Client
from bili.store import read_json

BV = "BV1LGpP6cEw5"
TEXT = "（删除功能端到端验证）"

cfg = config.load()
cookies = (read_json(cfg.paths.state / "cookies.json", {}) or {}).get("cookies") or {}
client = Client(cookies)
aid = (client.get_payload(f"{API}/x/web-interface/view", {"bvid": BV}).get("data") or {}).get("aid")

r = comment_dom.post(cfg, bvid=BV, message=TEXT, sync_dynamic=False, watch_seconds=0)
print("发评论:", r.ok, r.rpid, flush=True)
if not (r.ok and r.rpid):
    raise SystemExit(1)

print("等待索引 240 秒…", flush=True)
time.sleep(240)

d = comment_dom.delete_comment(cfg, bvid=BV, text_hint="删除功能端到端验证")
print("删除:", d.ok, d.reason, flush=True)
time.sleep(3)

q = client.get_payload(
    "https://api.bilibili.com/x/v2/reply/reply",
    {"oid": aid, "type": 1, "root": r.rpid, "ps": 1, "pn": 1},
)
root = (q.get("data") or {}).get("root") or {}
print("复查 state:", root.get("state"), "| 正文:", ((root.get("content") or {}).get("message") or "(已删除)")[:30], flush=True)
