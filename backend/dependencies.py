"""FastAPI dependencies shared by routers."""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from .auth import Principal, authenticate_token, require_principal, require_user
from .store import InMemoryStore


def get_store(request: Request) -> InMemoryStore:
    return request.app.state.store


session_cookie_scheme = APIKeyCookie(
    name="session",
    scheme_name="sessionCookie",
    auto_error=False,
)
bearer_scheme = HTTPBearer(
    scheme_name="collabBearer",
    auto_error=False,
)


def authenticated_principal(
    session_cookie: str | None = Depends(session_cookie_scheme),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    store: InMemoryStore = Depends(get_store),
) -> Principal | None:
    raw_token = credentials.credentials if credentials is not None else session_cookie
    with store.lock:
        return authenticate_token(raw_token, store)


def current_user(
    principal: Principal | None = Depends(authenticated_principal),
) -> Principal:
    return require_user(principal)


def current_principal(
    principal: Principal | None = Depends(authenticated_principal),
) -> Principal:
    return require_principal(principal)
