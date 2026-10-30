"""原子读写。

断电是常态（宿舍 23:30 断电），所以任何状态写入都走「临时文件 → fsync → rename」，
读坏了的 JSON 不抛异常，而是隔离掉并返回默认值 —— 宁可丢掉当天计数，也不要让脚本崩在半路。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("bilipet.store")

_ENCODING = "utf-8"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=_ENCODING, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        text = path.read_text(encoding=_ENCODING)
    except FileNotFoundError:
        return default
    except OSError as exc:
        log.warning("读取 %s 失败：%s，按默认值处理", path.name, type(exc).__name__)
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        bad = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.replace(bad)
            log.warning("%s 内容损坏，已隔离为 %s，本次按默认值继续", path.name, bad.name)
        except OSError:
            log.warning("%s 内容损坏且无法隔离，本次按默认值继续", path.name)
        return default
