"""Application metrics for the interview domain.

The environment and the deployed version are deliberately not attributes here.
They are resource attributes carried by every signal this process exports, set
once in ``backend.telemetry``, so each metric below is already split by
environment and by deployed version without every call site repeating them.

The instruments are created at import. The metrics API hands out a proxy until
``backend.telemetry`` installs a real meter provider, so they record nothing
when telemetry is off and start recording the moment it is configured.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry import metrics

meter = metrics.get_meter("interview-share-canvas")

rooms_created = meter.create_counter(
    "interview.rooms.created",
    unit="{room}",
    description="Interview rooms created. Every interview is one collaboration room.",
)

participants_active = meter.create_up_down_counter(
    "interview.participants.active",
    unit="{participant}",
    description="Participants currently connected to a collaboration room.",
)

elements_created = meter.create_counter(
    "canvas.elements.created",
    unit="{element}",
    description="Canvas elements a saved document added, counted per kind.",
)

element_creation_failures = meter.create_counter(
    "canvas.elements.creation_failures",
    unit="{failure}",
    description="Canvas writes rejected, so the elements they carried were never created.",
)

# The document's element collections, and the singular kind reported for each.
ELEMENT_KINDS = {"nodes": "node", "edges": "edge", "strokes": "stroke"}


def record_room_created(source: str) -> None:
    """Count an interview room. ``source`` is how it came about."""
    rooms_created.add(1, {"room.source": source})


def record_participant_connected(role: str) -> None:
    participants_active.add(1, {"participant.role": role})


def record_participant_disconnected(role: str) -> None:
    participants_active.add(-1, {"participant.role": role})


def record_elements_created(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> None:
    """Count what a saved document added, per kind of element.

    A canvas write replaces the whole document, so the elements created are the
    rise in each kind's count. A deletion is not a negative creation, so a kind
    that shrank contributes nothing.
    """
    for collection, kind in ELEMENT_KINDS.items():
        before = len(previous.get(collection) or ()) if previous else 0
        after = len(current.get(collection) or ())
        if after > before:
            elements_created.add(after - before, {"element.kind": kind})


def record_element_creation_failure(reason: str) -> None:
    """Count a rejected canvas write. ``reason`` is the error code returned."""
    element_creation_failures.add(1, {"failure.reason": reason})
