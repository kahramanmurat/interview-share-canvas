"""Pydantic request and response models for the API contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class APIModel(BaseModel):
    """Base model with the contract's strict object behavior by default."""

    model_config = ConfigDict(extra="forbid")


class ErrorResponse(APIModel):
    code: str
    message: str


class MagicLinkRequest(APIModel):
    email: EmailStr = Field(max_length=320)


class User(APIModel):
    id: str
    email: EmailStr
    display_name: str
    organization_id: str | None
    created_at: datetime | None = None


class MagicLinkResponse(APIModel):
    user: User
    expires_in: int = Field(ge=1)


SessionState = Literal["draft", "live", "ended", "archived"]
ParticipantRole = Literal["owner", "interviewer", "candidate", "observer"]
GuestRole = Literal["candidate", "interviewer", "observer"]


class Participant(APIModel):
    id: str
    session_id: str
    user_id: str | None
    display_name: str = Field(min_length=2, max_length=200)
    role: ParticipantRole
    joined_at: datetime
    left_at: datetime | None


class Session(APIModel):
    id: str
    owner_user_id: str
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(max_length=20_000)
    state: SessionState
    candidate_editing_enabled: bool
    cursors_visible: bool
    duration_minutes: int = Field(ge=1, le=480)
    scheduled_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime
    participants: list[str]
    active_participants: list[str]


class CreateSessionRequest(APIModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(default="", max_length=20_000)
    duration_minutes: Literal[30, 45, 60] = 45
    scheduled_at: datetime | None = None
    template_id: Literal["blank", "shortener"] = "blank"
    candidate_editing_enabled: bool = True
    editing: bool = Field(default=None, deprecated=True)


class UpdateSessionRequest(APIModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"minProperties": 1},
    )

    title: str = Field(default=None, min_length=1, max_length=200)
    prompt: str = Field(default=None, max_length=20_000)
    duration_minutes: int = Field(default=None, ge=1, le=480)
    scheduled_at: datetime | None = None
    candidate_editing_enabled: bool = None
    cursors_visible: bool = None
    state: Literal["archived"] = None

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> UpdateSessionRequest:
        if not self.model_fields_set:
            raise ValueError("At least one session field is required.")
        return self


class CreateGuestLinkRequest(APIModel):
    role: GuestRole = "candidate"
    expires_at: datetime | None = None
    max_uses: int | None = Field(default=10, ge=1, le=10)


class GuestLink(APIModel):
    id: str
    session_id: str
    token_hash: str
    role_granted: GuestRole
    expires_at: datetime | None
    max_uses: int | None = Field(ge=1, le=10)
    revoked_at: datetime | None
    created_at: datetime


class CreateGuestLinkResponse(APIModel):
    link: GuestLink
    url: str
    token: str = Field(pattern=r"^[A-Fa-f0-9]{32}$", min_length=32, max_length=32)


class JoinPreview(APIModel):
    session_id: str
    title: str
    owner: str
    duration_minutes: int = Field(ge=1)
    capacity: int = Field(ge=1, le=10)


class JoinRequest(APIModel):
    display_name: str = Field(min_length=2, max_length=200)
    role: Literal["candidate"] = "candidate"


class JoinResponse(APIModel):
    participant: Participant
    session: Session
    collab_token: str
    collab_url: str = Field(pattern=r"^wss://")
    expires_in: int = Field(ge=1)


CANVAS_NODE_TYPES = Literal[
    "service",
    "process",
    "text",
    "note",
    "boundary",
    "sql",
    "nosql",
    "cache",
    "blob",
    "warehouse",
    "queue",
    "stream",
    "pubsub",
    "browser",
    "mobile",
    "gateway",
    "lb",
    "cdn",
    "external",
    "server",
    "worker",
    "lambda",
    "cluster",
    "llm",
    "embed",
    "vector",
    "agent",
    "generic",
    "rect",
    "ellipse",
]


