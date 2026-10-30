"""楼中楼专项：发根评论 → 等索引 → 回复 → 验证 → 清理。"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bili import comment_dom, config, reply_dom
from bili.client import API, Client
from bili.store import read_json

BV = "BV1LGpP6cEw5"
cfg = config.load()
cookies = (read_json(cfg.paths.state / "cookies.json", {}) or {}).get("cookies") or {}
client = Client(cookies)
aid = (client.get_payload(f"{API}/x/web-interface/view", {"bvid": BV}).get("data") or {}).get("aid")

root = comment_dom.post(cfg, bvid=BV, message="（QA 楼中楼根评论）", sync_dynamic=False, watch_seconds=0)
print("根评论:", root.ok, root.rpid, root.reason)
if not (root.ok and root.rpid):
    raise SystemExit(1)

print("等待索引 35 秒…")
time.sleep(35)

rep = reply_dom.reply(cfg, bvid=BV, target_rpid=root.rpid, text="（QA 楼中楼回复）")
print("楼中楼:", rep.ok, rep.rpid, rep.reason)

if rep.ok and rep.rpid:
    time.sleep(3)
    q = client.get_payload(
        "https://api.bilibili.com/x/v2/reply/reply",
        {"oid": aid, "type": 1, "root": root.rpid, "ps": 10, "pn": 1},
    )
    rr = (q.get("data") or {}).get("replies") or []
    print("楼内条数:", len(rr), "| 状态:", [(x.get("rpid"), x.get("state")) for x in rr])

print("需要清理的 rpid:", [root.rpid] + ([rep.rpid] if rep.ok and rep.rpid else []))
