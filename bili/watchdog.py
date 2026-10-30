"""看门狗：检查常驻进程是否活着，死了就拉起来。

为什么需要：计划任务可能因为各种原因把常驻进程结束掉（实测 16:32 被强制终止，
任务返回码 0x40010004），而日志里不会留任何痕迹——最危险的状态是
「你以为它在跑，其实它死了」。

由计划任务每 10 分钟调用一次。自身很轻：读一个 json + 查一次 pid。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili import config, guard  # noqa: E402

STATE = "watchdog.json"
MAX_RESTARTS_PER_HOUR = 20


def _log_path(cfg) -> pathlib.Path:
    return cfg.paths.logs / "watchdog.log"


def _log(cfg, message: str) -> None:
    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    with _log_path(cfg).open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def _state_path(cfg) -> pathlib.Path:
    return cfg.paths.state / STATE


def _restart_budget(cfg) -> bool:
    """一小时内最多拉起几次，防止 daemon 秒崩时疯狂重启。"""
    path = _state_path(cfg)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    stamps = [t for t in data.get("restarts", []) if time.time() - t < 3600]
    if stamps and time.time() - max(stamps) < 60:
        _log(cfg, "距上次重启不足 60 秒，跳过（防抖）")
        return False
    if len(stamps) >= MAX_RESTARTS_PER_HOUR:
        _log(cfg, f"一小时内已重启 {len(stamps)} 次，本轮放弃")
        return False
    stamps.append(int(time.time()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"restarts": stamps}), encoding="utf-8")
    return True


def daemon_alive(cfg) -> tuple[bool, int]:
    lock = cfg.paths.state / "run.lock"
    if not lock.exists():
        return False, 0
    try:
        pid = int(json.loads(lock.read_text(encoding="utf-8")).get("pid") or 0)
    except Exception:  # noqa: BLE001
        return False, 0
    return (pid > 0 and guard._pid_alive(pid)), pid


def launch(cfg) -> int:
    exe = cfg.root / ".venv" / "Scripts" / "pythonw.exe"
    if not exe.exists():
        _log(cfg, f"找不到解释器：{exe}")
        return 0
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [str(exe), "runner.py", "--daemon"],
        cwd=str(cfg.root),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _log(cfg, f"已拉起 daemon，pid={proc.pid}")
    return proc.pid


def main() -> int:
    try:
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001 - 配置坏了也要能在 watchdog.log 里看到
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    alive, pid = daemon_alive(cfg)
    if alive:
        return 0

    stale = cfg.paths.state / "run.lock"
    if stale.exists():
        try:
            stale.unlink()
            _log(cfg, f"清理残留锁（pid={pid} 已不在）")
        except OSError:
            pass

    if not _restart_budget(cfg):
        return 1

    launch(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
