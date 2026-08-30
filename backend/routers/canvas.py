"""Canvas snapshot reads and autosave writes."""

from __future__ import annotations

import copy

from fastapi import APIRouter, Depends

from ..auth import Principal
from ..dependencies import current_principal, get_store
from ..errors import APIError
from ..metrics import record_elements_created
from ..models import CanvasDocument, CanvasResponse, SaveCanvasRequest, SaveCanvasResponse
from ..store import DatabaseStore, utc_now
from .helpers import (
    canvas_document_dict,
    require_session_member,
    validate_canvas_limits,
)


router = APIRouter(prefix="/v1/sessions", tags=["Canvas"])


def _get_canvas(store: DatabaseStore, session_id: str):
    canvas = store.canvases.get(session_id)
    if canvas is None:
        raise APIError("canvas_not_found", "The canvas for this interview was not found.", 404)
    return canvas


def _public_canvas(canvas) -> dict:
    return {
        "canvas_document_id": canvas.id,
        "schema_version": canvas.schema_version,
        "operation_cursor": canvas.latest_operation_cursor,
        "doc": copy.deepcopy(canvas.doc),
    }


@router.get(
    "/{id}/canvas",
    response_model=CanvasResponse,
    response_model_exclude_none=True,
    operation_id="getCanvas",
)
def get_canvas(
    id: str,
    principal: Principal = Depends(current_principal),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    with store.lock:
        require_session_member(store, id, principal)
        return _public_canvas(_get_canvas(store, id))


@router.post("/{id}/canvas", response_model=SaveCanvasResponse, operation_id="saveCanvas")
def save_canvas(
    id: str,
    payload: SaveCanvasRequest,
    principal: Principal = Depends(current_principal),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    validate_canvas_limits(payload.doc)
    with store.lock:
        session, role = require_session_member(store, id, principal)
        if role == "observer":
            raise APIError("editing_forbidden", "Observers cannot edit this canvas.", 403)
        if role == "candidate" and not session.candidate_editing_enabled:
            raise APIError("editing_locked", "The interviewer has locked editing.", 403)
        if session.state == "ended" and role != "owner":
            raise APIError("session_ended", "This interview has ended.", 403)
        if session.state == "archived":
            raise APIError("session_closed", "This interview has ended.", 403)

        canvas = _get_canvas(store, id)
        if payload.client_operation_id:
            prior = canvas.operation_ids.get(payload.client_operation_id)
            if prior is not None:
                cursor, saved_at = prior
                return {"operation_cursor": cursor, "saved_at": saved_at}

        now = utc_now()
        previous_doc = canvas.doc
        canvas.doc = canvas_document_dict(payload.doc)
        record_elements_created(previous_doc, canvas.doc)
        canvas.latest_operation_cursor += 1
        canvas.updated_at = now
        if payload.client_operation_id:
            canvas.operation_ids[payload.client_operation_id] = [
                canvas.latest_operation_cursor,
                now.isoformat(),
            ]
        session.updated_at = now
        return {
            "operation_cursor": canvas.latest_operation_cursor,
            "saved_at": now,
        }
