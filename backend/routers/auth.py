"""Authentication endpoints."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Response

from ..auth import hash_password, verify_password
from ..dependencies import get_store
from ..errors import APIError
from ..models import (
    MagicLinkRequest,
    MagicLinkResponse,
    PasswordLoginRequest,
    PasswordLoginResponse,
)
from ..store import DatabaseStore, UserRecord


router = APIRouter(prefix="/v1/auth", tags=["Authentication"])


def _display_name_for_email(email: str) -> str:
    local_part = email.split("@", 1)[0]
    words = re.split(r"[._+\-]+", local_part)
    return " ".join(word[:1].upper() + word[1:] for word in words if word) or "Interviewer"


def _set_session_cookie(response: Response, store: DatabaseStore, user: UserRecord) -> str:
    raw_token = store.issue_session_token(user.id)
    response.set_cookie(
        key="session",
        value=raw_token,
        max_age=86_400,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    # The cookie is the documented browser contract. This header also makes
    # the opaque token convenient for API clients that prefer Authorization.
    response.headers["X-Session-Token"] = raw_token
    return raw_token


@router.post("/magic-link", response_model=MagicLinkResponse, operation_id="signIn")
def request_magic_link(
    payload: MagicLinkRequest,
    response: Response,
    store: DatabaseStore = Depends(get_store),
) -> MagicLinkResponse:
    email = str(payload.email).lower()
    with store.lock:
        user = store.find_user_by_email(email)
        if user is None:
            user = store.create_user(
                email=email,
                display_name=_display_name_for_email(email),
                organization_id=None,
                password_hash_value=hash_password("magic-link-only:" + email),
            )
        _set_session_cookie(response, store, user)
        return MagicLinkResponse(user=store.public_user(user), expires_in=900)


@router.post(
    "/login",
    response_model=PasswordLoginResponse,
    include_in_schema=False,
)
def password_login(
    payload: PasswordLoginRequest,
    response: Response,
    store: DatabaseStore = Depends(get_store),
) -> PasswordLoginResponse:
    """Local password login for API clients; the contract's UI uses magic links."""

    with store.lock:
        user = store.find_user_by_email(str(payload.email).lower())
        if user is None or not verify_password(payload.password, user.password_hash):
            raise APIError("invalid_credentials", "Email or password is incorrect.", 401)
        raw_token = _set_session_cookie(response, store, user)
        return PasswordLoginResponse(
            access_token=raw_token,
            token_type="bearer",
            user=store.public_user(user),
        )
