"""把「回复我的」里白名单还没回过的评论，逐条点赞 + 楼中楼回复。"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bili import config, reply_dom, replies
from bili.client import API, Client
from bili.llm import Llm
from bili.store import read_json
from brain import speak as speak_mod

MAX_ITEMS = 8
ALREADY = {322318852080}  # 之前手工回过的

cfg = config.load()
cookies = (read_json(cfg.paths.state / "cookies.json", {}) or {}).get("cookies") or {}
client = Client(cookies)
llm = Llm(cfg)

# 先记上手工回过的那条
for rpid in ALREADY:
    replies.mark_replied(cfg, rpid)

feed = client.get_payload(f"{API}/x/msgfeed/reply", {"pn": 1, "ps": 30})
items = (feed.get("data") or {}).get("items") or []
print(f"回复我的共 {len(items)} 条")

done_set = replies.replied_set(cfg)
targets = []
for it in items:
    user = it.get("user") or {}
    mid = int(user.get("mid") or 0)
    if not cfg.targets.is_allowed(mid):
        continue
    payload = it.get("item") or {}
    rpid = payload.get("source_id")
    if not rpid or str(rpid) in done_set:
        continue
    targets.append(
        {
            "rpid": str(rpid),
            "root": payload.get("root_id"),
            "aid": payload.get("subject_id"),
            "bvid": (payload.get("uri") or "").rsplit("/", 1)[-1],
            "name": user.get("nickname") or str(mid),
            "mid": mid,
            "my_comment": (payload.get("title") or "").strip(),
        }
    )

print(f"待回 {len(targets)} 条：", [(t["rpid"], t["name"]) for t in targets])

for item in targets[:MAX_ITEMS]:
    # 取对方的回复正文
    try:
        thread = client.get_payload(
            f"{API}/x/v2/reply/reply",
            {"oid": item["aid"], "type": 1, "root": item["root"], "ps": 50, "pn": 1},
        )
    except Exception as exc:  # noqa: BLE001
        print("  拉楼失败:", type(exc).__name__)
        continue

    their_text = ""
    for reply in (thread.get("data") or {}).get("replies") or []:
        if str(reply.get("rpid")) == item["rpid"]:
            their_text = ((reply.get("content") or {}).get("message") or "").strip()
            break
    if not their_text:
        print(f"  跳过 {item['rpid']}：找不到回复正文")
        continue

    text = speak_mod.reply_comment(
        llm, cfg,
        who=item["name"],
        my_comment=item["my_comment"],
        their_reply=their_text,
    )
    print(f"\n回 {item['name']}：「{their_text[:30]}」→ {text}")
    if not text:
        continue

    result = reply_dom.reply(cfg, bvid=item["bvid"], target_rpid=item["rpid"], text=text)
    print("  回复:", result.ok, result.reason)
    if result.ok:
        replies.mark_replied(cfg, item["rpid"])
        liked = reply_dom.like_comment(cfg, bvid=item["bvid"], target_rpid=item["rpid"])
        print("  点赞:", liked.ok, liked.reason)
    time.sleep(4)

print("\n全部处理完毕")
