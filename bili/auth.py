"""P0：扫码登录 → cookie 原子落盘 → 自检 → ALERT。

用法（在工作区根目录）：
    .venv\\Scripts\\python.exe -m bili.auth login     # 生成二维码，用手机 B 站 App 扫
    .venv\\Scripts\\python.exe -m bili.auth check     # 用已存 cookie 自检

不碰密码，也不把 cookie 打到任何输出里：落盘只有 state/cookies.json 一处。
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as cfgmod
from .client import Client, NetworkProblem, NotLoggedIn, BiliError, RiskControl
from .logs import get, setup
from .store import atomic_write_json, ensure_dir, read_json

log = get("auth")

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV = "https://api.bilibili.com/x/web-interface/nav"

COOKIE_FILE = "cookies.json"
QR_PNG = "login-qr.png"

# 轮询返回的二维码状态
QR_OK = 0
QR_EXPIRED = 86038
QR_SCANNED = 86090
QR_WAITING = 86101

_STATUS_TEXT = {
    QR_WAITING: "等待扫码…",
    QR_SCANNED: "已扫码，请在手机上点确认",
    QR_EXPIRED: "二维码已过期",
}

KEEP_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")


def _cookie_path(cfg: cfgmod.Config) -> Path:
    return ensure_dir(cfg.paths.state) / COOKIE_FILE


def alert(cfg: cfgmod.Config, reason: str) -> None:
    """写 ALERT.md。日志里的原因已经脱敏，这里再兜一层。"""
    path = ensure_dir(cfg.paths.reports) / "ALERT.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {stamp}  {reason}\n")
    except OSError as exc:
        log.error("写 ALERT 失败：%s", type(exc).__name__)
    log.error("ALERT: %s", reason)


def _render_qr(url: str, png_path: Path, *, quiet: bool = False) -> None:
    import segno

    qr = segno.make(url, error="m")
    qr.save(str(png_path), scale=6, border=2)
    if quiet:
        return
    print(f"\n二维码已保存：{png_path}")
    print("（用手机 B 站 App 扫；终端里的方块图若显示正常也可以直接扫）\n")
    qr.terminal(compact=True)


def _cookies_from_url(url: str) -> dict[str, str]:
    """登录成功时 data.url 会带一遍 cookie 参数，作为 Set-Cookie 缺失时的兜底。"""
    query = parse_qs(urlparse(url).query)
    return {name: values[0] for name, values in query.items() if values and name in KEEP_COOKIES}


def _nav(client: Client) -> dict:
    payload = client.get_payload(NAV)
    data = payload.get("data") or {}
    if not data.get("isLogin"):
        raise NotLoggedIn("nav 显示未登录")
    return data


def _wait_scan(client: Client, qrcode_key: str, timeout: int) -> tuple[int, dict]:
    """轮询二维码状态。返回 (结束状态, poll data)，超时状态为 -1。"""
    deadline = _time.monotonic() + timeout
    last_status = None
    data: dict = {}
    while _time.monotonic() < deadline:
        payload = client.get_payload(QR_POLL, {"qrcode_key": qrcode_key})
        data = payload.get("data") or {}
        status = data.get("code")
        if status in (QR_OK, QR_EXPIRED):
            return status, data
        if status not in (QR_WAITING, QR_SCANNED):
            return status, data
        if status != last_status:
            print(f"  {_STATUS_TEXT.get(status, str(status))}")
            last_status = status
        _time.sleep(2.0)
    return -1, data


def login(cfg: cfgmod.Config, *, timeout: int = 180, refreshes: int = 3) -> int:
    """扫码登录。B 站二维码本身只有 3 分钟寿命，所以过期就换一张，最多换 refreshes 张。"""
    client = Client()
    png = ensure_dir(cfg.paths.state) / QR_PNG
    data: dict = {}

    for attempt in range(1, refreshes + 1):
        gen = client.get_payload(QR_GENERATE).get("data") or {}
        qrcode_key, qr_url = gen.get("qrcode_key"), gen.get("url")
        if not qrcode_key or not qr_url:
            alert(cfg, "登录二维码接口返回结构异常")
            return 1

        if attempt == 1:
            _render_qr(qr_url, png)
        else:
            _render_qr(qr_url, png, quiet=True)
            print(f"二维码已刷新（第 {attempt}/{refreshes} 张），同一路径覆盖")

        status, data = _wait_scan(client, qrcode_key, timeout)

        if status == QR_OK:
            break
        if status == QR_EXPIRED:
            log.warning("登录二维码过期（第 %s 张）", attempt)
            if attempt == refreshes:
                print("二维码连续过期，先不登录了；要登录再跑一次 login")
                png.unlink(missing_ok=True)
                return 2
            continue
        if status == -1:
            log.warning("扫码超时（%ss），未完成登录", timeout)
            print(f"等待扫码超时（{timeout} 秒），重新跑一次 login 即可")
            png.unlink(missing_ok=True)
            return 2
        log.warning("扫码轮询返回未知状态 %s", status)
        print(f"扫码轮询返回未知状态 {status}")
        png.unlink(missing_ok=True)
        return 2
    else:
        return 2

    png.unlink(missing_ok=True)

    cookies = client.cookie_dict()
    if not cookies.get("SESSDATA"):
        cookies.update(_cookies_from_url(data.get("url", "")))
    if not cookies.get("SESSDATA"):
        alert(cfg, "扫码成功但没拿到 SESSDATA，登录未完成")
        return 2

    client.session.cookies.update(cookies)
    try:
        me = _nav(client)
    except NotLoggedIn:
        alert(cfg, "扫码后 nav 仍显示未登录（cookie 未生效）")
        return 2

    uid, name = me.get("mid"), me.get("uname")
    if cfg.account.uid and uid != cfg.account.uid:
        alert(cfg, f"登录到错误的账号：uid={uid}（期望 {cfg.account.uid}）")
        return 3
    if cfg.account.name and name != cfg.account.name:
        log.warning("昵称与配置不一致：实际 %s，配置 %s（uid 正确，继续）", name, cfg.account.name)

    expires = _parse_expires(data.get("url", ""))
    saved = {
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "uid": uid,
        "name": name,
        "expires_at": expires,
        "cookies": {k: v for k, v in cookies.items() if k in KEEP_COOKIES or k == "SESSDATA"},
    }
    atomic_write_json(_cookie_path(cfg), saved)

    log.info("登录成功：uid=%s name=%s", uid, name)
    if expires:
        when = datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M")
        log.info("cookie 到期时间：%s", when)
    print(f"\n登录成功：{name} (uid={uid})")
    return 0


def _parse_expires(url: str) -> int | None:
    query = parse_qs(urlparse(url).query)
    raw = (query.get("Expires") or [None])[0]
    if raw and str(raw).isdigit():
        return int(raw)
    return None


def check(cfg: cfgmod.Config) -> int:
    saved = read_json(_cookie_path(cfg), None)
    if not saved or not saved.get("cookies"):
        alert(cfg, "没有本地 cookie，需要扫码登录")
        return 2

    client = Client(saved["cookies"])
    try:
        me = _nav(client)
    except NotLoggedIn:
        alert(cfg, "cookie 已失效，需要重新扫码")
        return 2
    except RiskControl as exc:
        alert(cfg, f"自检时触发风控：{exc}")
        return 4
    except (BiliError, NetworkProblem) as exc:
        log.error("自检失败：%s", exc)
        return 1

    uid, name = me.get("mid"), me.get("uname")
    if cfg.account.uid and uid != cfg.account.uid:
        alert(cfg, f"本地 cookie 属于 uid={uid}，不是配置的 {cfg.account.uid}")
        return 3

    log.info("自检通过：uid=%s name=%s", uid, name)
    print(f"自检通过：{name} (uid={uid})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bili.auth", description="扫码登录与登录自检")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login", help="扫码登录（需要手机 B 站 App）")
    p_login.add_argument("--timeout", type=int, default=300, help="等待扫码的秒数")
    sub.add_parser("check", help="用已存 cookie 自检")

    args = parser.parse_args(argv)
    timeout = getattr(args, "timeout", 300)
    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    setup(cfg.paths.logs, cfg.log_level)

    try:
        return login(cfg, timeout=timeout) if args.cmd == "login" else check(cfg)
    except RiskControl as exc:
        alert(cfg, f"风控拦截：{exc}")
        return 4
    except NetworkProblem as exc:
        log.error("%s", exc)
        print(f"网络问题：{exc}", file=sys.stderr)
        return 1
    except BiliError as exc:
        log.error("接口错误：%s", exc)
        print(f"接口错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
