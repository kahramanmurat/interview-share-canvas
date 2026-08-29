"""SQLAlchemy persistence boundary and seeded demo data."""

from __future__ import annotations

import copy
import hmac
import os
import secrets
from collections.abc import Iterator, MutableMapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Generic, TypeVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
    or_,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from .auth import (
    DEMO_USER_EMAIL,
    DEMO_USER_PASSWORD,
    hash_password,
    issue_token,
    token_hash,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def days_ago(days: int) -> datetime:
    return utc_now() - timedelta(days=days)


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    organization_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionRecord(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32), index=True)
    candidate_editing_enabled: Mapped[bool] = mapped_column(Boolean)
    cursors_visible: Mapped[bool] = mapped_column(Boolean)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ParticipantRecord(Base):
    __tablename__ = "participants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GuestLinkRecord(Base):
    __tablename__ = "guest_links"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role_granted: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(Integer, default=0)


class CanvasRecord(Base):
    __tablename__ = "canvases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), unique=True, index=True
    )
    schema_version: Mapped[int] = mapped_column(Integer)
    latest_operation_cursor: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    doc: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON))
    operation_ids: Mapped[dict[str, list[Any]]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict
    )


class AuditRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SessionTokenRecord(Base):
    __tablename__ = "session_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CollabTokenRecord(Base):
    __tablename__ = "collaboration_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True
    )
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


PALETTE_SEED: dict[str, dict[str, list[dict[str, Any]]]] = {
    "blank": {"nodes": [], "edges": [], "strokes": []},
    "shortener": {
        "nodes": [
            {"id": "n1", "type": "browser", "label": "Web client", "x": 80, "y": 200, "w": 168, "h": 92},
            {"id": "n2", "type": "gateway", "label": "API gateway", "x": 340, "y": 200, "w": 168, "h": 92},
            {"id": "n3", "type": "service", "label": "Redirect service", "x": 600, "y": 120, "w": 176, "h": 92},
            {"id": "n4", "type": "cache", "label": "Redis — hot slugs", "x": 880, "y": 120, "w": 176, "h": 92},
            {"id": "n5", "type": "sql", "label": "Postgres — links", "x": 880, "y": 280, "w": 176, "h": 92},
        ],
        "edges": [
            {"id": "e1", "from": "n1", "to": "n2", "label": "HTTPS", "style": "elbow", "arrowEnd": True},
            {"id": "e2", "from": "n2", "to": "n3", "label": "", "style": "elbow", "arrowEnd": True},
            {"id": "e3", "from": "n3", "to": "n4", "label": "read", "style": "elbow", "arrowEnd": True},
            {"id": "e4", "from": "n3", "to": "n5", "label": "read/write", "style": "elbow", "arrowEnd": True},
        ],
        "strokes": [],
    },
}


TEMPLATES = [
    {"id": "blank", "name": "Blank canvas", "note": "Start from nothing"},
    {"id": "shortener", "name": "URL shortener starter", "note": "Client, gateway, cache, store"},
]


RecordT = TypeVar("RecordT", bound=Base)


class EntityMapping(MutableMapping[str, RecordT], Generic[RecordT]):
    """Small dictionary-compatible facade over a SQLAlchemy table."""

    def __init__(self, store: "DatabaseStore", model: type[RecordT], key_column: Any) -> None:
        self.store = store
        self.model = model
        self.key_column = key_column

    def __getitem__(self, key: str) -> RecordT:
        record = self.store.db.scalar(select(self.model).where(self.key_column == key))
        if record is None:
            raise KeyError(key)
        return record

    def __setitem__(self, key: str, value: RecordT) -> None:
        if getattr(value, self.key_column.key) != key:
            raise ValueError("Mapping key does not match record key.")
        self.store.db.add(value)
        self.store.db.flush()

    def __delitem__(self, key: str) -> None:
        record = self[key]
        self.store.db.delete(record)
        self.store.db.flush()

    def __iter__(self) -> Iterator[str]:
        return iter(self.store.db.scalars(select(self.key_column)).all())

    def __len__(self) -> int:
        return int(self.store.db.scalar(select(func.count()).select_from(self.model)) or 0)

    def values(self) -> list[RecordT]:  # type: ignore[override]
        return list(self.store.db.scalars(select(self.model)).all())

    def items(self) -> list[tuple[str, RecordT]]:  # type: ignore[override]
        return [(getattr(record, self.key_column.key), record) for record in self.values()]


