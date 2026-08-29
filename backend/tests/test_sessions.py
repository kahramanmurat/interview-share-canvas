from __future__ import annotations

from .conftest import get_live_session


def test_session_lifecycle_and_duplicate(client, owner_token):
    created = client.post(
        "/v1/sessions",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "title": "Staff backend interview",
            "prompt": "Design a rate limiter.",
            "duration_minutes": 45,
            "template_id": "shortener",
            "editing": False,
        },
    )
    assert created.status_code == 201
    session = created.json()
    assert session["state"] == "draft"
    assert session["candidate_editing_enabled"] is False
    assert len(session["participants"]) == 1

    session_id = session["id"]
    started = client.post(
        f"/v1/sessions/{session_id}/start",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert started.status_code == 200
    assert started.json()["state"] == "live"

    patched = client.patch(
        f"/v1/sessions/{session_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"title": "Renamed interview", "candidate_editing_enabled": True},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed interview"

    duplicate = client.post(
        f"/v1/sessions/{session_id}/duplicate",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["title"] == "Renamed interview (copy)"
    assert duplicate.json()["state"] == "draft"

    ended = client.post(
        f"/v1/sessions/{session_id}/end",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert ended.status_code == 200
    assert ended.json()["state"] == "ended"
    assert ended.json()["candidate_editing_enabled"] is False


def test_session_metadata_is_owner_or_member_scoped(client, owner_token):
    live = get_live_session(client, owner_token)
    other = client.post(
        "/v1/auth/magic-link",
        json={"email": "someone@example.com"},
    )
    other_token = other.headers["x-session-token"]

    forbidden = client.get(
        f"/v1/sessions/{live['id']}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "forbidden"


def test_archive_and_audit(client, owner_token):
    live = get_live_session(client, owner_token)
    archived = client.patch(
        f"/v1/sessions/{live['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"state": "archived"},
    )
    assert archived.status_code == 200
    assert archived.json()["state"] == "archived"

    audit = client.get(
        f"/v1/sessions/{live['id']}/audit",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert audit.status_code == 200
    assert all("action" in event and "at" in event for event in audit.json())


def test_only_owner_can_delete_session_and_related_records(client, app, owner_token):
    live = get_live_session(client, owner_token)
    session_id = live["id"]
    other = client.post(
        "/v1/auth/magic-link",
        json={"email": "other-interviewer@example.com"},
    )
    other_token = other.headers["x-session-token"]

    forbidden = client.delete(
        f"/v1/sessions/{session_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert forbidden.status_code == 403

    deleted = client.delete(
        f"/v1/sessions/{session_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert session_id not in app.state.store.sessions
    assert session_id not in app.state.store.canvases
    assert not any(
        participant.session_id == session_id
        for participant in app.state.store.participants.values()
    )

    missing = client.get(
        f"/v1/sessions/{session_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert missing.status_code == 404
