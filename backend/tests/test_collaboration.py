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