class StoreLock(AbstractContextManager["StoreLock"]):
    """Serialize writes and commit or roll back each store operation."""

    def __init__(self, store: "DatabaseStore") -> None:
        self.store = store

    def __enter__(self) -> "StoreLock":
        self.store._mutex.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is None:
                self.store.db.commit()
                self.store.db.expire_all()
            else:
                self.store.db.rollback()
        finally:
            self.store._mutex.release()
        return False


def _sqlite_engine_options(database_url: str) -> dict[str, Any]:
    if not database_url.startswith("sqlite"):
        return {}
    options: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if database_url.endswith(":memory:") or database_url in {"sqlite://", "sqlite+pysqlite://"}:
        options["poolclass"] = StaticPool
    return options


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class DatabaseStore:
    """Database-agnostic SQLAlchemy persistence boundary."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        seed: bool = True,
        public_base_url: str | None = None,
    ) -> None:
        self.database_url = database_url or os.getenv(
            "DATABASE_URL", "sqlite+pysqlite:///./data/interview-share-canvas.db"
        )
        self.engine: Engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            **_sqlite_engine_options(self.database_url),
        )
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        Base.metadata.create_all(self.engine)
        self._session_factory = scoped_session(
            sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=True)
        )
        self._mutex = RLock()
        self.lock = StoreLock(self)
        self.public_base_url = (
            public_base_url or os.getenv("PUBLIC_BASE_URL", "https://interviews.northwind.dev")
        ).rstrip("/")
        self.users = EntityMapping(self, UserRecord, UserRecord.id)
        self.sessions = EntityMapping(self, SessionRecord, SessionRecord.id)
        self.participants = EntityMapping(self, ParticipantRecord, ParticipantRecord.id)
        self.guest_links = EntityMapping(self, GuestLinkRecord, GuestLinkRecord.id)
        self.canvases = EntityMapping(self, CanvasRecord, CanvasRecord.session_id)
        self.session_tokens = EntityMapping(self, SessionTokenRecord, SessionTokenRecord.token_hash)
        self.collab_tokens = EntityMapping(self, CollabTokenRecord, CollabTokenRecord.token_hash)
        # Active sockets and transient cursor positions are transport state,
        # not durable application records.
        self.rooms: dict[str, set[Any]] = {}
        self.presence: dict[str, dict[str, dict[str, Any]]] = {}

        if seed and self.db.scalar(select(func.count()).select_from(UserRecord)) == 0:
            with self.lock:
                self.seed_demo_data()

    @property
    def db(self):
        return self._session_factory()

    def close(self) -> None:
        self._session_factory.remove()
        self.engine.dispose()

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(4)}"

    def add_audit(self, session_id: str, action: str, *, at: datetime | None = None) -> AuditRecord:
        event = AuditRecord(
            id=self.new_id("aud"),
            session_id=session_id,
            action=action,
            at=at or utc_now(),
        )
        self.db.add(event)
        self.db.flush()
        return event

    def create_user(
        self,
        *,
        email: str,
        display_name: str,
        organization_id: str | None,
        password: str | None = None,
        password_hash_value: str | None = None,
        user_id: str | None = None,
        created_at: datetime | None = None,
    ) -> UserRecord:
        normalized_email = email.strip().lower()
        user = UserRecord(
            id=user_id or self.new_id("usr"),
            email=normalized_email,
            display_name=display_name,
            organization_id=organization_id,
            password_hash=password_hash_value or hash_password(password or secrets.token_urlsafe(24)),
            created_at=created_at or utc_now(),
        )
        self.users[user.id] = user
        return user

    def find_user_by_email(self, email: str) -> UserRecord | None:
        normalized_email = email.strip().lower()
        return self.db.scalar(select(UserRecord).where(UserRecord.email == normalized_email))

    def create_session(
        self,
        *,
        owner_user_id: str,
        title: str,
        prompt: str = "",
        duration_minutes: int = 45,
        scheduled_at: datetime | None = None,
        candidate_editing_enabled: bool = True,
        cursors_visible: bool = True,
        template_id: str = "blank",
        state: str = "draft",
        session_id: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        add_owner_participant: bool = True,
    ) -> SessionRecord:
        created = created_at or utc_now()
        session = SessionRecord(
            id=session_id or self.new_id("ses"),
            owner_user_id=owner_user_id,
            title=title,
            prompt=prompt,
            state=state,
            candidate_editing_enabled=candidate_editing_enabled,
            cursors_visible=cursors_visible,
            duration_minutes=duration_minutes,
            scheduled_at=scheduled_at,
            started_at=started_at,
            ended_at=ended_at,
            created_at=created,
            updated_at=updated_at or created,
        )
        self.sessions[session.id] = session
        seed = copy.deepcopy(PALETTE_SEED.get(template_id, PALETTE_SEED["blank"]))
        self.canvases[session.id] = CanvasRecord(
            id=self.new_id("cvs"),
            session_id=session.id,
            schema_version=3,
            latest_operation_cursor=0,
            updated_at=session.updated_at,
            doc=seed,
        )
        if add_owner_participant:
            owner = self.users.get(owner_user_id)
            if owner is not None:
                self.add_participant(
                    session_id=session.id,
                    user_id=owner.id,
                    display_name=owner.display_name,
                    role="owner",
                    joined_at=session.started_at or session.created_at,
                )
        return session

    def add_participant(
        self,
        *,
        session_id: str,
        user_id: str | None,
        display_name: str,
        role: str,
        joined_at: datetime | None = None,
        participant_id: str | None = None,
    ) -> ParticipantRecord:
        participant = ParticipantRecord(
            id=participant_id or self.new_id("par"),
            session_id=session_id,
            user_id=user_id,
            display_name=display_name,
            role=role,
            joined_at=joined_at or utc_now(),
            left_at=None,
        )
        self.participants[participant.id] = participant
        return participant

    def public_user(self, user: UserRecord) -> dict[str, Any]:
        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "organization_id": user.organization_id,
            "created_at": user.created_at,
        }

    def public_participant(self, participant: ParticipantRecord) -> dict[str, Any]:
        return {
            "id": participant.id,
            "session_id": participant.session_id,
            "user_id": participant.user_id,
            "display_name": participant.display_name,
            "role": participant.role,
            "joined_at": participant.joined_at,
            "left_at": participant.left_at,
        }

    def public_session(self, session: SessionRecord) -> dict[str, Any]:
        participants = list(
            self.db.scalars(
                select(ParticipantRecord).where(ParticipantRecord.session_id == session.id)
            ).all()
        )
        return {
            "id": session.id,
            "owner_user_id": session.owner_user_id,
            "title": session.title,
            "prompt": session.prompt,
            "state": session.state,
            "candidate_editing_enabled": session.candidate_editing_enabled,
            "cursors_visible": session.cursors_visible,
            "duration_minutes": session.duration_minutes,
            "scheduled_at": session.scheduled_at,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "participants": [p.display_name for p in participants],
            "active_participants": [p.display_name for p in participants if p.left_at is None],
        }

    def public_guest_link(self, link: GuestLinkRecord) -> dict[str, Any]:
        return {
            "id": link.id,
            "session_id": link.session_id,
            "token_hash": f"sha256:{link.token_hash}",
            "role_granted": link.role_granted,
            "expires_at": link.expires_at,
            "max_uses": link.max_uses,
            "revoked_at": link.revoked_at,
            "created_at": link.created_at,
        }

    def public_audit(self, session_id: str) -> list[dict[str, Any]]:
        events = self.db.scalars(
            select(AuditRecord)
            .where(AuditRecord.session_id == session_id)
            .order_by(AuditRecord.at.desc())
        ).all()
        return [
            {"id": event.id, "session_id": event.session_id, "action": event.action, "at": event.at}
            for event in events
        ]

    def issue_session_token(self, user_id: str, *, ttl_seconds: int = 86_400) -> str:
        raw = issue_token("usr_")
        self.session_tokens[token_hash(raw)] = SessionTokenRecord(
            token_hash=token_hash(raw),
            user_id=user_id,
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        )
        return raw

    def get_session_token(self, raw: str) -> SessionTokenRecord | None:
        digest = token_hash(raw)
        record = self.session_tokens.get(digest)
        if record is None:
            return None
        if _ensure_utc(record.expires_at) <= utc_now():
            return None
        return record

    def issue_collab_token(
        self,
        *,
        session_id: str,
        participant_id: str,
        ttl_seconds: int = 300,
    ) -> str:
        raw = issue_token("cbt_")
        self.collab_tokens[token_hash(raw)] = CollabTokenRecord(
            token_hash=token_hash(raw),
            session_id=session_id,
            participant_id=participant_id,
            expires_at=utc_now() + timedelta(seconds=ttl_seconds),
        )
        return raw

    def get_collab_token(self, raw: str) -> CollabTokenRecord | None:
        digest = token_hash(raw)
        record = self.collab_tokens.get(digest)
        if record is None:
            return None
        if _ensure_utc(record.expires_at) <= utc_now():
            return None
        return record

    def find_guest_link(self, raw_token: str) -> GuestLinkRecord | None:
        digest = token_hash(raw_token)
        link = self.db.scalar(select(GuestLinkRecord).where(GuestLinkRecord.token_hash == digest))
        return link if link is not None and hmac.compare_digest(link.token_hash, digest) else None

    def active_participants(self, session_id: str) -> list[ParticipantRecord]:
        return list(
            self.db.scalars(
                select(ParticipantRecord).where(
                    ParticipantRecord.session_id == session_id,
                    ParticipantRecord.left_at.is_(None),
                )
            ).all()
        )

    def user_role_for_session(self, user_id: str, session_id: str) -> str | None:
        session = self.sessions.get(session_id)
        if session is not None and session.owner_user_id == user_id:
            return "owner"
        participant = self.db.scalar(
            select(ParticipantRecord).where(
                ParticipantRecord.session_id == session_id,
                ParticipantRecord.user_id == user_id,
                ParticipantRecord.left_at.is_(None),
            )
        )
        return participant.role if participant is not None else None

    def visible_sessions(self, user_id: str) -> list[SessionRecord]:
        member_session_ids = select(ParticipantRecord.session_id).where(
            ParticipantRecord.user_id == user_id,
            ParticipantRecord.left_at.is_(None),
        )
        return list(
            self.db.scalars(
                select(SessionRecord).where(
                    or_(
                        SessionRecord.owner_user_id == user_id,
                        SessionRecord.id.in_(member_session_ids),
                    )
                )
            ).all()
        )

    def delete_session_records(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            self.db.delete(session)
            self.db.flush()

    def seed_demo_data(self) -> None:
        owner = self.create_user(
            user_id="usr_owner",
            email=DEMO_USER_EMAIL,
            display_name="Dana Reyes",
            organization_id="org_1",
            password=DEMO_USER_PASSWORD,
        )

        ended_created = days_ago(6)
        ended = self.create_session(
            owner_user_id=owner.id,
            session_id="ses_seed_ended",
            title="Senior BE — Priya Raghavan",
            prompt="Design a URL shortener that serves 50k redirects/second with custom slugs and click analytics.",
            state="ended",
            template_id="shortener",
            created_at=ended_created,
            updated_at=ended_created,
            started_at=ended_created,
            ended_at=ended_created + timedelta(minutes=41, seconds=30),
        )
        ended_owner = next(
            p for p in self.participants.values()
            if p.session_id == ended.id and p.role == "owner"
        )
        ended_owner.left_at = ended.ended_at
        priya = self.add_participant(
            session_id=ended.id,
            user_id=None,
            display_name="Priya Raghavan",
            role="candidate",
            joined_at=ended_created,
        )
        priya.left_at = ended.ended_at

        live_started = utc_now() - timedelta(minutes=14)
        live = self.create_session(
            owner_user_id=owner.id,
            session_id="ses_seed_live",
            title="Staff Infra — Marcus Oyelaran",
            prompt="Design a multi-region rate limiter used by every internal service. Discuss consistency trade-offs.",
            state="live",
            template_id="shortener",
            created_at=utc_now(),
            updated_at=utc_now(),
            started_at=live_started,
        )
        self.add_participant(
            session_id=live.id,
            user_id=None,
            display_name="Marcus Oyelaran",
            role="candidate",
            joined_at=live_started,
        )

        self.create_session(
            owner_user_id=owner.id,
            session_id="ses_seed_draft",
            title="Senior FE — Ana Sørensen",
            prompt="Design the collaborative canvas you are drawing on right now. Focus on the sync layer.",
            scheduled_at=utc_now() + timedelta(hours=26),
            created_at=days_ago(1),
            updated_at=days_ago(1),
        )

        archived_created = days_ago(21)
        archived = self.create_session(
            owner_user_id=owner.id,
            session_id="ses_seed_archived",
            title="Platform — Tomás Lindqvist",
            prompt="Design an event pipeline for product analytics at 1M events/minute.",
            state="archived",
            template_id="shortener",
            created_at=archived_created,
            updated_at=days_ago(20),
            started_at=archived_created,
            ended_at=archived_created + timedelta(minutes=38),
        )
        archived_owner = next(
            p for p in self.participants.values()
            if p.session_id == archived.id and p.role == "owner"
        )
        archived_owner.left_at = archived.ended_at

        for index, action in enumerate(
            ["session.created", "link.rotated", "session.started", "permission.changed", "session.ended"]
        ):
            self.add_audit(
                ended.id,
                action,
                at=ended_created + timedelta(seconds=index * 420),
            )
