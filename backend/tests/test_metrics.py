"""The domain metrics, read back through a real in-memory meter provider."""

from __future__ import annotations

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from .conftest import get_live_session


@pytest.fixture(scope="module", autouse=True)
def meter_reader():
    """Install one real meter provider for this module and read from it.

    The instruments in backend.metrics are proxies until a provider is set, and
    they rebind when it is, so the application code under test needs no seam.
    """
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return reader


def points(reader, name):
    """Every data point recorded for one metric, newest collection only."""
    data = reader.get_metrics_data()
    collected = []
    if data is None:
        # Nothing has been recorded yet in this process.
        return collected
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    collected.extend(metric.data.data_points)
    return collected


def total(reader, name, **attributes):
    """The summed value of one metric, optionally filtered by attributes."""
    return sum(
        point.value
        for point in points(reader, name)
        if all(point.attributes.get(key) == value for key, value in attributes.items())
    )


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def test_creating_an_interview_counts_a_room(client, owner_token, meter_reader):
    before = total(meter_reader, "interview.rooms.created", **{"room.source": "new"})

    response = client.post(
        "/v1/sessions",
        headers=auth_header(owner_token),
        json={"title": "Metrics interview", "prompt": "Design a queue", "duration_minutes": 45},
    )
    assert response.status_code == 201

    after = total(meter_reader, "interview.rooms.created", **{"room.source": "new"})
    assert after == before + 1


def test_duplicating_an_interview_counts_its_own_source(client, owner_token, meter_reader):
    live = get_live_session(client, owner_token)
    before = total(meter_reader, "interview.rooms.created", **{"room.source": "duplicate"})

    response = client.post(
        f"/v1/sessions/{live['id']}/duplicate",
        headers=auth_header(owner_token),
    )
    assert response.status_code == 201

    after = total(meter_reader, "interview.rooms.created", **{"room.source": "duplicate"})
    assert after == before + 1


def test_a_connected_participant_is_active_only_while_connected(
    client, owner_token, meter_reader
):
    live = get_live_session(client, owner_token)
    name = "interview.participants.active"
    before = total(meter_reader, name, **{"participant.role": "owner"})

    with client.websocket_connect(
        f"/v1/rooms/{live['id']}",
        headers=auth_header(owner_token),
    ) as websocket:
        assert websocket.receive_json() == {"type": "status", "status": "connected"}
        during = total(meter_reader, name, **{"participant.role": "owner"})
        assert during == before + 1

    after = total(meter_reader, name, **{"participant.role": "owner"})
    assert after == before


def test_saving_a_canvas_counts_the_elements_it_added(client, owner_token, meter_reader):
    live = get_live_session(client, owner_token)
    document = client.get(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
    ).json()["doc"]
    name = "canvas.elements.created"
    before_nodes = total(meter_reader, name, **{"element.kind": "node"})
    before_edges = total(meter_reader, name, **{"element.kind": "edge"})

    document["nodes"].append(
        {"id": "n-metric", "type": "service", "label": "Metrics", "x": 10, "y": 10, "w": 120, "h": 60}
    )
    document["nodes"].append(
        {"id": "n-metric-2", "type": "sql", "label": "Store", "x": 20, "y": 20, "w": 120, "h": 60}
    )
    response = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
        json={"client_operation_id": "op-metrics-1", "doc": document},
    )
    assert response.status_code == 200

    assert total(meter_reader, name, **{"element.kind": "node"}) == before_nodes + 2
    # Nothing was added to the other collections, so nothing was counted there.
    assert total(meter_reader, name, **{"element.kind": "edge"}) == before_edges


def test_deleting_elements_counts_nothing(client, owner_token, meter_reader):
    live = get_live_session(client, owner_token)
    document = client.get(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
    ).json()["doc"]
    name = "canvas.elements.created"
    before = total(meter_reader, name, **{"element.kind": "node"})

    document["nodes"] = document["nodes"][:1]
    response = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
        json={"client_operation_id": "op-metrics-delete", "doc": document},
    )
    assert response.status_code == 200

    assert total(meter_reader, name, **{"element.kind": "node"}) == before


def test_a_rejected_canvas_write_counts_a_creation_failure(client, owner_token, meter_reader):
    live = get_live_session(client, owner_token)
    document = client.get(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
    ).json()["doc"]
    name = "canvas.elements.creation_failures"
    before = total(meter_reader, name, **{"failure.reason": "document_too_large"})

    document["nodes"] = [
        {"id": f"n-{index}", "type": "service", "label": "x", "x": 0, "y": 0, "w": 100, "h": 60}
        for index in range(1_500)
    ]
    document["edges"] = [
        {"id": f"e-{index}", "from": "n-0", "to": "n-1", "label": "", "style": "straight", "arrowEnd": True}
        for index in range(600)
    ]
    response = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
        json={"client_operation_id": "op-metrics-large", "doc": document},
    )
    assert response.status_code == 413

    after = total(meter_reader, name, **{"failure.reason": "document_too_large"})
    assert after == before + 1


def test_a_malformed_canvas_body_counts_a_creation_failure(client, owner_token, meter_reader):
    live = get_live_session(client, owner_token)
    name = "canvas.elements.creation_failures"
    before = total(meter_reader, name, **{"failure.reason": "validation_error"})

    response = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(owner_token),
        json={"doc": {"nodes": [{"id": "broken"}], "edges": [], "strokes": []}},
    )
    assert response.status_code == 400

    after = total(meter_reader, name, **{"failure.reason": "validation_error"})
    assert after == before + 1


def test_a_locked_candidate_write_counts_its_own_reason(client, owner_token, meter_reader):
    live = get_live_session(client, owner_token)
    client.patch(
        f"/v1/sessions/{live['id']}",
        headers=auth_header(owner_token),
        json={"candidate_editing_enabled": False},
    )
    link = client.post(
        f"/v1/sessions/{live['id']}/guest-links",
        headers=auth_header(owner_token),
        json={},
    ).json()
    candidate = client.post(
        f"/v1/join/{link['token']}",
        json={"display_name": "Locked Out"},
    ).json()
    document = client.get(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(candidate["collab_token"]),
    ).json()["doc"]
    name = "canvas.elements.creation_failures"
    before = total(meter_reader, name, **{"failure.reason": "editing_locked"})

    response = client.post(
        f"/v1/sessions/{live['id']}/canvas",
        headers=auth_header(candidate["collab_token"]),
        json={"client_operation_id": "op-metrics-locked", "doc": document},
    )
    assert response.status_code == 403

    after = total(meter_reader, name, **{"failure.reason": "editing_locked"})
    assert after == before + 1
