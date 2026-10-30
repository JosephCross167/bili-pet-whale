"""守护进程的守护进程：不动计划任务也能自愈。

为什么不用计划任务：注册 `BiliPet-Watchdog` 需要管理员权限，
而 `BiliPet-Boot` 只在开机时触发一次——进程中途被结束就没人管了。

这个 supervisor 是一个常驻的小循环：每 5 分钟检查一次 daemon 是否还活着，
死了就拉起来。它自己很轻（一次只读一个 json + 查一个 pid）。

开机启动：把 `scripts/start-supervisor.vbs` 放进 shell:startup 即可，不需要管理员。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bili import config, guard, watchdog  # noqa: E402

CHECK_SECONDS = int(os.environ.get("BILI_SUPERVISOR_INTERVAL", "300"))


def _log(cfg, message: str) -> None:
    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    with (cfg.paths.logs / "supervisor.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def main() -> int:
    cfg = config.load()

    # 防止重复：如果 15 分钟内还有另一个 supervisor 在心跳，就退出
    beat_path = cfg.paths.state / "supervisor.json"
    try:
        import json

        beat = json.loads(beat_path.read_text(encoding="utf-8"))
        other = int(beat.get("pid") or 0)
        age = time.time() - float(beat.get("at") or 0)
        if other and other != os.getpid() and age < 900 and guard._pid_alive(other):
            _log(cfg, f"已有 supervisor 在跑（pid={other}，{age:.0f} 秒前的心跳），本次退出")
            return 0
    except Exception:  # noqa: BLE001 - 没有心跳文件是正常的
        pass

    _log(cfg, f"supervisor 启动，pid={os.getpid()}，每 {CHECK_SECONDS} 秒检查一次")

    while True:
        try:
            # 写心跳：它自己死了，下一次启动能看出最后活着的时间
            try:
                import json as _json

                atomic_write_json = _json.dumps({"pid": os.getpid(), "at": time.time()}, ensure_ascii=False)
                beat_path.write_text(atomic_write_json, encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

            alive, pid = watchdog.daemon_alive(cfg)
            if not alive:
                if pid:
                    stale = cfg.paths.state / "run.lock"
                    if stale.exists():
                        try:
                            stale.unlink()
                        except OSError:
                            pass
                # 直接复用 watchdog 的启动逻辑（含每小时重启上限）
                code = watchdog.main()
                _log(cfg, f"检测到 daemon 已停止，拉起结果 code={code}")
            else:
                # 每次检查都记一行（5 分钟一次，成本可以忽略），
                # 这样 supervisor 自己死掉时能一眼看出最后活着的时间点
                _log(cfg, f"daemon 存活 pid={pid}")
        except Exception as exc:  # noqa: BLE001 - supervisor 自己不能死
            try:
                _log(cfg, f"检查出错：{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
