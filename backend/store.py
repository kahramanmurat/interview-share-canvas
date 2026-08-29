"""Thread-safe in-memory persistence and seeded demo data."""

from __future__ import annotations

import copy
import hmac
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

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


@dataclass
class UserRecord:
    id: str
    email: str
    display_name: str
    organization_id: str | None
    password_hash: str
    created_at: datetime


@dataclass
class SessionRecord:
    id: str
    owner_user_id: str
    title: str
    prompt: str
    state: str
    candidate_editing_enabled: bool
    cursors_visible: bool
    duration_minutes: int
    scheduled_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class ParticipantRecord:
    id: str
    session_id: str
    user_id: str | None
    display_name: str
    role: str
    joined_at: datetime
    left_at: datetime | None


@dataclass
class GuestLinkRecord:
    id: str
    session_id: str
    token_hash: str
    role_granted: str
    expires_at: datetime | None
    max_uses: int | None
    revoked_at: datetime | None
    created_at: datetime
    use_count: int = 0


@dataclass
class CanvasRecord:
    id: str
    session_id: str
    schema_version: int
    latest_operation_cursor: int
    updated_at: datetime
    doc: dict[str, Any]
    operation_ids: dict[str, tuple[int, datetime]] = field(default_factory=dict)


@dataclass
class AuditRecord:
    id: str
    session_id: str
    action: str
    at: datetime


@dataclass
class SessionTokenRecord:
    token_hash: str
    user_id: str
    expires_at: datetime


@dataclass
class CollabTokenRecord:
    token_hash: str
    session_id: str
    participant_id: str
    expires_at: datetime


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


class InMemoryStore:
    """A small persistence boundary that can be replaced by a database later."""

    def __init__(self, *, seed: bool = True, public_base_url: str | None = None) -> None:
        self.lock = RLock()
        self.public_base_url = (
            public_base_url or os.getenv("PUBLIC_BASE_URL", "https://interviews.northwind.dev")
        ).rstrip("/")
        self.users: dict[str, UserRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.participants: dict[str, ParticipantRecord] = {}
        self.guest_links: dict[str, GuestLinkRecord] = {}
        self.canvases: dict[str, CanvasRecord] = {}
        self.audit: list[AuditRecord] = []
        self.session_tokens: dict[str, SessionTokenRecord] = {}
        self.collab_tokens: dict[str, CollabTokenRecord] = {}
        self.rooms: dict[str, set[Any]] = {}
        self.presence: dict[str, dict[str, dict[str, Any]]] = {}
        if seed:
            self.seed_demo_data()

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(4)}"

    def add_audit(self, session_id: str, action: str, *, at: datetime | None = None) -> AuditRecord:
        event = AuditRecord(
            id=self.new_id("aud"),
            session_id=session_id,
            action=action,
            at=at or utc_now(),
        )
        self.audit.append(event)
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
        return next((u for u in self.users.values() if u.email == normalized_email), None)

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
        participants = [
            p for p in self.participants.values() if p.session_id == session.id
        ]
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
        events = [event for event in self.audit if event.session_id == session_id]
        events.sort(key=lambda event: event.at, reverse=True)
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
        if record.expires_at <= utc_now():
            self.session_tokens.pop(digest, None)
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
        if record.expires_at <= utc_now():
            self.collab_tokens.pop(digest, None)
            return None
        return record

    def find_guest_link(self, raw_token: str) -> GuestLinkRecord | None:
        digest = token_hash(raw_token)
        for link in self.guest_links.values():
            if hmac.compare_digest(link.token_hash, digest):
                return link
        return None

    def active_participants(self, session_id: str) -> list[ParticipantRecord]:
        return [
            p for p in self.participants.values()
            if p.session_id == session_id and p.left_at is None
        ]

    def user_role_for_session(self, user_id: str, session_id: str) -> str | None:
        session = self.sessions.get(session_id)
        if session is not None and session.owner_user_id == user_id:
            return "owner"
        for participant in self.participants.values():
            if (
                participant.session_id == session_id
                and participant.user_id == user_id
                and participant.left_at is None
            ):
                return participant.role
        return None

    def visible_sessions(self, user_id: str) -> list[SessionRecord]:
        return [
            session for session in self.sessions.values()
            if self.user_role_for_session(user_id, session.id) is not None
        ]

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