class CanvasNode(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: CANVAS_NODE_TYPES
    label: str = Field(max_length=500)
    x: float
    y: float
    w: float = Field(ge=96)
    h: float = Field(ge=56)
    desc: str = Field(
        default=None,
        max_length=2_000,
        exclude_if=lambda value: value is None,
    )


class CanvasEdge(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: str
    label: str = Field(max_length=500)
    style: Literal["straight", "elbow", "curved"]
    arrowStart: bool = False
    arrowEnd: bool
    dashed: bool = False
    color: str = Field(
        default=None,
        pattern=r"^#[0-9A-Fa-f]{6}$",
        exclude_if=lambda value: value is None,
    )
    width: float = Field(
        default=None,
        ge=0.5,
        le=20,
        exclude_if=lambda value: value is None,
    )

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class CanvasStroke(APIModel):
    id: str
    tool: Literal["pen", "highlighter"]
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    width: float = Field(gt=0, le=100)
    points: list[tuple[float, float]] = Field(min_length=2, max_length=10_000)


class CanvasDocument(APIModel):
    nodes: list[CanvasNode] = Field(max_length=2_000)
    edges: list[CanvasEdge] = Field(max_length=2_000)
    strokes: list[CanvasStroke] = Field(max_length=2_000)


class CanvasResponse(APIModel):
    canvas_document_id: str
    schema_version: int = Field(ge=1)
    operation_cursor: int = Field(ge=0)
    doc: CanvasDocument


class SaveCanvasRequest(APIModel):
    doc: CanvasDocument
    actor: Literal["owner", "candidate"] = None
    client_operation_id: str = Field(default=None, max_length=200)


class SaveCanvasResponse(APIModel):
    operation_cursor: int = Field(ge=1)
    saved_at: datetime


class ExportSession(APIModel):
    id: str
    title: str
    prompt: str
    ended_at: datetime | None


class ExportResponse(APIModel):
    schema_version: int = Field(ge=1)
    session: ExportSession
    canvas: CanvasDocument
    exported_at: datetime


AuditAction = Literal[
    "session.created",
    "link.rotated",
    "session.started",
    "permission.changed",
    "session.ended",
    "link.revoked",
    "participant.removed",
]


class AuditEvent(APIModel):
    id: str
    session_id: str
    action: AuditAction
    at: datetime


class OkResponse(APIModel):
    ok: Literal[True] = True


class PasswordLoginRequest(APIModel):
    """Small non-contract helper used for local bearer-token sign-in."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class PasswordLoginResponse(APIModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: User


class WebSocketMessageBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    protocol_version: str = "1"
    session_id: str
    correlation_id: str = None
    operation_id: str = None


class JoinRoomMessage(WebSocketMessageBase):
    participant_id: str = None
    last_operation_cursor: int = Field(default=None, ge=0)


class DocumentUpdateMessage(WebSocketMessageBase):
    client_operation_id: str
    operation: dict[str, Any] = None


class Cursor(APIModel):
    x: float
    y: float


class PresenceUpdateMessage(WebSocketMessageBase):
    cursor: Cursor = None
    selection: list[str] = Field(default_factory=list)


class PingMessage(WebSocketMessageBase):
    pass


class PresenceParticipant(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str
    role: ParticipantRole
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    you: bool = None
    cursor: Cursor = None
    selection: list[str] = Field(default_factory=list)


class RoomJoinedMessage(APIModel):
    session_id: str
    operation_cursor: int = Field(ge=0)
    participants: list[PresenceParticipant]


PresenceSnapshotMessage = list[PresenceParticipant]


class PermissionChangedMessage(APIModel):
    candidate_editing_enabled: bool


class SessionEndedMessage(APIModel):
    at: datetime


class AckMessage(APIModel):
    client_operation_id: str


class ResyncedMessage(APIModel):
    operation_cursor: int = Field(ge=0)


StatusMessage = Literal["connecting", "connected", "reconnecting", "offline"]
