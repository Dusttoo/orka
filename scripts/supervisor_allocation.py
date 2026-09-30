#!/usr/bin/env python3
"""Deterministic, starvation-bounded lane allocation for the supervisor."""

from __future__ import annotations

from typing import Any


class AllocationError(RuntimeError):
    pass


QUEUE_CLASSES = (
    "repair",
    "recovery",
    "continuation",
    "dependency_unlocking",
    "fresh",
)
WEIGHTED_RING = (
    "repair",
    "recovery",
    "repair",
    "continuation",
    "recovery",
    "fresh",
)
ACTION_BY_CLASS = {
    "repair": "repair",
    "recovery": "recovery",
    "continuation": "launch",
    "dependency_unlocking": "launch",
    "fresh": "launch",
}


def _queues(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise AllocationError("allocation candidates must be an object")
    result: dict[str, list[str]] = {}
    seen: set[str] = set()
    for queue_class in QUEUE_CLASSES:
        raw = value.get(queue_class, [])
        if not isinstance(raw, list) or any(
            not isinstance(ticket, str) or not ticket for ticket in raw
        ):
            raise AllocationError(f"{queue_class} candidates must be ticket strings")
        result[queue_class] = []
        for ticket in raw:
            if ticket in seen:
                continue
            seen.add(ticket)
            result[queue_class].append(ticket)
    return result


def allocate_lanes(
    candidates: Any,
    *,
    available: int,
    concurrency: int,
    cursor: int = 0,
) -> dict[str, Any]:
    """Select ordered work without consuming skipped controller candidates.

    A one-lane repository retains strict finish-first ordering. Multi-lane
    repositories reserve one available lane for dependency-unlocking work and
    rotate the remaining capacity through a weighted durable cursor.
    """

    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise AllocationError("available lane count must be a nonnegative integer")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency < 1
    ):
        raise AllocationError("concurrency must be a positive integer")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise AllocationError("allocation cursor must be a nonnegative integer")

    queues = _queues(candidates)
    selections: list[dict[str, Any]] = []

    def select(queue_class: str, reason: str) -> None:
        ticket = queues[queue_class].pop(0)
        selections.append(
            {
                "slot": len(selections) + 1,
                "ticket": ticket,
                "class": queue_class,
                "action": ACTION_BY_CLASS[queue_class],
                "reason": reason,
            }
        )

    if available == 0:
        return {"selections": [], "next_cursor": cursor % len(WEIGHTED_RING)}

    if concurrency == 1:
        for queue_class in QUEUE_CLASSES:
            if queues[queue_class]:
                select(queue_class, "single-lane-finish-first")
                break
        return {"selections": selections, "next_cursor": cursor % len(WEIGHTED_RING)}

    if queues["dependency_unlocking"]:
        select("dependency_unlocking", "reserved-dependency-unlocking-lane")

    next_cursor = cursor % len(WEIGHTED_RING)
    while len(selections) < available:
        matched = False
        for offset in range(len(WEIGHTED_RING)):
            index = (next_cursor + offset) % len(WEIGHTED_RING)
            queue_class = WEIGHTED_RING[index]
            if not queues[queue_class]:
                continue
            select(queue_class, f"weighted-fair-share:{queue_class}")
            next_cursor = (index + 1) % len(WEIGHTED_RING)
            matched = True
            break
        if matched:
            continue
        if queues["dependency_unlocking"]:
            select("dependency_unlocking", "additional-dependency-unlocking-capacity")
            continue
        break

    return {"selections": selections, "next_cursor": next_cursor}
