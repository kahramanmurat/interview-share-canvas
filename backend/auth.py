"""Password hashing and bearer/cookie authentication helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

from .errors import APIError


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 310_000
DEMO_USER_EMAIL = "dana@northwind.dev"
DEMO_USER_PASSWORD = "northwind-demo-password"


def hash_password(password: str) -> str:
    """Hash a password with a salted, deliberately expensive KDF."""

    if not password:
        raise ValueError("Password cannot be empty.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_ITERATIONS),
            salt.hex(),
            digest.hex(),
        )
    )


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password without exposing whether the stored hash is valid."""

    if not isinstance(password, str):
        return False
    try:
        scheme, iterations_text, salt_hex, digest_hex = password_hash.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def issue_token(prefix: str = "") -> str:
    """Create an opaque token; only its hash is persisted by the store."""

    return f"{prefix}{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "collab"]
    user_id: str | None = None
    session_id: str | None = None
    participant_id: str | None = None
    role: str | None = None


def authenticate_token(raw_token: str | None, store: "DatabaseStore") -> Principal | None:
    if not raw_token:
        return None

    collab = store.get_collab_token(raw_token)
    if collab is not None:
        participant = store.participants.get(collab.participant_id)
        session = store.sessions.get(collab.session_id)
        if (
            participant is not None
            and participant.left_at is None
            and session is not None
        ):
            return Principal(
                kind="collab",
                session_id=collab.session_id,
                participant_id=collab.participant_id,
                role=participant.role,
            )
        return None

    user_token = store.get_session_token(raw_token)
    if user_token is None or store.users.get(user_token.user_id) is None:
        return None
    return Principal(kind="user", user_id=user_token.user_id)


def require_user(principal: Principal | None) -> Principal:
    if principal is None or principal.kind != "user":
        raise APIError("unauthenticated", "Sign in is required.", 401)
    return principal


def require_principal(principal: Principal | None) -> Principal:
    if principal is None:
        raise APIError("unauthenticated", "Sign in is required.", 401)
    return principal


# Imported only for type checking to avoid a store/auth import cycle.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import DatabaseStore
