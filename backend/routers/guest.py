"""Guest-link creation, preview, join, and revocation endpoints."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, status

from ..auth import Principal
from ..dependencies import current_user, get_store
from ..errors import APIError
from ..models import (
    CreateGuestLinkRequest,
    CreateGuestLinkResponse,
    JoinPreview,
    JoinRequest,
    JoinResponse,
    OkResponse,
)
from ..store import DatabaseStore, GuestLinkRecord, SessionRecord, utc_now
from .helpers import get_session_or_404, require_owner


router = APIRouter(tags=["Guest access"])

GUEST_TOKEN_PATTERN = re.compile(r"^[A-Fa-f0-9]{32}$")


def _valid_token_path(token: str) -> str:
    # Path constraints are also declared on the route so generated OpenAPI
    # carries the same 128-bit token contract.
    if not GUEST_TOKEN_PATTERN.fullmatch(token):
        raise APIError(
            "token_invalid",
            "This link is not valid. Ask your interviewer for a new one.",
            404,
        )
    return token


def _is_expired(value: datetime | None) -> bool:
    if value is None:
        return False
    current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return current <= utc_now()


def _valid_link_and_session(
    store: DatabaseStore,
    token: str,
) -> tuple[GuestLinkRecord, SessionRecord]:
    _valid_token_path(token)
    link = store.find_guest_link(token)
    if link is None:
        raise APIError(
            "token_invalid",
            "This link is not valid. Ask your interviewer for a new one.",
            404,
        )
    if link.revoked_at is not None:
        raise APIError("token_revoked", "This link was revoked.", 403)
    if _is_expired(link.expires_at):
        raise APIError("token_expired", "This link has expired.", 403)
    if link.max_uses is not None and link.use_count >= link.max_uses:
        raise APIError("link_exhausted", "This link has reached its usage limit.", 403)
    session = get_session_or_404(store, link.session_id)
    if session.state in {"ended", "archived"}:
        raise APIError("session_closed", "This interview has ended.", 403)
    return link, session


@router.post(
    "/v1/sessions/{id}/guest-links",
    response_model=CreateGuestLinkResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createGuestLink",
)
def create_guest_link(
    id: str,
    payload: CreateGuestLinkRequest | None = None,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    options = payload or CreateGuestLinkRequest()
    if options.expires_at is not None and _is_expired(options.expires_at):
        raise APIError("invalid_expiry", "The guest link expiration must be in the future.", 400)

    with store.lock:
        session = require_owner(store, id, principal)
        if session.state in {"ended", "archived"}:
            raise APIError("session_closed", "This interview has ended.", 403)
        now = utc_now()
        for link in store.guest_links.values():
            if link.session_id == id and link.revoked_at is None:
                link.revoked_at = now

        raw_token = secrets.token_hex(16)
        link = GuestLinkRecord(
            id=store.new_id("lnk"),
            session_id=id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            role_granted=options.role,
            expires_at=options.expires_at,
            max_uses=options.max_uses,
            revoked_at=None,
            created_at=now,
        )
        store.guest_links[link.id] = link
        store.add_audit(id, "link.rotated", at=now)
        return {
            "link": store.public_guest_link(link),
            "url": f"{store.public_base_url}/join/{raw_token}",
            "token": raw_token,
        }


@router.delete(
    "/v1/sessions/{id}/guest-links/{linkId}",
    response_model=OkResponse,
    operation_id="revokeGuestLink",
)
def revoke_guest_link(
    id: str,
    linkId: str,
    principal: Principal = Depends(current_user),
    store: DatabaseStore = Depends(get_store),
) -> OkResponse:
    with store.lock:
        require_owner(store, id, principal)
        link = store.guest_links.get(linkId)
        if link is None or link.session_id != id:
            raise APIError("link_not_found", "That guest link was not found.", 404)
        if link.revoked_at is None:
            link.revoked_at = utc_now()
            store.add_audit(id, "link.revoked")
        return OkResponse()


@router.get(
    "/v1/join/{token}",
    response_model=JoinPreview,
    operation_id="previewJoin",
)
def preview_join(
    token: str = Path(min_length=32, max_length=32, pattern=r"^[A-Fa-f0-9]{32}$"),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    with store.lock:
        link, session = _valid_link_and_session(store, token)
        owner = store.users.get(session.owner_user_id)
        return {
            "session_id": session.id,
            "title": session.title,
            "owner": owner.display_name if owner else "Interviewer",
            "duration_minutes": session.duration_minutes,
            "capacity": 10,
        }


@router.post(
    "/v1/join/{token}",
    response_model=JoinResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="joinSession",
)
def join_session(
    payload: JoinRequest,
    token: str = Path(min_length=32, max_length=32, pattern=r"^[A-Fa-f0-9]{32}$"),
    store: DatabaseStore = Depends(get_store),
) -> dict:
    if len(payload.display_name.strip()) < 2:
        raise APIError("name_required", "Enter the name your interviewer will see.", 400)
    with store.lock:
        link, session = _valid_link_and_session(store, token)
        active = store.active_participants(session.id)
        if len(active) >= 10:
            raise APIError("at_capacity", "This interview is full.", 409)

        participant = store.add_participant(
            session_id=session.id,
            user_id=None,
            display_name=payload.display_name.strip(),
            role=link.role_granted,
        )
        link.use_count += 1
        collab_token = store.issue_collab_token(
            session_id=session.id,
            participant_id=participant.id,
        )
        return {
            "participant": store.public_participant(participant),
            "session": store.public_session(session),
            "collab_token": collab_token,
            "collab_url": f"wss://collab.northwind.dev/v1/rooms/{session.id}",
            "expires_in": 300,
        }
