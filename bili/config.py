"""读取 config.toml，并提供唯一的密钥出口。

设计上的两条硬规矩：
1. 配置有问题就当场炸，不要带着半截配置跑；
2. 密钥只存字面量、只经 ``resolve_llm_key()`` 取，任何 repr / 日志 / 异常都不带它。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]

_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{8,}$")
_MASK = "***"


class ConfigError(RuntimeError):
    """配置不合法。"""


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    def contains(self, t: time) -> bool:
        return self.start <= t <= self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


@dataclass(frozen=True)
class Budget:
    low: int
    high: int

    def __post_init__(self) -> None:
        if self.low < 0 or self.high < self.low:
            raise ConfigError(f"预算区间不合法: [{self.low}, {self.high}]")

    def __str__(self) -> str:
        return f"{self.low}~{self.high}"


@dataclass(frozen=True)
class Account:
    uid: int
    name: str


@dataclass(frozen=True)
class Schedule:
    windows: tuple[Window, ...]
    jitter_minutes: int


@dataclass(frozen=True)
class CommentSchedule:
    """评论有独立节拍，和完整流程解耦。"""

    interval_minutes: int = 120
    chance: float = 0.65

    def __post_init__(self) -> None:
        if self.interval_minutes < 10:
            raise ConfigError("[comment].interval_minutes 至少 10 分钟")
        if not 0.0 <= self.chance <= 1.0:
            raise ConfigError("[comment].chance 必须在 0~1")


@dataclass(frozen=True)
class ReplySchedule:
    """楼中楼回复：只回白名单里的人。"""

    enabled: bool = True
    interval_minutes: int = 15
    per_person_per_day: int = 5
    max_per_tick: int = 2
    like_replies: bool = True


@dataclass(frozen=True)
class StrangerReply:
    """陌生人回复：只回一次，不展开对话。"""

    enabled: bool = True
    per_day: int = 3
    per_person_per_day: int = 1
    only_questions: bool = True


@dataclass(frozen=True)
class TasteConfig:
    """口味画像打分开关与阈值。"""

    enabled: bool = True
    min_score_admin: float = 0.0
    min_score_ours: float = 1.0
    min_score_self: float = 1.5


@dataclass(frozen=True)
class Seed:
    """养推荐流：每天真播几条目标分区的视频。"""

    enabled: bool = True
    per_day: int = 3
    seconds: int = 25
    interval_minutes: int = 240


@dataclass(frozen=True)
class Watch:
    """发评论前真播多少秒。"""

    enabled: bool = True
    seconds: int = 30
    quality: int = 16

    def __post_init__(self) -> None:
        if self.seconds < 0 or self.seconds > 300:
            raise ConfigError("[watch].seconds 应在 0~300")


@dataclass(frozen=True)
class Hourly:
    """每小时天花板（短时频率才是风控真正看的）。0 = 不限。"""

    read: int = 300
    write: int = 12
    llm: int = 30


@dataclass(frozen=True)
class Guard:
    require_ac_power: bool
    network_preflight: bool
    budget: dict[str, Budget]
    hourly: Hourly = Hourly()


@dataclass(frozen=True)
class Llm:
    provider: str
    base_url: str
    model: str
    max_calls_per_run: int
    temperature: float
    api_key: str = ""

    def __repr__(self) -> str:  # 永不让密钥进 repr
        where = "set" if self.api_key else "unset"
        return (
            f"Llm(provider={self.provider!r}, model={self.model!r}, "
            f"api_key={_MASK}({where}))"
        )


@dataclass(frozen=True)
class P1:
    feed_pages: int
    coarse_keep: int
    fine_keep: int
    max_comment_drafts: int
    seen_days: int = 7


@dataclass(frozen=True)
class Paths:
    state: Path
    logs: Path
    reports: Path


@dataclass(frozen=True)
class Targets:
    """私信 / @ 的白名单。只有管理员一个 UID，别人谁来都不行。"""

    admin_uid: int
    report_mail: str

    @property
    def uids(self) -> tuple[int, ...]:
        return (self.admin_uid,)

    def is_allowed(self, uid: int) -> bool:
        return uid == self.admin_uid

    def name_of(self, uid: int) -> str:
        if uid == self.admin_uid:
            return "主人"
        raise ValueError(f"UID {uid} 不在白名单里")


@dataclass(frozen=True)
class Mail:
    server: str
    port: int
    use_ssl: bool
    enabled: bool
    username: str = ""
    password: str = field(default="", repr=False)

    def __repr__(self) -> str:  # 授权码永不进 repr
        state = "ready" if self.ready else "incomplete"
        return f"Mail(server={self.server!r}, port={self.port}, {state})"

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.server and self.username and self.password)


@dataclass(frozen=True)
class HardLimits:
    """绝对上限：特殊请求也不能突破。"""

    limits: dict[str, int] = field(default_factory=dict)

    def get(self, kind: str) -> int | None:
        return self.limits.get(kind)


@dataclass(frozen=True)
class Speak:
    """发言节奏：每个时间段的头尾各一波，一波最多几条，两条之间隔多久。"""

    enabled: bool
    per_batch: int
    reply_per_run: int
    gap_low: int
    gap_high: int
    chat_chance: float = 0.45
    share_chance: float = 0.55
    min_share_score: int = 12
    silence_half_days: int = 3
    silence_zero_days: int = 7
    sync_dynamic: bool = True
    comment_chance: float = 0.5
    at_per_person: int = 2

    def __post_init__(self) -> None:
        if self.per_batch < 0 or self.reply_per_run < 0:
            raise ConfigError("speak 的条数不能是负数")
        if self.gap_low < 0 or self.gap_high < self.gap_low:
            raise ConfigError("speak.gap_seconds 区间不合法")
        for name in ("chat_chance", "share_chance"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"speak.{name} 必须在 0~1 之间")


@dataclass(frozen=True)
class Session:
    """一次「开机会话」跑多久、多久看一次私信。"""

    minutes: int
    dm_poll_seconds: int
    poll_jitter_seconds: int = 15
    limit_check_every: int = 20

    def __post_init__(self) -> None:
        if self.minutes < 0 or self.dm_poll_seconds < 10:
            raise ConfigError("[session] 参数不合理（minutes>=0, dm_poll_seconds>=10）")
        if self.poll_jitter_seconds < 0:
            raise ConfigError("[session].poll_jitter_seconds 不能是负数")


@dataclass(frozen=True)
class Daemon:
    """常驻模式：开机起、关机止，中间定时跑完整流程 + 一直盯私信。"""

    enabled: bool
    main_interval_minutes: int

    def __post_init__(self) -> None:
        if self.main_interval_minutes < 1:
            raise ConfigError("[daemon].main_interval_minutes 至少 1 分钟")


@dataclass(frozen=True)
class Actions:
    """点赞/收藏/投币的节奏参数。

    *_chance 是「这一轮真的动手」的概率：每日预算是天花板，不是配额。
    """

    fav_folder: str
    engage_gap_low: int
    engage_gap_high: int
    like_chance: float = 0.55
    favorite_chance: float = 0.35

    def __post_init__(self) -> None:
        if self.engage_gap_low < 0 or self.engage_gap_high < self.engage_gap_low:
            raise ConfigError("[actions].engage_gap_seconds 区间不合法")
        for name in ("like_chance", "favorite_chance"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ConfigError(f"[actions].{name} 必须在 0~1 之间")


@dataclass(frozen=True)
class Config:
    root: Path
    account: Account
    mode: str
    timezone: str
    schedule: Schedule
    guard: Guard
    hard_limits: HardLimits
    llm: Llm
    p1: P1
    paths: Paths
    targets: Targets
    mail: Mail
    speak: Speak
    comment: CommentSchedule
    reply: ReplySchedule
    strangers: StrangerReply
    watch: Watch
    seed: Seed
    taste: TasteConfig
    session: Session
    daemon: Daemon
    actions: Actions
    log_level: str


def _get(table: dict, path: str, kind, *, default=None, required=True):
    cur = table
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            if required:
                raise ConfigError(f"config.toml 缺少 {path}")
            return default
        cur = cur[part]
    if not isinstance(cur, kind):
        raise ConfigError(f"config.toml 的 {path} 类型不对：期望 {kind.__name__}")
    return cur


def _parse_window(raw) -> Window:
    if not (isinstance(raw, list) and len(raw) == 2):
        raise ConfigError(f"时间窗格式应为 [开始, 结束]，实际 {raw!r}")
    try:
        start, end = time.fromisoformat(str(raw[0])), time.fromisoformat(str(raw[1]))
    except ValueError as exc:
        raise ConfigError(f"时间窗无法解析: {raw!r}") from exc
    if end <= start:
        raise ConfigError(f"时间窗结束必须晚于开始: {raw!r}")
    return Window(start, end)


def _parse_budget(raw, name: str) -> Budget:
    if not (isinstance(raw, list) and len(raw) == 2 and all(isinstance(x, int) for x in raw)):
        raise ConfigError(f"[guard.daily_budget].{name} 应为 [下限, 上限]")
    return Budget(raw[0], raw[1])


def _parse_hourly(raw: dict) -> Hourly:
    block = _get(raw, "guard.hourly", dict, required=False, default={})
    return Hourly(
        read=_get(block, "read", int, required=False, default=300),
        write=_get(block, "write", int, required=False, default=12),
        llm=_get(block, "llm", int, required=False, default=30),
    )


def _parse_hard_limits(raw: dict) -> HardLimits:
    guard_block = _get(raw, "guard", dict, required=False, default={})
    block = _get(guard_block, "hard_limits", dict, required=False, default={})
    limits = {str(k): int(v) for k, v in block.items() if isinstance(v, (int, float))}
    return HardLimits(limits=limits)


def _parse_speak(raw: dict) -> Speak:
    block = _get(raw, "speak", dict, required=False, default={})
    gap = block.get("gap_seconds") or [60, 150]
    if not (isinstance(gap, list) and len(gap) == 2):
        raise ConfigError("[speak].gap_seconds 应为 [最小, 最大] 秒")
    return Speak(
        enabled=_get(block, "enabled", bool, required=False, default=True),
        per_batch=_get(block, "per_batch", int, required=False, default=1),
        reply_per_run=_get(block, "reply_per_run", int, required=False, default=2),
        gap_low=int(gap[0]),
        gap_high=int(gap[1]),
        chat_chance=float(_get(block, "chat_chance", (int, float), required=False, default=0.45)),
        share_chance=float(_get(block, "share_chance", (int, float), required=False, default=0.55)),
        min_share_score=_get(block, "min_share_score", int, required=False, default=12),
        silence_half_days=_get(block, "silence_half_days", int, required=False, default=3),
        silence_zero_days=_get(block, "silence_zero_days", int, required=False, default=7),
        sync_dynamic=_get(block, "sync_dynamic", bool, required=False, default=True),
        comment_chance=float(_get(block, "comment_chance", (int, float), required=False, default=0.5)),
        at_per_person=_get(block, "at_per_person", int, required=False, default=2),
    )


def _parse_comment(raw: dict) -> CommentSchedule:
    block = _get(raw, "comment", dict, required=False, default={})
    return CommentSchedule(
        interval_minutes=_get(block, "interval_minutes", int, required=False, default=120),
        chance=float(_get(block, "chance", (int, float), required=False, default=0.65)),
    )


def _parse_reply(raw: dict) -> ReplySchedule:
    block = _get(raw, "reply", dict, required=False, default={})
    return ReplySchedule(
        enabled=_get(block, "enabled", bool, required=False, default=True),
        interval_minutes=_get(block, "interval_minutes", int, required=False, default=15),
        per_person_per_day=_get(block, "per_person_per_day", int, required=False, default=5),
        max_per_tick=_get(block, "max_per_tick", int, required=False, default=2),
        like_replies=_get(block, "like_replies", bool, required=False, default=True),
    )


def _parse_strangers(raw: dict) -> StrangerReply:
    reply_block = _get(raw, "reply", dict, required=False, default={})
    block = _get(reply_block, "strangers", dict, required=False, default={})
    return StrangerReply(
        enabled=_get(block, "enabled", bool, required=False, default=True),
        per_day=_get(block, "per_day", int, required=False, default=3),
        per_person_per_day=_get(block, "per_person_per_day", int, required=False, default=1),
        only_questions=_get(block, "only_questions", bool, required=False, default=True),
    )


def _parse_taste(raw: dict) -> TasteConfig:
    block = _get(raw, "taste", dict, required=False, default={})
    return TasteConfig(
        enabled=_get(block, "enabled", bool, required=False, default=True),
        min_score_admin=float(_get(block, "min_score_admin", (int, float), required=False, default=0.0)),
            min_score_ours=float(_get(block, "min_score_ours", (int, float), required=False, default=1.0)),
        min_score_self=float(_get(block, "min_score_self", (int, float), required=False, default=1.5)),
    )


def _parse_seed(raw: dict) -> Seed:
    block = _get(raw, "seed", dict, required=False, default={})
    return Seed(
        enabled=_get(block, "enabled", bool, required=False, default=True),
        per_day=_get(block, "per_day", int, required=False, default=3),
        seconds=_get(block, "seconds", int, required=False, default=25),
        interval_minutes=_get(block, "interval_minutes", int, required=False, default=240),
    )


def _parse_watch(raw: dict) -> Watch:
    block = _get(raw, "watch", dict, required=False, default={})
    return Watch(
        enabled=_get(block, "enabled", bool, required=False, default=True),
        seconds=_get(block, "seconds", int, required=False, default=30),
        quality=_get(block, "quality", int, required=False, default=16),
    )


def _parse_session(raw: dict) -> Session:
    block = _get(raw, "session", dict, required=False, default={})
    return Session(
        minutes=_get(block, "minutes", int, required=False, default=0),
        dm_poll_seconds=_get(block, "dm_poll_seconds", int, required=False, default=45),
        poll_jitter_seconds=_get(block, "poll_jitter_seconds", int, required=False, default=15),
        limit_check_every=_get(block, "limit_check_every", int, required=False, default=20),
    )


def _parse_daemon(raw: dict) -> Daemon:
    block = _get(raw, "daemon", dict, required=False, default={})
    return Daemon(
        enabled=_get(block, "enabled", bool, required=False, default=False),
        main_interval_minutes=_get(block, "main_interval_minutes", int, required=False, default=360),
    )


def _parse_actions(raw: dict) -> Actions:
    block = _get(raw, "actions", dict, required=False, default={})
    gap = block.get("engage_gap_seconds") or [20, 45]
    if not (isinstance(gap, list) and len(gap) == 2):
        raise ConfigError("[actions].engage_gap_seconds 应为 [最小, 最大] 秒")
    return Actions(
        fav_folder=_get(block, "fav_folder", str, required=False, default="默认收藏夹"),
        engage_gap_low=int(gap[0]),
        engage_gap_high=int(gap[1]),
        like_chance=float(_get(block, "like_chance", (int, float), required=False, default=0.55)),
        favorite_chance=float(
            _get(block, "favorite_chance", (int, float), required=False, default=0.35)
        ),
    )


def resolve_root() -> Path:
    env = os.environ.get("BILI_PET_ROOT")
    return Path(env).resolve() if env else ROOT


def load(root: Path | None = None) -> Config:
    root = (root or resolve_root()).resolve()
    path = root / "config.toml"
    if not path.exists():
        raise ConfigError(f"找不到 {path}")
    with path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config.toml 语法错误: {exc}") from exc

    mode = _get(raw, "run.mode", str)
    if mode != "shadow":
        raise ConfigError(f"run.mode={mode!r}：目前只实现了 shadow（只读、不发任何东西）")

    tz = _get(raw, "run.timezone", str)
    try:
        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001 - 时区名写错就是配置错
        raise ConfigError(f"未知时区 {tz!r}") from exc

    windows_raw = _get(raw, "schedule.windows", list, required=False, default=[])
    windows = tuple(_parse_window(w) for w in windows_raw)
    # 空列表 = 不设固定钟点，只靠开机触发

    budget_raw = _get(raw, "guard.daily_budget", dict)
    budget = {name: _parse_budget(v, name) for name, v in budget_raw.items()}

    cfg = Config(
        root=root,
        account=_parse_account(raw, root),
        mode=mode,
        timezone=tz,
        schedule=Schedule(
            windows=windows,
            jitter_minutes=_get(raw, "schedule.jitter_minutes", int),
        ),
        guard=Guard(
            require_ac_power=_get(raw, "guard.require_ac_power", bool),
            network_preflight=_get(raw, "guard.network_preflight", bool),
            budget=budget,
            hourly=_parse_hourly(raw),
        ),
        hard_limits=_parse_hard_limits(raw),
        llm=Llm(
            provider=_get(raw, "llm.provider", str),
            base_url=_get(raw, "llm.base_url", str).rstrip("/"),
            model=_get(raw, "llm.model", str),
            max_calls_per_run=_get(raw, "llm.max_calls_per_run", int),
            temperature=float(_get(raw, "llm.temperature", (int, float))),
            api_key=resolve_llm_key(root),
        ),
        p1=P1(
            feed_pages=_get(raw, "p1.feed_pages", int),
            coarse_keep=_get(raw, "p1.coarse_keep", int),
            fine_keep=_get(raw, "p1.fine_keep", int),
            max_comment_drafts=_get(raw, "p1.max_comment_drafts", int),
            seen_days=_get(raw, "p1.seen_days", int, required=False, default=7),
        ),
        paths=Paths(
            state=root / _get(raw, "paths.state", str),
            logs=root / _get(raw, "paths.logs", str),
            reports=root / _get(raw, "paths.reports", str),
        ),
        targets=_parse_targets(raw, root),
        mail=_parse_mail(raw, root),
        speak=_parse_speak(raw),
        comment=_parse_comment(raw),
        reply=_parse_reply(raw),
        strangers=_parse_strangers(raw),
        watch=_parse_watch(raw),
        seed=_parse_seed(raw),
        taste=_parse_taste(raw),
        session=_parse_session(raw),
        daemon=_parse_daemon(raw),
        actions=_parse_actions(raw),
        log_level=_get(raw, "logging.level", str),
    )
    return cfg


def _parse_account(raw: dict, root: Path) -> Account:
    """账号 UID / 昵称：config.toml 里是占位符，真实值放 secrets.toml。"""
    secret = (_read_secrets(root).get("account") or {})
    uid = int(secret.get("uid") or _get(raw, "account.uid", int, required=False, default=0))
    name = str(secret.get("name") or _get(raw, "account.name", str, required=False, default=""))
    if uid <= 0:
        raise ConfigError(
            "没有账号 UID：在 secrets.toml 的 [account].uid 填宠物号 UID（config.toml 里是占位符）"
        )
    return Account(uid=uid, name=name)


def _parse_targets(raw: dict, root: Path) -> Targets:
    """白名单 UID / 接收邮箱：同样在 secrets.toml 里填真实值。"""
    targets = _get(raw, "targets", dict, required=False, default={})
    secret = (_read_secrets(root).get("targets") or {})

    admin_uid = int(secret.get("admin_uid") or _get(targets, "admin_uid", int, required=False, default=0))
    if admin_uid <= 0:
        raise ConfigError("管理员 UID 没填：在 secrets.toml 的 [targets] 里填 admin_uid")

    report_mail = str(
        secret.get("report_mail") or _get(targets, "report_mail", str, required=False, default="")
    )
    if "@" not in report_mail:
        raise ConfigError("report_mail 没填或不像邮箱：在 secrets.toml 的 [targets] 里填")
    return Targets(admin_uid=admin_uid, report_mail=report_mail)


def _parse_mail(raw: dict, root: Path) -> Mail:
    """服务器参数在 config.toml，账号与授权码在 secrets.toml。"""
    block = _get(raw, "mail", dict, required=False, default={})
    secret = (_read_secrets(root).get("mail") or {})
    return Mail(
        server=_get(block, "server", str, required=False, default=""),
        port=_get(block, "port", int, required=False, default=465),
        use_ssl=_get(block, "use_ssl", bool, required=False, default=True),
        enabled=_get(block, "enabled", bool, required=False, default=False),
        username=str(secret.get("username", "")),
        password=str(secret.get("password", "")),
    )


def _looks_like_key(text: str) -> str:
    token = text.strip().strip('"').strip("'")
    return token if _KEY_RE.match(token) else ""


def _read_secrets(root: Path) -> dict:
    """secrets.toml：密钥与授权码唯一落脚点。文件不存在不算错（可以用环境变量）。"""
    path = root / "secrets.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"secrets.toml 语法错误：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"读不了 secrets.toml：{type(exc).__name__}") from exc


def resolve_llm_key(root: Path) -> str:
    """按约定顺序找密钥。只返回字符串，绝不打印。"""
    env = _looks_like_key(os.environ.get("BILI_PET_LLM_KEY", ""))
    if env:
        return env

    key = _looks_like_key(str(_read_secrets(root).get("llm", {}).get("api_key", "")))
    if key:
        return key

    envfile = root / ".env"
    if envfile.exists():
        try:
            for line in envfile.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == "BILI_PET_LLM_KEY":
                    key = _looks_like_key(value)
                    if key:
                        return key
        except OSError:
            pass
    return ""


def mask(secret: str) -> str:
    """日志/展示用：只留前缀，长度也不泄露。"""
    if not secret:
        return "<unset>"
    return f"{secret[:6]}{_MASK}(len={len(secret)})"
