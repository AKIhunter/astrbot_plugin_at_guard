"""文字判定断线恢复的状态模型和最小持久化边界。"""
from __future__ import annotations

import math
import re
import socket
import sqlite3
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


COOLDOWN_SECONDS = 10 * 60
SCHEMA_VERSION = 2
MAX_HISTORY = 32


class OutageState(str, Enum):
    DISABLED = "disabled"
    HEALTHY = "healthy"
    COOLING = "cooling"
    PROBING = "probing"
    RECOVERING = "recovering"
    NEEDS_ATTENTION = "needs_attention"


LEGAL_TRANSITIONS = {
    OutageState.DISABLED: frozenset({OutageState.HEALTHY}),
    OutageState.HEALTHY: frozenset({
        OutageState.COOLING,
        OutageState.NEEDS_ATTENTION,
        OutageState.DISABLED,
    }),
    OutageState.COOLING: frozenset({OutageState.PROBING}),
    OutageState.PROBING: frozenset({
        OutageState.COOLING,
        OutageState.RECOVERING,
        OutageState.NEEDS_ATTENTION,
    }),
    OutageState.RECOVERING: frozenset({OutageState.HEALTHY}),
    OutageState.NEEDS_ATTENTION: frozenset({OutageState.PROBING}),
}
VALID_STATES = frozenset(state.value for state in OutageState)


class BackfillStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    NO_REPLY = "no_reply"
    RETRY_PENDING = "retry_pending"
    SEND_UNKNOWN = "send_unknown"
    UNAVAILABLE = "unavailable"


VALID_BACKFILL_STATUSES = frozenset(status.value for status in BackfillStatus)


class NotificationPhase(str, Enum):
    INTERRUPTED = "interrupted"
    CONTINUED = "continued"
    RECOVERED = "recovered"


VALID_NOTIFICATION_PHASES = frozenset(phase.value for phase in NotificationPhase)


class FailureDisposition(str, Enum):
    RECOVERABLE = "recoverable"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class ClassifierFailure:
    category: str
    disposition: FailureDisposition

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", _category(self.category))
        object.__setattr__(
            self,
            "disposition",
            self.disposition
            if isinstance(self.disposition, FailureDisposition)
            else FailureDisposition(str(self.disposition)),
        )


_RECOVERABLE_CATEGORIES = frozenset({
    "timeout",
    "connection_error",
    "http_429",
    "http_5xx",
    "empty_response",
    "protocol_error",
})
_ATTENTION_CATEGORIES = frozenset({
    "authentication_error",
    "provider_missing",
    "configuration_error",
})
_IGNORED_REASONS = frozenset({
    "allowed",
    "budget",
    "vision_budget",
    "too_short",
    "no_content",
    "below_threshold",
    "model_silent",
    "others_talking",
    "conversation_ended",
    "already_answered",
    "context_missing",
    "media_unresolved",
    "no_value",
    "directed",
    "continuation",
    "useful",
})
FAILURE_CATEGORY_LABELS = {
    "timeout": "请求超时",
    "connection_error": "连接失败",
    "http_429": "请求受限",
    "http_5xx": "上游暂不可用",
    "empty_response": "空响应",
    "protocol_error": "协议错误",
    "authentication_error": "认证失败",
    "provider_missing": "Provider 缺失",
    "configuration_error": "配置错误",
}


def _http_status(exc: BaseException) -> int | None:
    for candidate in _exception_chain(exc):
        value = getattr(candidate, "status_code", None)
        if value is None:
            response = getattr(candidate, "response", None)
            value = getattr(response, "status_code", None)
            if value is None:
                value = getattr(response, "status", None)
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    return None


