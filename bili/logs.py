"""日志：写文件 + 控制台，所有输出先过一遍脱敏。

脱敏不是可选项：cookie 和 key 一旦进了日志，就等于进了日报、截图和 git 历史。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(SESSDATA=)[^;,\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(bili_jct=)[^;,\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(DedeUserID__ckMd5=)[^;,\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(buvid[^=]*=)[^;,\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+"), r"\1<redacted>"),
    (re.compile(r"(\"(?:api_key|token|password|secret)\"\s*:\s*\")[^\"]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                k: (redact(v) if isinstance(v, str) else v) for k, v in record.args.items()
            }
        return True


def setup(log_dir: Path, level: str = "INFO", *, console: bool = True) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("bilipet")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    root.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%m-%d %H:%M:%S")
    redactor = _RedactFilter()

    # 常驻进程会跨天，所以不能用「启动那天」当文件名：午夜自动切分。
    # 结果形如 bilipet.log（今天）+ bilipet.log.2026-10-26（昨天），保留 7 天。
    file_handler = TimedRotatingFileHandler(
        log_dir / "bilipet.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        stream.addFilter(redactor)
        root.addHandler(stream)

    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"bilipet.{name}")
