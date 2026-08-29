"""JSON export and audit history endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import Principal
from ..dependencies import current_user, get_store
from ..models import AuditEvent, CanvasDocument, ExportResponse, ExportSession
from ..store import DatabaseStore, utc_now
from .helpers import require_session_member


router = APIRouter(prefix="/v1/sessions", tags=["Review"])


@router.get("/{id}/export", response_model=ExportResponse, operation_id="exportJson")
def export_json(
    id: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    with store.lock:
        session, _ = require_session_member(store, id, principal)
        canvas = store.canvases[id]
        document = CanvasDocument.model_validate(canvas.doc)
        return {
            "schema_version": canvas.schema_version,
            "session": ExportSession(
                id=session.id,
                title=session.title,
                prompt=session.prompt,
                ended_at=session.ended_at,
            ),
            "canvas": document,
            "exported_at": utc_now(),
        }


@router.get("/{id}/audit", response_model=list[AuditEvent], operation_id="auditTrail")
def audit_trail(
    id: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> list[dict]:
    with store.lock:
        require_session_member(store, id, principal)
        # ``require_session_member`` also gives the correct 404 response for
        # unknown sessions before the audit list is read.
        return store.public_audit(id)
