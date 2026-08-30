"""Session lifecycle and participant-management endpoints."""

from __future__ import annotations

import copy

from fastapi import APIRouter, Depends, status

from ..auth import Principal
from ..dependencies import current_user, get_store
from ..errors import APIError
from ..metrics import record_room_created
from ..models import (
    CreateSessionRequest,
    OkResponse,
    Session,
    UpdateSessionRequest,
)
from ..store import DatabaseStore, utc_now
from .helpers import require_owner, require_session_member


router = APIRouter(prefix="/v1/sessions", tags=["Sessions"])


@router.get("", response_model=list[Session], operation_id="listSessions")
def list_sessions(
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> list[dict]:
    with store.lock:
        sessions = store.visible_sessions(principal.user_id or "")
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return [store.public_session(session) for session in sessions]


@router.post(
    "",
    response_model=Session,
    status_code=status.HTTP_201_CREATED,
    operation_id="createSession",
)
def create_session(
    payload: CreateSessionRequest,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    title = payload.title.strip()
    if not title:
        raise APIError("title_required", "Give the interview a title.", 400)

    candidate_editing_enabled = payload.candidate_editing_enabled
    # ``editing`` is retained for the prototype's older create form. The
    # canonical field wins whenever both fields are supplied.
    legacy_editing = payload.__dict__.get("editing")
    if "candidate_editing_enabled" not in payload.model_fields_set and legacy_editing is not None:
        candidate_editing_enabled = legacy_editing

    with store.lock:
        session = store.create_session(
            owner_user_id=principal.user_id or "",
            title=title,
            prompt=payload.prompt,
            duration_minutes=payload.duration_minutes,
            scheduled_at=payload.scheduled_at,
            candidate_editing_enabled=candidate_editing_enabled,
            template_id=payload.template_id,
        )
        store.add_audit(session.id, "session.created")
        record_room_created("new")
        return store.public_session(session)


@router.get("/{id}", response_model=Session, operation_id="getSession")
def get_session(
    id: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    with store.lock:
        session, _ = require_session_member(store, id, principal)
        return store.public_session(session)


@router.patch("/{id}", response_model=Session, operation_id="patchSession")
def patch_session(
    id: str,
    payload: UpdateSessionRequest,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    with store.lock:
        session = require_owner(store, id, principal)
        changes = payload.model_dump(exclude_unset=True)
        permission_changed = (
            "candidate_editing_enabled" in changes
            and changes["candidate_editing_enabled"] != session.candidate_editing_enabled
        )
        for field_name, value in changes.items():
            if field_name == "title":
                value = value.strip()
                if not value:
                    raise APIError("title_required", "Give the interview a title.", 400)
            setattr(session, field_name, value)
        session.updated_at = utc_now()
        if permission_changed:
            store.add_audit(id, "permission.changed")
        return store.public_session(session)


@router.post("/{id}/start", response_model=Session, operation_id="startSession")
def start_session(
    id: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    with store.lock:
        session = require_owner(store, id, principal)
        if session.state == "archived":
            raise APIError("invalid_state", "An archived interview cannot be started.", 400)
        session.state = "live"
        session.started_at = session.started_at or utc_now()
        session.updated_at = utc_now()
        store.add_audit(id, "session.started")
        return store.public_session(session)


@router.post("/{id}/end", response_model=Session, operation_id="endSession")
def end_session(
    id: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    with store.lock:
        session = require_owner(store, id, principal)
        if session.state == "archived":
            raise APIError("invalid_state", "An archived interview cannot be ended.", 400)
        session.state = "ended"
        session.ended_at = utc_now()
        session.candidate_editing_enabled = False
        session.updated_at = utc_now()
        store.add_audit(id, "session.ended")
        return store.public_session(session)


@router.post(
    "/{id}/duplicate",
    response_model=Session,
    status_code=status.HTTP_201_CREATED,
    operation_id="duplicateSession",
)
def duplicate_session(
    id: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    with store.lock:
        source = require_owner(store, id, principal)
        duplicate = store.create_session(
            owner_user_id=source.owner_user_id,
            title=f"{source.title} (copy)",
            prompt=source.prompt,
            duration_minutes=source.duration_minutes,
            template_id="blank",
        )
        store.canvases[duplicate.id].doc = copy.deepcopy(store.canvases[source.id].doc)
        store.add_audit(duplicate.id, "session.created")
        record_room_created("duplicate")
        return store.public_session(duplicate)


@router.delete("/{id}", response_model=OkResponse, operation_id="deleteSession")
async def delete_session(
    id: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> OkResponse:
    """Permanently delete an interview and its dependent database records."""

    with store.lock:
        require_owner(store, id, principal)
        connections = list(store.rooms.pop(id, set()))
        store.presence.pop(id, None)
        store.delete_session_records(id)

    for connection in connections:
        try:
            await connection.send_json({"type": "session_deleted"})
            await connection.close(code=1000, reason="This interview was deleted.")
        except Exception:
            pass
    return OkResponse()


@router.delete(
    "/{id}/participants/{participantId}",
    response_model=OkResponse,
    operation_id="removeParticipant",
)
def remove_participant(
    id: str,
    participantId: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> OkResponse:
    with store.lock:
        require_owner(store, id, principal)
        participant = store.participants.get(participantId)
        if participant is None or participant.session_id != id:
            raise APIError("participant_not_found", "That participant is not in this interview.", 404)
        if participant.role == "owner":
            raise APIError("owner_cannot_be_removed", "The interview owner cannot be removed.", 400)
        participant.left_at = utc_now()
        store.add_audit(id, "participant.removed")
        return OkResponse()
