from __future__ import annotations

from .conftest import get_live_session


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def test_guest_link_preview_join_and_bearer_canvas_access(client, owner_token):
    live = get_live_session(client, owner_token)
    link_response = client.post(
        f"/v1/sessions/{live['id']}/guest-links",
        headers=auth_header(owner_token),
        json={},
    )
    assert link_response.status_code == 201
    link = link_response.json()
    raw_token = link["token"]
    assert len(raw_token) == 32
    assert raw_token not in link["link"]["token_hash"]

    preview = client.get(f"/v1/join/{raw_token}")
    assert preview.status_code == 200
    assert preview.json()["session_id"] == live["id"]
    assert "prompt" not in preview.json()

    joined = client.post(
        f"/v1/join/{raw_token}",
        json={"display_name": "Candidate One"},
    )
    assert joined.status_code == 201
    joined_body = joined.json()
    collab_token = joined_body["collab_token"]
    assert joined_body["participant"]["role"] == "candidate"

    canvas = client.get(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(collab_token),
    )
    assert canvas.status_code == 200
    document = canvas.json()["doc"]
    assert len(document["nodes"]) == 5

    saved = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(collab_token),
        json={"actor": "owner", "client_operation_id": "op-1", "doc": document},
    )
    assert saved.status_code == 200
    assert saved.json()["operation_cursor"] == 1

    repeated = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(collab_token),
        json={"client_operation_id": "op-1", "doc": document},
    )
    assert repeated.status_code == 200
    assert repeated.json()["operation_cursor"] == 1


def test_candidate_lock_is_enforced_from_principal_not_actor_hint(client, owner_token):
    live = get_live_session(client, owner_token)
    link = client.post(
        f"/v1/sessions/{live['id']}/guest-links",
        headers=auth_header(owner_token),
        json={},
    ).json()
    joined = client.post(
        f"/v1/join/{link['token']}",
        json={"display_name": "Locked Candidate"},
    ).json()
    collab_token = joined["collab_token"]
    document = client.get(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(collab_token),
    ).json()["doc"]

    locked = client.patch(
        f"/v1/sessions/{live['id']}",
        headers=auth_header(owner_token),
        json={"candidate_editing_enabled": False},
    )
    assert locked.status_code == 200
    rejected = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(collab_token),
        json={"actor": "owner", "doc": document},
    )
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "editing_locked"


def test_canvas_element_limit_returns_payload_too_large(client, owner_token):
    live = get_live_session(client, owner_token)
    document = client.get(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
    ).json()["doc"]
    document["nodes"] = [
        {"id": f"node-{index}", "type": "generic", "label": "Node", "x": 0, "y": 0, "w": 96, "h": 56}
        for index in range(2_000)
    ]
    document["edges"] = [
        {"id": "edge-too-many", "from": "node-0", "to": "node-1", "label": "", "style": "elbow", "arrowEnd": True}
    ]
    response = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
        json={"doc": document},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "document_too_large"


def test_rotating_and_revoking_guest_links_invalidates_old_token(client, owner_token):
    live = get_live_session(client, owner_token)
    first = client.post(
        f"/v1/sessions/{live['id']}/guest-links",
        headers=auth_header(owner_token),
        json={},
    ).json()
    second = client.post(
        f"/v1/sessions/{live['id']}/guest-links",
        headers=auth_header(owner_token),
        json={},
    ).json()
    assert client.get(f"/v1/join/{first['token']}").status_code == 403

    revoked = client.delete(
        f"/v1/sessions/{live['id']}/guest-links/{second['link']['id']}",
        headers=auth_header(owner_token),
    )
    assert revoked.status_code == 200
    assert client.get(f"/v1/join/{second['token']}").status_code == 403


def test_export_contains_saved_canvas(client, owner_token):
    live = get_live_session(client, owner_token)
    exported = client.get(
        f"/v1/sessions/{live['id']}/export",
        headers=auth_header(owner_token),
    )
    assert exported.status_code == 200
    assert exported.json()["session"]["id"] == live["id"]
    assert set(exported.json()["canvas"]) == {"nodes", "edges", "strokes"}