def _exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(5):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify_classifier_failure(
    reason: str = "",
    *,
    exception: BaseException | None = None,
    category: str = "",
) -> ClassifierFailure | None:
    """Return only a bounded category; exception messages are never retained."""
    normalized = str(category or reason or "").strip().lower()
    aliases = {
        "deadline": "timeout",
        "parse_error": "protocol_error",
        "no_provider": "provider_missing",
        "provider_not_found": "provider_missing",
        "config_error": "configuration_error",
        "http_400": "configuration_error",
        "http_404": "configuration_error",
        "http_422": "configuration_error",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in _RECOVERABLE_CATEGORIES:
        return ClassifierFailure(normalized, FailureDisposition.RECOVERABLE)
    if normalized in _ATTENTION_CATEGORIES:
        return ClassifierFailure(normalized, FailureDisposition.NEEDS_ATTENTION)
    if normalized in _IGNORED_REASONS:
        return None
    if exception is None:
        return None

    status = _http_status(exception)
    if status in {401, 403}:
        return ClassifierFailure(
            "authentication_error", FailureDisposition.NEEDS_ATTENTION
        )
    if status == 429:
        return ClassifierFailure("http_429", FailureDisposition.RECOVERABLE)
    if status is not None and 500 <= status <= 599:
        return ClassifierFailure("http_5xx", FailureDisposition.RECOVERABLE)
    if status in {400, 404, 422}:
        return ClassifierFailure(
            "configuration_error", FailureDisposition.NEEDS_ATTENTION
        )

    chain = tuple(_exception_chain(exception))
    names = {type(item).__name__.lower() for item in chain}
    if (
        any(isinstance(item, (TimeoutError, socket.timeout)) for item in chain)
        or any("timeout" in name or "timedout" in name for name in names)
    ):
        return ClassifierFailure("timeout", FailureDisposition.RECOVERABLE)
    if any(
        isinstance(item, (ConnectionError, socket.gaierror, ssl.SSLError))
        for item in chain
    ) or any(
        any(token in name for token in (
            "connection",
            "connecterror",
            "networkerror",
            "dnserror",
            "sslerror",
            "tlserror",
        ))
        for name in names
    ):
        return ClassifierFailure("connection_error", FailureDisposition.RECOVERABLE)
    if any(
        any(token in name for token in (
            "authentication",
            "unauthorized",
            "permissiondenied",
            "forbidden",
        ))
        for name in names
    ):
        return ClassifierFailure(
            "authentication_error", FailureDisposition.NEEDS_ATTENTION
        )
    if any("ratelimit" in name or "toomanyrequests" in name for name in names):
        return ClassifierFailure("http_429", FailureDisposition.RECOVERABLE)
    if any(
        any(token in name for token in (
            "serviceunavailable",
            "internalserver",
            "badgateway",
        ))
        for name in names
    ):
        return ClassifierFailure("http_5xx", FailureDisposition.RECOVERABLE)
    if any(
        any(token in name for token in (
            "configuration",
            "configerror",
            "badrequest",
            "invalidrequest",
            "providernotfound",
        ))
        for name in names
    ) or any(isinstance(item, (KeyError, ValueError)) for item in chain):
        return ClassifierFailure(
            "configuration_error", FailureDisposition.NEEDS_ATTENTION
        )
    return None


def failure_category_label(category: str) -> str:
    return FAILURE_CATEGORY_LABELS.get(str(category or "").strip().lower(), "未知错误")


def redacted_provider_id(value: Any) -> str:
    """Keep a public provider label while refusing URLs and credential-like values."""
    text = str(value or "").strip()
    folded = text.casefold()
    if (
        not text
        or len(text) > 512
        or "://" in text
        or any(ord(character) < 32 for character in text)
        or re.search(r"(?:api[_-]?key|token|secret|bearer|sk-[a-z0-9])", folded)
    ):
        return "current_provider"
    return text


class InvalidOutageTransition(ValueError):
    pass


@dataclass
class OutageStateMachine:
    state: OutageState

    def __post_init__(self) -> None:
        self.state = _state(self.state)

    def transition(self, target: OutageState | str) -> OutageState:
        target = _state(target)
        if target not in LEGAL_TRANSITIONS[self.state]:
            raise InvalidOutageTransition(
                f"illegal outage transition: {self.state.value} -> {target.value}"
            )
        self.state = target
        return self.state


@dataclass
class AffectedSession:
    session_id: str
    first_affected_at: float
    last_affected_at: float
    backfill_status: BackfillStatus = BackfillStatus.PENDING
    updated_at: float = 0.0
    retry_not_before: float | None = None

    def __post_init__(self) -> None:
        self.session_id = _identifier(self.session_id, "session_id", 1024)
        self.first_affected_at = _timestamp(self.first_affected_at, "first_affected_at")
        self.last_affected_at = _timestamp(self.last_affected_at, "last_affected_at")
        self.updated_at = _timestamp(
            self.updated_at or self.last_affected_at, "updated_at"
        )
        self.retry_not_before = _optional_timestamp(
            self.retry_not_before, "retry_not_before"
        )
        self.backfill_status = _backfill_status(self.backfill_status)
        if self.last_affected_at < self.first_affected_at:
            raise ValueError("last_affected_at precedes first_affected_at")


@dataclass(frozen=True)
class PendingRecoverySession:
    session_id: str
    first_affected_at: float
    last_affected_at: float
    backfill_status: BackfillStatus
    retry_not_before: float | None


@dataclass(frozen=True)
class PendingRecoveryWork:
    """Task 5 可消费的只读窗口；不包含消息正文或上游响应。"""

    event_id: str
    window_start: float
    window_end: float
    provider_id: str
    error_category: str
    sessions: tuple[PendingRecoverySession, ...]


@dataclass(frozen=True)
class NotificationReceipt:
    phase: NotificationPhase
    cycle: int
    recipient_id: str
    sent_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", _notification_phase(self.phase))
        if isinstance(self.cycle, bool) or int(self.cycle) != self.cycle or self.cycle < 0:
            raise ValueError("cycle must be a non-negative integer")
        object.__setattr__(self, "cycle", int(self.cycle))
        object.__setattr__(
            self, "recipient_id", _identifier(self.recipient_id, "recipient_id", 256)
        )
        object.__setattr__(self, "sent_at", _timestamp(self.sent_at, "sent_at"))


@dataclass
class OutageEvent:
    event_id: str
    started_at: float
    state: OutageState
    error_category: str
    provider_id: str
    updated_at: float
    cooldown_until: float | None = None
    recovery_window_end: float | None = None
    ended_at: float | None = None
    affected_sessions: dict[str, AffectedSession] = field(default_factory=dict)
    notifications: list[NotificationReceipt] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", str(self.event_id)):
            raise ValueError("event_id must be 32 lowercase hex characters")
        self.started_at = _timestamp(self.started_at, "started_at")
        self.updated_at = _timestamp(self.updated_at, "updated_at")
        self.state = _state(self.state)
        self.error_category = _category(self.error_category)
        self.provider_id = _identifier(self.provider_id, "provider_id", 512)
        self.cooldown_until = _optional_timestamp(
            self.cooldown_until, "cooldown_until"
        )
        self.recovery_window_end = _optional_timestamp(
            self.recovery_window_end, "recovery_window_end"
        )
        self.ended_at = _optional_timestamp(self.ended_at, "ended_at")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        sessions = {}
        for session in self.affected_sessions.values():
            if not isinstance(session, AffectedSession):
                raise TypeError("affected_sessions must contain AffectedSession")
            sessions[session.session_id] = session
        self.affected_sessions = sessions
        self.notifications = [
            receipt if isinstance(receipt, NotificationReceipt)
            else NotificationReceipt(**receipt)
            for receipt in self.notifications
        ]


def _state(value: OutageState | str) -> OutageState:
    try:
        return value if isinstance(value, OutageState) else OutageState(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown outage state: {value!r}") from exc


def _backfill_status(value: BackfillStatus | str) -> BackfillStatus:
    try:
        return (
            value
            if isinstance(value, BackfillStatus)
            else BackfillStatus(str(value))
        )
    except ValueError as exc:
        raise ValueError(f"unknown backfill status: {value!r}") from exc


def _notification_phase(value: NotificationPhase | str) -> NotificationPhase:
    try:
        return (
            value
            if isinstance(value, NotificationPhase)
            else NotificationPhase(str(value))
        )
    except ValueError as exc:
        raise ValueError(f"unknown notification phase: {value!r}") from exc


def _timestamp(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite timestamp")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite timestamp") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field_name} must be a finite timestamp")
    return result


def _optional_timestamp(value: Any, field_name: str) -> float | None:
    return None if value is None else _timestamp(value, field_name)


def _identifier(value: Any, field_name: str, maximum: int) -> str:
    result = str(value or "").strip()
    if (
        not result
        or len(result) > maximum
        or "://" in result
        or any(ord(character) < 32 for character in result)
    ):
        raise ValueError(f"invalid {field_name}")
    return result


def _category(value: Any) -> str:
    result = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,79}", result):
        raise ValueError("error_category must be a redacted category code")
    return result


