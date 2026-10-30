"""联网 QA（只读）：验证各接口在当前账号下可用、字段齐全。"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bili import config, feed, taste, video
from bili.client import API, Client
from bili.store import read_json

results: list[tuple[str, str, str]] = []


def check(api: str, case: str, ok: bool, detail: str = "") -> None:
    results.append((api, case, ("PASS" if ok else "FAIL") + (f" | {detail}" if detail else "")))


cfg = config.load()
cookies = (read_json(cfg.paths.state / "cookies.json", {}) or {}).get("cookies") or {}
client = Client(cookies)
MY_VIDEO = "BV1LGpP6cEw5"  # 你自己投稿的视频，用于只读探测

# 1. 登录态
try:
    nav = client.nav()
    check("nav", "登录态有效", bool(nav.get("isLogin")), f"uid={nav.get('mid')} 硬币={nav.get('money')}")
except Exception as exc:  # noqa: BLE001
    check("nav", "登录态有效", False, type(exc).__name__)

# 2. 推荐流
try:
    pool = feed.fetch_recommend(client, pages=1)
    check("feed", "推荐流取到", len(pool) > 0, f"{len(pool)} 条")
    pool = feed.enrich(client, pool, limit=3)
    v0 = pool[0]
    check("feed", "enrich 填 tags", bool(v0.tags), f"{len(v0.tags)} 个")
    check("feed", "分区名可读", v0.zone_label != "未知分区", v0.zone_label)
except Exception as exc:  # noqa: BLE001
    check("feed", "推荐流取到", False, type(exc).__name__)

# 3. 视频详情 / 字幕
try:
    v = video.detail(client, MY_VIDEO)
    check("video", "详情可取", bool(v.title), v.title[:20])
    t = video.transcript(client, MY_VIDEO)
    check("video", "transcript 不报错", True, f"{len(t)} 字")
except Exception as exc:  # noqa: BLE001
    check("video", "详情可取", False, type(exc).__name__)

# 4. 评论列表
try:
    comments = video.top_comments(client, v.aid, count=5) if v.aid else []
    check("video", "热评可取", isinstance(comments, list), f"{len(comments)} 条")
except Exception as exc:  # noqa: BLE001
    check("video", "热评可取", False, type(exc).__name__)

# 5. 收藏夹
try:
    folders = taste._folders(client, cfg.targets.admin_uid)
    check("taste", "收藏夹列表可取", len(folders) > 0, f"{len(folders)} 个")
except Exception as exc:  # noqa: BLE001
    check("taste", "收藏夹列表可取", False, type(exc).__name__)

# 6. 消息中心
for name, path in (("回复我的", "/x/msgfeed/reply"), ("@我的", "/x/msgfeed/at")):
    try:
        payload = client.get_payload(f"{API}{path}", {"pn": 1, "ps": 5})
        items = (payload.get("data") or {}).get("items") or []
        check("msgfeed", name, True, f"{len(items)} 条")
    except Exception as exc:  # noqa: BLE001
        check("msgfeed", name, False, type(exc).__name__)

# 7. 搜索
try:
    hits = client.get_wbi(f"{API}/x/web-interface/wbi/search/type", {
        "search_type": "video", "keyword": "明日方舟", "order": "click", "page": 1, "page_size": 5,
    })
    got = ((hits.get("data") or {}).get("result") or [])
    check("search", "wbi 搜索可用", len(got) > 0, f"{len(got)} 条")
except Exception as exc:  # noqa: BLE001
    check("search", "wbi 搜索可用", False, type(exc).__name__)

# 8. 私信会话
try:
    sess = client.get_payload(
        "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions",
        {"session_type": 1, "group_id": 0, "ps": 20},
    )
    lst = (sess.get("data") or {}).get("session_list") or []
    check("dm", "会话列表可取", True, f"{len(lst)} 个会话")
except Exception as exc:  # noqa: BLE001
    check("dm", "会话列表可取", False, type(exc).__name__)

# 9. 动态
try:
    d = client.get_payload(f"{API}/x/polymer/web-dynamic/v1/feed/space", {"host_mid": cfg.targets.admin_uid})
    items = (d.get("data") or {}).get("items") or []
    check("dynamic", "空间动态可取", True, f"{len(items)} 条")
except Exception as exc:  # noqa: BLE001
    check("dynamic", "空间动态可取", False, type(exc).__name__)

print(f"{'接口':<10}{'用例':<22}结果")
print("-" * 78)
for api, case, res in results:
    print(f"{api:<10}{case:<22}{res}")
fails = [r for r in results if r[2].startswith("FAIL")]
print("-" * 78)
print(f"共 {len(results)} 项，失败 {len(fails)} 项")
