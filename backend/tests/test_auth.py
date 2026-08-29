from __future__ import annotations

from backend.auth import DEMO_USER_PASSWORD


def test_protected_endpoint_requires_authentication(client):
    response = client.get("/v1/sessions")

    assert response.status_code == 401
    assert response.json() == {
        "code": "unauthenticated",
        "message": "Sign in is required.",
    }


def test_magic_link_establishes_cookie_and_lists_seeded_sessions(client):
    sign_in = client.post(
        "/v1/auth/magic-link",
        json={"email": "dana@northwind.dev"},
    )

    assert sign_in.status_code == 200
    assert sign_in.json()["user"]["display_name"] == "Dana Reyes"
    assert "session" in sign_in.cookies
    sessions = client.get("/v1/sessions")
    assert sessions.status_code == 200
    assert len(sessions.json()) == 4


def test_invalid_email_uses_contract_error_shape(client):
    response = client.post(
        "/v1/auth/magic-link",
        json={"email": "not-an-email"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_passwords_are_hashed_and_password_login_returns_bearer_token(client, app):
    user = app.state.store.users["usr_owner"]
    assert user.password_hash != DEMO_USER_PASSWORD
    assert "$" in user.password_hash

    bad_login = client.post(
        "/v1/auth/login",
        json={"email": "dana@northwind.dev", "password": "wrong"},
    )
    assert bad_login.status_code == 401

    login = client.post(
        "/v1/auth/login",
        json={"email": "dana@northwind.dev", "password": DEMO_USER_PASSWORD},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert login.json()["token_type"] == "bearer"
    assert client.get(
        "/v1/sessions",
        headers={"Authorization": f"Bearer {token}"},
    ).status_code == 200