class OutageEventStore:
    """只保存恢复所需标识和状态；表中没有消息、prompt 或响应正文列。"""

    def __init__(self, data_dir: Path | str | None, logger: Any = None) -> None:
        self.path = (
            Path(data_dir) / "classifier_outage_recovery.sqlite3"
            if data_dir
            else None
        )
        self.logger = logger
        self.health = "memory" if self.path is None else "pending"
        self._memory: dict[str, OutageEvent] = {}
        self._lock = threading.RLock()
        if self.path is not None:
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.path is None:
            raise RuntimeError("persistent outage storage is unavailable")
        db = sqlite3.connect(str(self.path), timeout=3)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS outage_events (
                        event_id TEXT PRIMARY KEY,
                        schema_version INTEGER NOT NULL,
                        started_at REAL NOT NULL,
                        ended_at REAL,
                        state TEXT NOT NULL,
                        error_category TEXT NOT NULL,
                        provider_id TEXT NOT NULL,
                        cooldown_until REAL,
                        recovery_window_end REAL,
                        updated_at REAL NOT NULL
                    )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS affected_sessions (
                        event_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        first_affected_at REAL NOT NULL,
                        last_affected_at REAL NOT NULL,
                        backfill_status TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        retry_not_before REAL,
                        PRIMARY KEY (event_id, session_id),
                        FOREIGN KEY (event_id) REFERENCES outage_events(event_id)
                            ON DELETE CASCADE
                    )"""
                )
                columns = {
                    row[1]
                    for row in db.execute(
                        "PRAGMA table_info(affected_sessions)"
                    )
                }
                if "retry_not_before" not in columns:
                    db.execute(
                        "ALTER TABLE affected_sessions "
                        "ADD COLUMN retry_not_before REAL"
                    )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS notification_receipts (
                        event_id TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        cycle INTEGER NOT NULL,
                        recipient_id TEXT NOT NULL,
                        sent_at REAL NOT NULL,
                        PRIMARY KEY (event_id, phase, cycle, recipient_id),
                        FOREIGN KEY (event_id) REFERENCES outage_events(event_id)
                            ON DELETE CASCADE
                    )"""
                )
            self.path.chmod(0o600)
            self.health = "ready"
        except (OSError, sqlite3.Error):
            self.health = "unavailable"
            self._warn("断线恢复存储初始化失败")

    def _warn(self, message: str) -> None:
        callback = getattr(self.logger, "warning", None)
        if callable(callback):
            callback(f"[LivingHello] {message}")

    def save(self, event: OutageEvent) -> bool:
        if not isinstance(event, OutageEvent):
            raise TypeError("event must be OutageEvent")
        # 重新校验可防止调用方在构造后写入未受控的枚举或标识。
        event.__post_init__()
        if self.path is None:
            self._memory[event.event_id] = _copy_event(event)
            return True
        if self.health == "unavailable":
            return False
        try:
            with self._lock, self._connect() as db:
                db.execute(
                    """INSERT INTO outage_events VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        schema_version=excluded.schema_version,
                        started_at=excluded.started_at,
                        ended_at=excluded.ended_at,
                        state=excluded.state,
                        error_category=excluded.error_category,
                        provider_id=excluded.provider_id,
                        cooldown_until=excluded.cooldown_until,
                        recovery_window_end=excluded.recovery_window_end,
                        updated_at=excluded.updated_at""",
                    (
                        event.event_id,
                        SCHEMA_VERSION,
                        event.started_at,
                        event.ended_at,
                        event.state.value,
                        event.error_category,
                        event.provider_id,
                        event.cooldown_until,
                        event.recovery_window_end,
                        event.updated_at,
                    ),
                )
                db.execute(
                    "DELETE FROM affected_sessions WHERE event_id=?", (event.event_id,)
                )
                db.executemany(
                    "INSERT INTO affected_sessions VALUES (?,?,?,?,?,?,?)",
                    [
                        (
                            event.event_id,
                            session.session_id,
                            session.first_affected_at,
                            session.last_affected_at,
                            session.backfill_status.value,
                            session.updated_at,
                            session.retry_not_before,
                        )
                        for session in event.affected_sessions.values()
                    ],
                )
                db.execute(
                    "DELETE FROM notification_receipts WHERE event_id=?",
                    (event.event_id,),
                )
                db.executemany(
                    "INSERT INTO notification_receipts VALUES (?,?,?,?,?)",
                    [
                        (
                            event.event_id,
                            receipt.phase.value,
                            receipt.cycle,
                            receipt.recipient_id,
                            receipt.sent_at,
                        )
                        for receipt in event.notifications
                    ],
                )
                if event.ended_at is not None:
                    db.execute(
                        """DELETE FROM outage_events WHERE event_id IN (
                            SELECT event_id FROM outage_events
                            WHERE ended_at IS NOT NULL
                            ORDER BY ended_at DESC
                            LIMIT -1 OFFSET ?
                        )""",
                        (MAX_HISTORY,),
                    )
            self.health = "ready"
            return True
        except (OSError, sqlite3.Error):
            self.health = "unavailable"
            self._warn("断线恢复状态写入失败")
            return False

    def load_active(self) -> OutageEvent | None:
        if self.path is None:
            active = [
                event for event in self._memory.values() if event.ended_at is None
            ]
            return _copy_event(max(active, key=lambda row: row.started_at)) if active else None
        return self._load(
            "WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1", ()
        )

    def load_latest(self) -> OutageEvent | None:
        if self.path is None:
            if not self._memory:
                return None
            return _copy_event(
                max(self._memory.values(), key=lambda row: row.started_at)
            )
        return self._load("ORDER BY started_at DESC LIMIT 1", ())

    def load(self, event_id: str) -> OutageEvent | None:
        event_id = _identifier(event_id, "event_id", 32)
        if self.path is None:
            event = self._memory.get(event_id)
            return _copy_event(event) if event else None
        return self._load("WHERE event_id=?", (event_id,))

    def _load(self, clause: str, parameters: tuple[Any, ...]) -> OutageEvent | None:
        if self.health == "unavailable" or self.path is None or not self.path.exists():
            return None
        try:
            with self._lock, self._connect() as db:
                row = db.execute(
                    """SELECT event_id, started_at, state, error_category,
                        provider_id, updated_at, cooldown_until,
                        recovery_window_end, ended_at
                    FROM outage_events """
                    + clause,
                    parameters,
                ).fetchone()
                if row is None:
                    return None
                event_id = row[0]
                sessions = {
                    item[0]: AffectedSession(
                        session_id=item[0],
                        first_affected_at=item[1],
                        last_affected_at=item[2],
                        backfill_status=item[3],
                        updated_at=item[4],
                        retry_not_before=item[5],
                    )
                    for item in db.execute(
                        """SELECT session_id, first_affected_at, last_affected_at,
                            backfill_status, updated_at, retry_not_before
                        FROM affected_sessions WHERE event_id=?""",
                        (event_id,),
                    )
                }
                notifications = [
                    NotificationReceipt(
                        phase=item[0],
                        cycle=item[1],
                        recipient_id=item[2],
                        sent_at=item[3],
                    )
                    for item in db.execute(
                        """SELECT phase, cycle, recipient_id, sent_at
                        FROM notification_receipts WHERE event_id=?
                        ORDER BY sent_at, phase, recipient_id""",
                        (event_id,),
                    )
                ]
            return OutageEvent(
                event_id=row[0],
                started_at=row[1],
                state=row[2],
                error_category=row[3],
                provider_id=row[4],
                updated_at=row[5],
                cooldown_until=row[6],
                recovery_window_end=row[7],
                ended_at=row[8],
                affected_sessions=sessions,
                notifications=notifications,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self.health = "unavailable"
            self._warn("断线恢复状态读取失败")
            return None


class ClassifierOutageRecovery:
    """判定断线状态、最小持久化和通知节流的共享边界。"""

    def __init__(
        self,
        config: dict[str, Any] | None,
        data_dir: Path | str | None,
        logger: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config if config is not None else {}
        self.clock = clock
        self.store = OutageEventStore(data_dir, logger)
        self.current_event = self.store.load_active()
        if self.enabled:
            initial = (
                self.current_event.state
                if self.current_event is not None
                else OutageState.HEALTHY
            )
        else:
            initial = OutageState.DISABLED
        self.machine = OutageStateMachine(initial)

    @property
    def enabled(self) -> bool:
        return self.config.get("classifier_outage_recovery_enabled", False) is True

    @property
    def state(self) -> OutageState:
        return self.machine.state

    @property
    def blocks_normal_calls(self) -> bool:
        return self.state in {
            OutageState.COOLING,
            OutageState.PROBING,
            OutageState.NEEDS_ATTENTION,
        }

    def sync_enabled(self) -> OutageState:
        """配置保存后同步开关；进行中的故障不能被开关直接丢弃。"""
        if self.enabled:
            if self.state is OutageState.DISABLED:
                return self.transition(OutageState.HEALTHY)
            return self.state
        if self.state is OutageState.HEALTHY:
            return self.transition(OutageState.DISABLED)
        if self.state is not OutageState.DISABLED:
            raise InvalidOutageTransition(
                "cannot disable outage recovery while an event is active"
            )
        return self.state

    def create_event(
        self,
        error_category: str,
        provider_id: str,
        *,
        state: OutageState | str = OutageState.COOLING,
        at: float | None = None,
    ) -> OutageEvent:
        target = _state(state)
        if target not in {OutageState.COOLING, OutageState.NEEDS_ATTENTION}:
            raise ValueError("new outage must start in cooling or needs_attention")
        if not self.enabled:
            raise InvalidOutageTransition("outage recovery is disabled")
        if self.current_event is not None:
            raise InvalidOutageTransition("an outage event is already active")
        now = _timestamp(self.clock() if at is None else at, "at")
        event = OutageEvent(
            event_id=uuid.uuid4().hex,
            started_at=now,
            state=target,
            error_category=error_category,
            provider_id=provider_id,
            cooldown_until=now + COOLDOWN_SECONDS
            if target is OutageState.COOLING
            else None,
            updated_at=now,
        )
        self.machine.transition(target)
        self.current_event = event
        self.store.save(event)
        return event

    def transition(
        self, target: OutageState | str, *, at: float | None = None
    ) -> OutageState:
        target = _state(target)
        now = _timestamp(self.clock() if at is None else at, "at")
        if target in {OutageState.COOLING, OutageState.NEEDS_ATTENTION} and (
            self.current_event is None
        ):
            raise InvalidOutageTransition("create an outage event before this transition")
        previous = self.machine.state
        if (
            previous is OutageState.DISABLED
            and target is OutageState.HEALTHY
            and self.current_event is not None
        ):
            raise InvalidOutageTransition(
                "cannot discard a persisted outage while enabling recovery"
            )
        self.machine.transition(target)
        event = self.current_event
        if event is not None:
            event.state = target
            event.updated_at = now
            if target is OutageState.COOLING:
                event.cooldown_until = now + COOLDOWN_SECONDS
            elif (
                target is OutageState.RECOVERING
                and event.recovery_window_end is None
            ):
                event.recovery_window_end = now
                event.cooldown_until = None
            elif target is OutageState.HEALTHY:
                event.cooldown_until = None
                event.ended_at = now
            self.store.save(event)
            if target is OutageState.HEALTHY:
                self.current_event = None
        elif previous is OutageState.HEALTHY and target is OutageState.DISABLED:
            self.config["classifier_outage_recovery_enabled"] = False
        elif previous is OutageState.DISABLED and target is OutageState.HEALTHY:
            self.config["classifier_outage_recovery_enabled"] = True
        return self.machine.state

    def begin_probe(
        self,
        provider_id: str,
        *,
        force: bool = False,
        at: float | None = None,
    ) -> str | None:
        """Claim the active event for one probe and return its stale-write token."""
        event = self._active_event()
        now = _timestamp(self.clock() if at is None else at, "at")
        if self.state is OutageState.COOLING:
            if (
                not force
                and event.cooldown_until is not None
                and now < event.cooldown_until
            ):
                return None
            self.transition(OutageState.PROBING, at=now)
        elif self.state is OutageState.NEEDS_ATTENTION:
            if not force:
                return None
            self.transition(OutageState.PROBING, at=now)
        elif self.state is not OutageState.PROBING:
            raise InvalidOutageTransition(
                f"cannot probe outage in {self.state.value}"
            )
        event = self._active_event()
        event.provider_id = _identifier(provider_id, "provider_id", 512)
        event.updated_at = now
        self.store.save(event)
        return event.event_id

    def complete_probe_success(
        self, event_id: str, *, at: float | None = None
    ) -> PendingRecoveryWork:
        self._require_probe_event(event_id)
        self.transition(OutageState.RECOVERING, at=at)
        work = self.pending_recovery()
        if work is None:  # Defensive: RECOVERING always has a frozen upper bound.
            raise InvalidOutageTransition("recovery work was not frozen")
        return work

    def complete_probe_failure(
        self,
        event_id: str,
        failure: ClassifierFailure,
        *,
        at: float | None = None,
    ) -> OutageState:
        event = self._require_probe_event(event_id)
        if not isinstance(failure, ClassifierFailure):
            raise TypeError("failure must be ClassifierFailure")
        event.error_category = failure.category
        target = (
            OutageState.COOLING
            if failure.disposition is FailureDisposition.RECOVERABLE
            else OutageState.NEEDS_ATTENTION
        )
        return self.transition(target, at=at)

    def pending_recovery(self) -> PendingRecoveryWork | None:
        event = self.current_event
        if (
            self.state is not OutageState.RECOVERING
            or event is None
            or event.recovery_window_end is None
        ):
            return None
        now = _timestamp(self.clock(), "clock")
        all_sessions = sorted(
            event.affected_sessions.values(),
            key=lambda item: item.session_id,
        )
        sessions = tuple(
            PendingRecoverySession(
                session_id=session.session_id,
                first_affected_at=session.first_affected_at,
                last_affected_at=session.last_affected_at,
                backfill_status=session.backfill_status,
                retry_not_before=session.retry_not_before,
            )
            for session in all_sessions
            if (
                session.backfill_status
                in {BackfillStatus.PENDING, BackfillStatus.PROCESSING}
                or (
                    session.backfill_status is BackfillStatus.RETRY_PENDING
                    and session.retry_not_before is not None
                    and session.retry_not_before <= now
                )
            )
        )
        if all_sessions and not sessions:
            return None
        return PendingRecoveryWork(
            event_id=event.event_id,
            window_start=event.started_at,
            window_end=event.recovery_window_end,
            provider_id=event.provider_id,
            error_category=event.error_category,
            sessions=sessions,
        )

    def next_recovery_retry_at(self) -> float | None:
        event = self.current_event
        if self.state is not OutageState.RECOVERING or event is None:
            return None
        due = [
            session.retry_not_before
            for session in event.affected_sessions.values()
            if (
                session.backfill_status is BackfillStatus.RETRY_PENDING
                and session.retry_not_before is not None
            )
        ]
        return min(due) if due else None

    def _require_probe_event(self, event_id: str) -> OutageEvent:
        event_id = _identifier(event_id, "event_id", 32)
        event = self._active_event()
        if event.event_id != event_id:
            raise InvalidOutageTransition("stale outage probe result")
        if self.state is not OutageState.PROBING:
            raise InvalidOutageTransition(
                f"outage is no longer probing: {self.state.value}"
            )
        return event

    def add_affected_session(
        self, session_id: str, *, at: float | None = None
    ) -> AffectedSession:
        event = self._active_event()
        now = _timestamp(self.clock() if at is None else at, "at")
        session_id = _identifier(session_id, "session_id", 1024)
        session = event.affected_sessions.get(session_id)
        if session is None:
            session = AffectedSession(session_id, now, now, updated_at=now)
            event.affected_sessions[session_id] = session
        else:
            session.last_affected_at = max(session.last_affected_at, now)
            session.updated_at = now
        event.updated_at = now
        self.store.save(event)
        return session

    def set_backfill_status(
        self,
        session_id: str,
        status: BackfillStatus | str,
        *,
        at: float | None = None,
    ) -> AffectedSession:
        event = self._active_event()
        session_id = _identifier(session_id, "session_id", 1024)
        if session_id not in event.affected_sessions:
            raise KeyError(session_id)
        now = _timestamp(self.clock() if at is None else at, "at")
        session = event.affected_sessions[session_id]
        session.backfill_status = _backfill_status(status)
        session.retry_not_before = (
            now + COOLDOWN_SECONDS
            if session.backfill_status is BackfillStatus.RETRY_PENDING
            else None
        )
        session.updated_at = now
        event.updated_at = now
        self.store.save(event)
        return session

    def mark_notification(
        self,
        phase: NotificationPhase | str,
        recipient_id: str,
        *,
        cycle: int = 0,
        at: float | None = None,
    ) -> bool:
        event = self._active_event()
        receipt = NotificationReceipt(
            phase=phase,
            cycle=cycle,
            recipient_id=recipient_id,
            sent_at=self.clock() if at is None else at,
        )
        identity = (receipt.phase, receipt.cycle, receipt.recipient_id)
        if any(
            (row.phase, row.cycle, row.recipient_id) == identity
            for row in event.notifications
        ):
            return False
        event.notifications.append(receipt)
        event.updated_at = max(event.updated_at, receipt.sent_at)
        self.store.save(event)
        return True

    def reserve_notification(
        self,
        phase: NotificationPhase | str,
        recipient_id: str,
        *,
        cycle: int = 0,
        at: float | None = None,
    ) -> bool:
        """Persist before sending so an unknown send result cannot duplicate alerts."""
        event = self._active_event()
        now = _timestamp(self.clock() if at is None else at, "at")
        recipient_id = _identifier(recipient_id, "recipient_id", 256)
        if any(
            row.recipient_id == recipient_id
            and 0 <= now - row.sent_at < COOLDOWN_SECONDS
            for row in event.notifications
        ):
            return False
        return self.mark_notification(
            phase, recipient_id, cycle=cycle, at=now
        )

    def _active_event(self) -> OutageEvent:
        if self.current_event is None:
            raise InvalidOutageTransition("no active outage event")
        return self.current_event


def _copy_event(event: OutageEvent) -> OutageEvent:
    return OutageEvent(
        event_id=event.event_id,
        started_at=event.started_at,
        state=event.state,
        error_category=event.error_category,
        provider_id=event.provider_id,
        updated_at=event.updated_at,
        cooldown_until=event.cooldown_until,
        recovery_window_end=event.recovery_window_end,
        ended_at=event.ended_at,
        affected_sessions={
            key: AffectedSession(
                session_id=value.session_id,
                first_affected_at=value.first_affected_at,
                last_affected_at=value.last_affected_at,
                backfill_status=value.backfill_status,
                updated_at=value.updated_at,
                retry_not_before=value.retry_not_before,
            )
            for key, value in event.affected_sessions.items()
        },
        notifications=list(event.notifications),
    )
