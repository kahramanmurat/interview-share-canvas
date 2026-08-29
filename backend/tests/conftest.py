from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.store import DatabaseStore


@pytest.fixture
def app():
    store = DatabaseStore("sqlite+pysqlite:///:memory:", seed=True)
    application = create_app(store)
    yield application
    store.close()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def owner_token(client):
    response = client.post(
        "/v1/auth/magic-link",
        json={"email": "dana@northwind.dev"},
    )
    assert response.status_code == 200
    return response.headers["x-session-token"]


def get_live_session(client, owner_token):
    response = client.get(
        "/v1/sessions",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert response.status_code == 200
    return next(session for session in response.json() if session["state"] == "live")
