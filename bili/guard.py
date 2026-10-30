"""护栏。比 actions 重要的那一半：预算、去重、熔断、网络/电源/时间窗。

P1 只用得到「读」的一半，但所有判断现在都按最终形态写好，
免得 P2 接写操作时再补一套平行逻辑（那才是 bug 温床）。
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as cfgmod
from .client import network_ok
from .logs import get
from .store import atomic_write_json, read_json

log = get("guard")

LOCK_STALE_SECONDS = 3 * 3600


# ---------------------------------------------------------------- 时间窗
def now_in(cfg: cfgmod.Config) -> datetime:
    return datetime.now(ZoneInfo(cfg.timezone))


def window_for(cfg: cfgmod.Config, moment: datetime | None = None) -> cfgmod.Window | None:
    moment = moment or now_in(cfg)
    for window in cfg.schedule.windows:
        if window.contains(moment.time()):
            return window
    return None


def jitter_seconds(cfg: cfgmod.Config, rng) -> int:
    """窗口内再随机推迟一段，别每天固定分钟数落在服务器眼里。

    推迟量不能把运行顶到窗口外（尤其别顶到 23:30 断电之后），所以再按「距窗口结束剩多少」截一次。
    """
    minutes = max(0, cfg.schedule.jitter_minutes)
    if not minutes:
        return 0
    window = window_for(cfg)
    if window is None:
        return rng.randint(0, minutes * 60)
    now = now_in(cfg)
    end = datetime.combine(now.date(), window.end, tzinfo=now.tzinfo)
    remain = max(0, int((end - now).total_seconds()) - 120)
    return min(rng.randint(0, minutes * 60), remain)


# ---------------------------------------------------------------- 电源
def on_ac_power() -> bool:
    class PowerStatus(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte),
            ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte),
            ("Reserved1", ctypes.c_ubyte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    status = PowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):  # type: ignore[attr-defined]
        log.warning("拿不到电源状态，按「接着电」处理")
        return True
    # 255 = 未知（台式机/虚拟机），也放行
    return status.ACLineStatus in (1, 255)


# ---------------------------------------------------------------- 熔断
def _breaker_path(cfg: cfgmod.Config) -> Path:
    return cfg.paths.state / "breaker.json"


def breaker_reason(cfg: cfgmod.Config) -> str | None:
    """今天已经熔断过就返回原因；跨天自动失效。"""
    data = read_json(_breaker_path(cfg), None) or {}
    if data.get("date") != now_in(cfg).date().isoformat():
        return None
    return data.get("reason") or None


def trip_breaker(cfg: cfgmod.Config, reason: str) -> None:
    today = now_in(cfg).date().isoformat()
    atomic_write_json(
        _breaker_path(cfg),
        {"date": today, "reason": reason, "tripped_at": now_in(cfg).isoformat(timespec="seconds")},
    )
    log.error("熔断：%s（今天不再跑）", reason)


# ---------------------------------------------------------------- 去重
def _seen_path(cfg: cfgmod.Config) -> Path:
    return cfg.paths.state / "seen.json"


def seen_bvids(cfg: cfgmod.Config, days: int | None = None) -> set[str]:
    """days=None 表示「看过就永远算看过」；给天数则只看最近这么多天。"""
    data = read_json(_seen_path(cfg), {}) or {}
    if not days:
        return set(data)
    cutoff = time.time() - days * 86400
    keep: set[str] = set()
    for bvid, stamp in data.items():
        try:
            ts = datetime.fromisoformat(str(stamp)).timestamp()
        except (TypeError, ValueError):
            ts = 0
        if ts >= cutoff:
            keep.add(bvid)
    return keep


def mark_seen(cfg: cfgmod.Config, bvids: list[str]) -> None:
    data = read_json(_seen_path(cfg), {}) or {}
    stamp = now_in(cfg).isoformat(timespec="seconds")
    for bvid in bvids:
        data[bvid] = stamp
    # 只留最近 3000 条，文件不无限长
    if len(data) > 3000:
        for key in sorted(data, key=lambda k: data[k])[: len(data) - 3000]:
            data.pop(key, None)
    atomic_write_json(_seen_path(cfg), data)


# ---------------------------------------------------------------- 每日计数
def _daily_path(cfg: cfgmod.Config) -> Path:
    return cfg.paths.state / "daily.json"


def daily(cfg: cfgmod.Config) -> dict:
    today = now_in(cfg).date().isoformat()
    data = read_json(_daily_path(cfg), None) or {}
    if data.get("date") != today:
        data = {"date": today, "counts": {}}
        atomic_write_json(_daily_path(cfg), data)
    return data


def hard_cap(cfg: cfgmod.Config, kind: str) -> int | None:
    """这一类动作的绝对上限（特殊请求也不能突破）。"""
    return cfg.hard_limits.get(kind)


def hard_left(cfg: cfgmod.Config, kind: str) -> int | None:
    cap = hard_cap(cfg, kind)
    if cap is None:
        return None
    used = int(daily(cfg)["counts"].get(kind, 0))
    return max(0, cap - used)


def budget_left(cfg: cfgmod.Config, kind: str, *, special: bool = False) -> int:
    """当天这一类还剩几条额度。

    special=True 表示「白名单的特殊请求」：跳过日常预算，只受硬上限约束。
    """
    used = int(daily(cfg)["counts"].get(kind, 0))
    cap_left = hard_left(cfg, kind)

    if special:
        return 10**6 if cap_left is None else cap_left

    limit = cfg.guard.budget.get(kind)
    if limit is None:
        return 0 if cap_left is None else cap_left
    normal_left = max(0, limit.high - used)
    return normal_left if cap_left is None else min(normal_left, cap_left)


def record(cfg: cfgmod.Config, kind: str, *, special: bool = False) -> None:
    """记一次动作。special 时额外记一笔，日报里标「特批」。"""
    bump(cfg, kind)
    if special:
        bump(cfg, f"special_{kind}")


def at_today(cfg: cfgmod.Config, uid: int) -> int:
    """今天已经 @ 过这个人几次。"""
    return int(daily(cfg)["counts"].get(f"at_{uid}", 0))


def bump_at(cfg: cfgmod.Config, uid: int) -> None:
    bump(cfg, f"at_{uid}")


def bump(cfg: cfgmod.Config, kind: str) -> None:
    data = daily(cfg)
    data["counts"][kind] = data["counts"].get(kind, 0) + 1
    atomic_write_json(_daily_path(cfg), data)


# ---------------------------------------------------------------- 沉默衰减
# 连续几天没人理它，就少说两句——这是"宠物有情绪"，也是省额度。
def _interaction_path(cfg: cfgmod.Config) -> Path:
    return cfg.paths.state / "interaction.json"


def touch_interaction(cfg: cfgmod.Config) -> None:
    atomic_write_json(_interaction_path(cfg), {"last_user_at": int(time.time())})


def silence_days(cfg: cfgmod.Config) -> int:
    data = read_json(_interaction_path(cfg), {}) or {}
    last = int(data.get("last_user_at") or 0)
    if not last:
        return 0
    return max(0, int((time.time() - last) // 86400))


# ---------------------------------------------------------------- 暂停
# 安全优先：一旦暂停，只有管理员能解除。
def _pause_path(cfg: cfgmod.Config) -> Path:
    return cfg.paths.state / "pause.json"


def pause_until(cfg: cfgmod.Config) -> int:
    data = read_json(_pause_path(cfg), {}) or {}
    return int(data.get("until") or 0)


def is_paused(cfg: cfgmod.Config) -> bool:
    return pause_until(cfg) > int(time.time())


def pause(cfg: cfgmod.Config, hours: float, *, by: int) -> int:
    until = int(time.time() + hours * 3600)
    atomic_write_json(_pause_path(cfg), {"until": until, "by": by, "at": int(time.time())})
    log.info("暂停到 %s（by uid=%s）", datetime.fromtimestamp(until), by)
    return until


def unpause(cfg: cfgmod.Config) -> None:
    try:
        _pause_path(cfg).unlink()
    except OSError:
        pass
    log.info("已解除暂停")


# ---------------------------------------------------------------- 单实例锁
@dataclass
class Lock:
    path: Path

    def touch(self) -> None:
        """常驻进程要定期刷新锁的时间戳，否则会被当成「陈旧锁」被接管。"""
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """Windows 上判断进程还在不在（os.kill(pid, 0) 在这里不可用）。"""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:  # noqa: BLE001 - 判断不了就当成"还活着"，宁可保守
        return True


def acquire_lock(cfg: cfgmod.Config) -> Lock | None:
    """单实例。判断顺序：锁里的进程还在吗 → 锁多久没刷新 → 都过了才接管。

    常驻进程崩溃后被任务计划 5 分钟后拉起时，靠 PID 判断可以立刻接管，
    不用等 LOCK_STALE_SECONDS（否则会白等 3 小时）。
    """
    path = cfg.paths.state / "run.lock"
    if path.exists():
        holder = int((read_json(path, {}) or {}).get("pid") or 0)
        if holder and holder != os.getpid() and _pid_alive(holder):
            log.warning("已有实例在运行（pid=%s），本次跳过", holder)
            return None
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = LOCK_STALE_SECONDS + 1
        if age < LOCK_STALE_SECONDS and holder and not _pid_alive(holder):
            log.warning("锁的持有进程 %s 已不在，接管", holder)
        elif age < LOCK_STALE_SECONDS:
            log.warning("已有一次运行在进行（锁 %.0f 秒前），本次跳过", age)
            return None
        else:
            log.warning("发现陈旧锁（%.0f 秒前），接管", age)
    atomic_write_json(path, {"pid": os.getpid(), "started_at": datetime.now().isoformat(timespec="seconds")})
    return Lock(path)


# ---------------------------------------------------------------- 汇总
def preflight(cfg: cfgmod.Config) -> tuple[bool, str]:
    if cfg.guard.require_ac_power and not on_ac_power():
        return False, "没插电"
    if cfg.guard.network_preflight:
        ok, why = network_ok()
        if not ok:
            return False, why
    return True, "ok"
