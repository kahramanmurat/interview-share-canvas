from __future__ import annotations

from .conftest import get_live_session


def test_owner_can_join_collaboration_room_with_bearer_token(client, owner_token):
    live = get_live_session(client, owner_token)
    with client.websocket_connect(
        f"/v1/rooms/{live['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
    ) as websocket:
        assert websocket.receive_json() == {"type": "status", "status": "connected"}
        websocket.send_json(
            {
                "type": "join_room",
                "session_id": live["id"],
            }
        )
        joined = websocket.receive_json()
        assert joined["type"] == "room_joined"
        assert joined["session_id"] == live["id"]
        assert joined["operation_cursor"] == 0
        assert websocket.receive_json()["type"] == "presence_snapshot"


def test_browser_can_join_collaboration_room_with_query_token(client, owner_token):
    live = get_live_session(client, owner_token)
    with client.websocket_connect(
        f"/v1/rooms/{live['id']}?access_token={owner_token}",
    ) as websocket:
        assert websocket.receive_json() == {"type": "status", "status": "connected"}
        websocket.send_json({"type": "join_room", "payload": {"session_id": live["id"]}})
        assert websocket.receive_json()["type"] == "room_joined"


def test_query_collaboration_token_takes_precedence_over_user_cookie(client, owner_token):
    live = get_live_session(client, owner_token)
    link_response = client.post(
        f"/v1/sessions/{live['id']}/guest-links",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={},
    )
    guest_token = link_response.json()["token"]
    join_response = client.post(
        f"/v1/join/{guest_token}",
        json={"display_name": "Browser Candidate"},
    )
    participant = join_response.json()["participant"]
    collaboration_token = join_response.json()["collab_token"]

    with client.websocket_connect(
        f"/v1/rooms/{live['id']}?access_token={collaboration_token}",
    ) as websocket:
        assert websocket.receive_json() == {"type": "status", "status": "connected"}
        websocket.send_json(
            {
                "type": "join_room",
                "payload": {
                    "session_id": live["id"],
                    "participant_id": participant["id"],
                },
            }
        )
        joined = websocket.receive_json()
        assert joined["type"] == "room_joined"
        assert next(item for item in joined["participants"] if item.get("you"))["id"] == participant["id"]
