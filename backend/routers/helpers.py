"""Authorization and serialization helpers shared by routers."""

from __future__ import annotations

from typing import Any

from ..auth import Principal
from ..errors import APIError
from ..models import CanvasDocument
from ..store import InMemoryStore, SessionRecord


def get_session_or_404(store: InMemoryStore, session_id: str) -> SessionRecord:
    session = store.sessions.get(session_id)
    if session is None:
        raise APIError("session_not_found", "That interview no longer exists.", 404)
    return session


def principal_role(
    store: InMemoryStore,
    session: SessionRecord,
    principal: Principal,
) -> str | None:
    if principal.kind == "collab":
        if principal.session_id != session.id:
            return None
        participant = store.participants.get(principal.participant_id or "")
        if participant is None or participant.left_at is not None:
            return None
        return participant.role
    if principal.user_id is None:
        return None
    return store.user_role_for_session(principal.user_id, session.id)


def require_session_member(
    store: InMemoryStore,
    session_id: str,
    principal: Principal,
) -> tuple[SessionRecord, str]:
    session = get_session_or_404(store, session_id)
    role = principal_role(store, session, principal)
    if role is None:
        raise APIError("forbidden", "You do not have access to this interview.", 403)
    return session, role


def require_owner(
    store: InMemoryStore,
    session_id: str,
    principal: Principal,
) -> SessionRecord:
    session = get_session_or_404(store, session_id)
    if principal.kind != "user" or principal.user_id != session.owner_user_id:
        raise APIError("forbidden", "Only the interviewer who owns this interview can do that.", 403)
    return session


def canvas_document_dict(document: CanvasDocument) -> dict[str, Any]:
    return document.model_dump(by_alias=True, exclude_none=True)


def validate_canvas_limits(document: CanvasDocument) -> None:
    element_count = len(document.nodes) + len(document.edges) + len(document.strokes)
    if element_count > 2_000:
        raise APIError(
            "document_too_large",
            "Canvas exceeds the supported element count.",
            413,
        )
    point_count = sum(len(stroke.points) for stroke in document.strokes)
    if point_count > 10_000:
        raise APIError(
            "document_too_large",
            "Canvas exceeds the supported freehand point count.",
            413,
        )
