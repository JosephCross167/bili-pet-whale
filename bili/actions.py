"""写操作。全项目唯一允许改动 B 站数据的地方。

三道闸，任何一道不过就抛 ActionRefused，绝不"尽力而为"：
1. 收件人/目标必须在白名单（`config.toml [targets]`），代码里逐条校验；
2. 必须有 csrf（bili_jct），没有说明 cookie 不完整；
3. 调用方必须显式传入 `confirm=True`——避免"顺手调一下"变成线上事故。

P1 的 runner 不会 import 这个模块；它只在被明确调用时工作。
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote, urlencode
import uuid

from . import config as cfgmod, quota
from .client import BiliError, Client, RiskControl
from .logs import get
from .store import atomic_write_json, read_json
from .watch import record_comment

log = get("actions")

LINK_RE_DYNAMIC = re.compile(r"(https?://|www\\.)", re.I)

SEND_DM = "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg"
REPLY_ADD = "https://api.bilibili.com/x/v2/reply/add"
DYNAMIC_ADD = "https://api.bilibili.com/x/dynamic/feed/create/dyn"


class ActionRefused(RuntimeError):
    """被护栏拦下——不是失败，是设计。"""


class NeedVerify(RuntimeError):
    """B 站要求人机验证（风控决策里 scene=video_coin 之类）。

    这种情况绝不能自动过验证：停下来、记一笔、跳过这一个动作，
    也不要熔断——熔断是给 412/-352 用的。
    """


def _device_id(cfg: cfgmod.Config) -> str:
    path = cfg.paths.state / "device.json"
    data = read_json(path, None) or {}
    dev_id = data.get("dev_id")
    if not dev_id:
        dev_id = uuid.uuid4().hex[:16]
        atomic_write_json(path, {"dev_id": dev_id})
    return str(dev_id)


def _post(client: Client, cfg: cfgmod.Config, url: str, data: dict) -> dict:
    """所有写操作都从这里出去：先过每小时天花板，再发。"""
    if quota.over(cfg, "write"):
        raise ActionRefused(
            f"本小时写操作已达上限 {quota.limit(cfg, 'write')} 次，先停手（下个整点恢复）"
        )
    quota.bump("write")
    client._pace()  # noqa: SLF001 - 复用同一套限速
    try:
        resp = client.session.post(url, data=data, timeout=client.timeout)
    except Exception as exc:  # noqa: BLE001
        raise BiliError(f"写请求失败：{type(exc).__name__}") from None
    if resp.status_code == 412:
        raise RiskControl("HTTP 412，写操作被风控拦截")
    if resp.status_code != 200:
        raise BiliError(f"HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        raise BiliError("写接口返回的不是 JSON") from None
    code = payload.get("code")
    if code == -101:
        raise BiliError("未登录（-101）")
    if code == -401 and isinstance(payload.get("data"), dict) and payload["data"].get("ga_data"):
        raise NeedVerify(f"需要人机验证（{payload['data'].get('ga_data', {}).get('decision_ctx', {}).get('origin_scene', '?')}）")
    if code in (-352, -412, -401):
        raise RiskControl(f"写操作触发风控（{code}）")
    if code != 0:
        raise BiliError(f"写接口返回 code={code} message={payload.get('message')}")
    return payload


def send_dm(client: Client, cfg: cfgmod.Config, uid: int, text: str, *, confirm: bool = False) -> dict:
    if not confirm:
        raise ActionRefused("send_dm 需要 confirm=True")
    if not cfg.targets.is_allowed(uid):
        raise ActionRefused(f"UID {uid} 不在白名单，拒绝发送")
    text = " ".join(text.split())[:300]
    if not text:
        raise ActionRefused("私信正文为空")
    if not client.csrf:
        raise ActionRefused("缺少 bili_jct（csrf），cookie 不完整")

    stamp = int(time.time())
    payload = _post(client, cfg, SEND_DM,
        {
            "msg[sender_uid]": cfg.account.uid,
            "msg[receiver_id]": uid,
            "msg[receiver_type]": 1,
            "msg[msg_type]": 1,
            "msg[dev_id]": _device_id(cfg),
            "msg[timestamp]": stamp,
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "csrf": client.csrf,
            "csrf_token": client.csrf,
        },
    )
    log.info("私信已发：uid=%s len=%s", uid, len(text))
    return payload


def post_comment(
    client: Client,
    cfg: cfgmod.Config,
    *,
    aid: int,
    message: str,
    at: dict[str, int] | None = None,
    bvid: str = "",
    confirm: bool = False,
) -> dict:
    """发一条评论。at 传 {昵称: uid}，两个 UID 都必须在白名单里。"""
    if not confirm:
        raise ActionRefused("post_comment 需要 confirm=True")
    for name, uid in (at or {}).items():
        if not cfg.targets.is_allowed(int(uid)):
            raise ActionRefused(f"@{name}({uid}) 不在白名单，拒绝发送")
    message = message.strip()
    if not message:
        raise ActionRefused("评论正文为空")
    if not client.csrf:
        raise ActionRefused("缺少 bili_jct（csrf），cookie 不完整")

    data = {
        "oid": aid,
        "type": 1,
        "message": message,
        "plat": 1,
        "csrf": client.csrf,
        # 网页端固定带这两个：gaia_source 是风控场景标记，statistics 是客户端统计
        "gaia_source": "main_web",
        "statistics": json.dumps({"appId": 100, "platform": 5}, separators=(",", ":")),
    }
    if at:
        data["at_name_to_mid"] = json.dumps(at, ensure_ascii=False)

    # 评论接口走 wbi 签名（抓包里的 w_rid/wts 就是证据）；不签名等于告诉服务端"我不是网页"
    params = {
        "x-bili-locale-json": json.dumps(
            {"c_locale": {"language": "zh", "script": "Hans"}, "always_translate": False},
            separators=(",", ":"),
        )
    }
    try:
        signed = client.signer().sign(params)
    except Exception as exc:  # noqa: BLE001 - 签名失败就别发，宁可少一条
        raise BiliError(f"评论签名失败：{type(exc).__name__}") from None
    query = urlencode(signed, quote_via=quote, safe="")
    url = f"{REPLY_ADD}?{query}"

    payload = _post(client, cfg, url, data)
    rpid = (payload.get("data") or {}).get("rpid")
    log.info("评论已发：aid=%s rpid=%s at=%s", aid, rpid, list((at or {}).keys()))
    if rpid:
        # 发出去只代表写进库了；可见性由 watch.check_comments 复查（新号首条常被置为仅自己可见）
        record_comment(
            cfg,
            rpid=rpid,
            aid=aid,
            bvid=bvid,
            message=message,
        )
    return payload


def post_dynamic(
    client: Client,
    cfg: cfgmod.Config,
    *,
    text: str,
    aid: int | None = None,
    confirm: bool = False,
) -> dict:
    """发一条动态；带 aid 就是「转发视频到动态」。

    用途是给评论留溯源入口，也让账号更像活人（真人发评论常顺手转动态）。
    ⚠ 这个接口的 dyn_req 结构是照社区文档拼的，**第一次实发要盯着返回码**，
    失败就只记日志、不影响评论本身。
    """
    if not confirm:
        raise ActionRefused("post_dynamic 需要 confirm=True")
    text = " ".join((text or "").split())[:200]
    if not text:
        raise ActionRefused("动态正文为空")
    if LINK_RE_DYNAMIC.search(text):
        raise ActionRefused("动态正文里不带链接")

    req: dict = {
        "content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]},
        "scene": 1,
    }
    if aid:
        req["attach_card"] = {"type": "DYNAMIC_TYPE_AV", "biz_id": int(aid)}

    payload = _post(
        client,
        cfg,
        DYNAMIC_ADD,
        {"dyn_req": json.dumps(req, ensure_ascii=False), "csrf": client.csrf},
    )
    dyn_id = (payload.get("data") or {}).get("dyn_id")
    log.info("动态已发：dyn_id=%s aid=%s", dyn_id, aid)
    return payload


# ---------------------------------------------------------------- 点赞 / 投币 / 收藏

LIKE = "https://api.bilibili.com/x/web-interface/archive/like"
COIN = "https://api.bilibili.com/x/web-interface/coin/add"
FAV_DEAL = "https://api.bilibili.com/x/v3/fav/resource/deal"
FAV_FOLDERS = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"


def fav_folder_id(client: Client, cfg: cfgmod.Config) -> int:
    """按名字找收藏夹。找不到就报错，不替用户新建、也不猜默认夹。"""
    payload = client.get_payload(FAV_FOLDERS, {"up_mid": cfg.account.uid})
    folders = (payload.get("data") or {}).get("list") or []
    for folder in folders:
        if folder.get("title") == cfg.actions.fav_folder:
            return int(folder["id"])
    titles = [f.get("title") for f in folders]
    raise ActionRefused(f"找不到收藏夹「{cfg.actions.fav_folder}」，现有：{titles}")


def like(client: Client, cfg: cfgmod.Config, aid: int, *, confirm: bool = False) -> dict:
    if not confirm:
        raise ActionRefused("like 需要 confirm=True")
    payload = _post(client, cfg, LIKE, {"aid": aid, "like": 1, "csrf": client.csrf})
    log.info("已点赞：aid=%s", aid)
    return payload


def unlike(client: Client, cfg: cfgmod.Config, aid: int, *, confirm: bool = False) -> dict:
    """取消点赞（like=2）。"""
    if not confirm:
        raise ActionRefused("unlike 需要 confirm=True")
    payload = _post(client, cfg, LIKE, {"aid": aid, "like": 2, "csrf": client.csrf})
    log.info("已取消点赞：aid=%s", aid)
    return payload


def coin(
    client: Client, cfg: cfgmod.Config, aid: int, *, multiply: int = 1, confirm: bool = False
) -> dict:
    if not confirm:
        raise ActionRefused("coin 需要 confirm=True")
    multiply = 2 if multiply >= 2 else 1
    payload = _post(
        client,
        cfg,
        COIN,
        {"aid": aid, "multiply": multiply, "select_like": 0, "csrf": client.csrf},
    )
    log.info("已投币：aid=%s ×%s", aid, multiply)
    return payload


def favorite(client: Client, cfg: cfgmod.Config, aid: int, *, confirm: bool = False) -> dict:
    if not confirm:
        raise ActionRefused("favorite 需要 confirm=True")
    fid = fav_folder_id(client, cfg)
    payload = _post(
        client, cfg, FAV_DEAL, {"rid": aid, "type": 2, "add_media_ids": fid, "csrf": client.csrf}
    )
    log.info("已收藏：aid=%s → 夹子 %s", aid, cfg.actions.fav_folder)
    return payload

