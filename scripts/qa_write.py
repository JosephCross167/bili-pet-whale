"""真实写操作 QA：只在你自己投稿的视频下操作，跑完清理。

视频：BV1LGpP6cEw5（你名下）
测试项：发评论 / 带@评论 / 楼中楼 / 点赞 / 收藏 / 投币
清理：删除评论、取消点赞与收藏
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bili import actions, comment_dom, config, reply_dom
from bili.client import API, Client
from bili.store import read_json

BV = "BV1LGpP6cEw5"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, "PASS" if ok else "FAIL", detail))


cfg = config.load()
cookies = (read_json(cfg.paths.state / "cookies.json", {}) or {}).get("cookies") or {}
client = Client(cookies)
aid = (client.get_payload(f"{API}/x/web-interface/view", {"bvid": BV}).get("data") or {}).get("aid")
check("准备：拿到 aid", bool(aid), str(aid))

created: list[int] = []


def state_of(rpid: int) -> int | None:
    q = client.get_payload(
        "https://api.bilibili.com/x/v2/reply/reply",
        {"oid": aid, "type": 1, "root": rpid, "ps": 1, "pn": 1},
    )
    return ((q.get("data") or {}).get("root") or {}).get("state")


def delete(rpid: int, root: int | None = None) -> bool:
    r = client.session.post(
        f"{API}/x/v2/reply/action",
        data={"oid": aid, "type": 1, "rpid": rpid, "root": root or rpid, "action": 0, "csrf": client.csrf},
        headers={"Referer": "https://www.bilibili.com/"},
        timeout=20,
    )
    try:
        return r.json().get("code") == 0
    except Exception:  # noqa: BLE001
        return False


# 1. 普通评论
res1 = comment_dom.post(cfg, bvid=BV, message="（QA 测试）普通评论，测完删除", sync_dynamic=False, watch_seconds=0)
check("发普通评论", res1.ok and bool(res1.rpid), f"rpid={res1.rpid} {res1.reason}")
if res1.ok and res1.rpid:
    created.append(res1.rpid)
    time.sleep(2)
    st = state_of(res1.rpid)
    check("普通评论可见", st == 0, f"state={st}")

# 2. 带 @ 的评论（@ 自己）
res2 = comment_dom.post(
    cfg, bvid=BV, message="（QA 测试）带@评论，测完删除", mention="康普顿167",
    sync_dynamic=False, watch_seconds=0,
)
check("发带@评论", res2.ok and bool(res2.rpid), f"rpid={res2.rpid} {res2.reason}")
if res2.ok and res2.rpid:
    created.append(res2.rpid)
    time.sleep(2)
    st = state_of(res2.rpid)
    q = client.get_payload(
        "https://api.bilibili.com/x/v2/reply/reply",
        {"oid": aid, "type": 1, "root": res2.rpid, "ps": 1, "pn": 1},
    )
    root = (q.get("data") or {}).get("root") or {}
    members = [m.get("uname") for m in ((root.get("content") or {}).get("members") or [])]
    check("带@评论可见且真@到人", st == 0 and "康普顿167" in members, f"state={st} members={members}")

# 3. 楼中楼：在普通评论下面回一条
if created:
    root_rpid = created[0]
    res3 = reply_dom.reply(cfg, bvid=BV, target_rpid=root_rpid, text="（QA 测试）楼中楼回复")
    check("楼中楼回复", res3.ok, f"rpid={res3.rpid} {res3.reason}")
    if res3.ok and res3.rpid:
        created.append(res3.rpid)
        time.sleep(2)
        check("楼中楼可见", state_of(res3.rpid) == 0, f"state={state_of(res3.rpid)}")

# 4. 点赞 / 取消
try:
    actions.like(client, cfg, aid, confirm=True)
    time.sleep(1)
    actions.unlike(client, cfg, aid, confirm=True)
    check("点赞/取消点赞", True)
except Exception as exc:  # noqa: BLE001
    check("点赞/取消点赞", False, type(exc).__name__)

# 5. 收藏 / 取消
try:
    actions.favorite(client, cfg, aid, confirm=True)
    time.sleep(1)
    folder = actions.fav_folder_id(client, cfg)
    client.session.post(
        f"{API}/x/v3/fav/resource/deal",
        data={"rid": aid, "type": 2, "add_media_ids": "", "del_media_ids": folder, "csrf": client.csrf},
        headers={"Referer": "https://www.bilibili.com/"},
        timeout=20,
    )
    check("收藏/取消收藏", True, f"folder={folder}")
except Exception as exc:  # noqa: BLE001
    check("收藏/取消收藏", False, type(exc).__name__)

# 6. 投币（不可撤销）
money_before = client.nav().get("money")
try:
    actions.coin(client, cfg, aid, multiply=1, confirm=True)
    money_after = client.nav().get("money")
    check("投币", money_before is not None and money_after is not None and money_after < money_before,
          f"{money_before} → {money_after}")
except Exception as exc:  # noqa: BLE001
    check("投币", False, type(exc).__name__)

# 7. 清理评论
deleted = 0
for rpid in reversed(created):
    ok = delete(rpid, root=created[0] if rpid != created[0] else None)
    if ok:
        deleted += 1
    time.sleep(1)
check("清理测试评论", deleted > 0, f"删除请求成功 {deleted}/{len(created)}")

# 复查
time.sleep(3)
left = [r for r in created if state_of(r) is not None]
check("复查：无残留", len(left) == 0, f"残留 rpid={left}")

print(f"{'用例':<28}结果")
print("-" * 70)
for name, res, detail in results:
    print(f"{name:<28}{res:<6}{detail}")
print("-" * 70)
print(f"共 {len(results)} 项，失败 {sum(1 for r in results if r[1] == 'FAIL')} 项")
