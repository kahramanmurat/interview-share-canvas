"""Minimal in-memory collaboration gateway for the contract's WebSocket events."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from ..auth import Principal, authenticate_token
from ..errors import APIError
from ..models import CanvasDocument
from ..store import InMemoryStore, ParticipantRecord, SessionRecord, utc_now
from .helpers import canvas_document_dict, require_session_member, validate_canvas_limits


router = APIRouter(tags=["Collaboration"])
REMOTE_COLORS = ["#ec3013", "#2d5fd0", "#0f8a54"]


def _websocket_principal(websocket: WebSocket, store: InMemoryStore) -> Principal | None:
    authorization = websocket.headers.get("authorization", "")
    raw_token: str | None = None
    if authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()
    if raw_token is None:
        # Browser WebSocket handshakes cannot set an Authorization header.
        raw_token = websocket.query_params.get("access_token")
    if raw_token is None:
        raw_token = websocket.cookies.get("session")
    return authenticate_token(raw_token, store)


def _participant_for_principal(
    store: InMemoryStore,
    session: SessionRecord,
    principal: Principal,
) -> ParticipantRecord | None:
    if principal.participant_id:
        return store.participants.get(principal.participant_id)
    for participant in store.participants.values():
        if (
            participant.session_id == session.id
            and participant.user_id == principal.user_id
            and participant.left_at is None
        ):
            return participant
    return None


def _presence_snapshot(
    store: InMemoryStore,
    session: SessionRecord,
    current_participant_id: str | None,
) -> list[dict[str, Any]]:
    participants = [
        participant for participant in store.participants.values()
        if participant.session_id == session.id and participant.left_at is None
    ]
    result: list[dict[str, Any]] = []
    for index, participant in enumerate(participants):
        item = {
            "id": participant.id,
            "display_name": participant.display_name,
            "role": participant.role,
            "color": REMOTE_COLORS[index % len(REMOTE_COLORS)],
        }
        if participant.id == current_participant_id:
            item["you"] = True
        item.update(store.presence.get(session.id, {}).get(participant.id, {}))
        # Presence records contain the participant identity too; keep the
        # canonical display and role from persistence if a client sent stale
        # values in an update.
        item["id"] = participant.id
        item["display_name"] = participant.display_name
        item["role"] = participant.role
        result.append(item)
    return result


async def _broadcast(
    store: InMemoryStore,
    session_id: str,
    message: dict[str, Any],
    *,
    exclude: WebSocket | None = None,
) -> None:
    connections = list(store.rooms.get(session_id, set()))
    stale: list[WebSocket] = []
    for connection in connections:
        if connection is exclude:
            continue
        try:
            await connection.send_json(message)
        except Exception:
            stale.append(connection)
    if stale:
        with store.lock:
            room = store.rooms.get(session_id, set())
            for connection in stale:
                room.discard(connection)


async def _send_error(websocket: WebSocket, error: APIError) -> None:
    await websocket.send_json(
        {"type": "error", "code": error.code, "message": error.message}
    )


def _persist_update(
    store: InMemoryStore,
    session_id: str,
    principal: Principal,
    document: CanvasDocument,
    client_operation_id: str,
) -> None:
    validate_canvas_limits(document)
    with store.lock:
        session, role = require_session_member(store, session_id, principal)
        if role == "observer":
            raise APIError("editing_forbidden", "Observers cannot edit this canvas.", 403)
        if role == "candidate" and not session.candidate_editing_enabled:
            raise APIError("editing_locked", "The interviewer has locked editing.", 403)
        if session.state == "ended" and role != "owner":
            raise APIError("session_ended", "This interview has ended.", 403)
        if session.state == "archived":
            raise APIError("session_closed", "This interview has ended.", 403)
        canvas = store.canvases[session_id]
        if client_operation_id in canvas.operation_ids:
            return
        now = utc_now()
        canvas.doc = canvas_document_dict(document)
        canvas.latest_operation_cursor += 1
        canvas.updated_at = now
        canvas.operation_ids[client_operation_id] = (canvas.latest_operation_cursor, now)
        session.updated_at = now


@router.websocket("/v1/rooms/{session_id}")
async def collaboration_room(websocket: WebSocket, session_id: str) -> None:
    store: InMemoryStore = websocket.app.state.store
    principal = _websocket_principal(websocket, store)
    if principal is None:
        await websocket.accept()
        await websocket.close(code=1008, reason="Sign in is required.")
        return

    try:
        with store.lock:
            session, _ = require_session_member(store, session_id, principal)
            participant = _participant_for_principal(store, session, principal)
            if participant is None:
                raise APIError("forbidden", "You must join this interview first.", 403)
            participant_id = participant.id
            store.rooms.setdefault(session_id, set())
            store.presence.setdefault(session_id, {})
    except APIError as error:
        await websocket.accept()
        await _send_error(websocket, error)
        await websocket.close(code=1008, reason=error.message)
        return

    await websocket.accept()
    with store.lock:
        store.rooms[session_id].add(websocket)
    await websocket.send_json({"type": "status", "status": "connected"})

    joined = False
    try:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            payload = message.get("payload") if isinstance(message.get("payload"), dict) else message

            if message_type == "join_room":
                requested_session = payload.get("session_id", session_id)
                requested_participant = payload.get("participant_id")
                if requested_session != session_id or (
                    requested_participant is not None and requested_participant != participant_id
                ):
                    await _send_error(
                        websocket,
                        APIError("invalid_room", "The room or participant does not match this token.", 403),
                    )
                    continue
                joined = True
                with store.lock:
                    current = store.canvases[session_id].latest_operation_cursor
                    roster = _presence_snapshot(store, session, participant_id)
                await websocket.send_json(
                    {
                        "type": "room_joined",
                        "session_id": session_id,
                        "operation_cursor": current,
                        "participants": roster,
                    }
                )
                await websocket.send_json(
                    {"type": "presence_snapshot", "participants": roster}
                )
                continue

            if not joined:
                await _send_error(
                    websocket,
                    APIError("room_not_joined", "Send a join_room message first.", 400),
                )
                continue

            if message_type == "document_update":
                client_operation_id = payload.get("client_operation_id")
                if not isinstance(client_operation_id, str) or not client_operation_id:
                    await _send_error(
                        websocket,
                        APIError("operation_id_required", "A client operation ID is required.", 400),
                    )
                    continue
                operation = payload.get("operation")
                if isinstance(operation, dict) and isinstance(operation.get("doc"), dict):
                    try:
                        document = CanvasDocument.model_validate(operation["doc"])
                        _persist_update(
                            store,
                            session_id,
                            principal,
                            document,
                            client_operation_id,
                        )
                    except (APIError, ValidationError) as error:
                        if isinstance(error, APIError):
                            await _send_error(websocket, error)
                        else:
                            await _send_error(
                                websocket,
                                APIError("validation_error", "The document is invalid.", 400),
                            )
                        continue
                await websocket.send_json(
                    {"type": "ack", "client_operation_id": client_operation_id}
                )
                await _broadcast(
                    store,
                    session_id,
                    {"type": "document_update", **payload},
                    exclude=websocket,
                )
                continue

            if message_type == "presence_update":
                update = {
                    "id": participant_id,
                    "cursor": payload.get("cursor"),
                    "selection": payload.get("selection", []),
                }
                with store.lock:
                    store.presence[session_id][participant_id] = update
                await _broadcast(
                    store,
                    session_id,
                    {"type": "presence_update", "participant_id": participant_id, **update},
                    exclude=websocket,
                )
                continue

            if message_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            await _send_error(
                websocket,
                APIError("unknown_message", "That collaboration message is not supported.", 400),
            )
    except WebSocketDisconnect:
        pass
    finally:
        with store.lock:
            store.rooms.get(session_id, set()).discard(websocket)
            store.presence.get(session_id, {}).pop(participant_id, None)
