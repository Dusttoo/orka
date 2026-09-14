#!/usr/bin/env python3
"""Validate controller-issued attempt capabilities against durable sprint state."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class AttemptCapabilityError(RuntimeError):
    pass


@contextmanager
def locked_validation(
    *, state_dir: Path, token: str, repository: str, sprint: str,
    ticket: str, role: str, run_id: str, worker: str, route: dict | None = None,
) -> Iterator[None]:
    matches: list[tuple[Path, dict]] = []
    for path in state_dir.glob("*.json"):
        if path.name.startswith("batch-"):
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        item = (state.get("tickets") or {}).get(ticket)
        capability = item.get("attempt_capability") if isinstance(item, dict) else None
        if isinstance(capability, dict) and capability.get("token") == token:
            matches.append((path, capability))
    if len(matches) != 1:
        raise AttemptCapabilityError("attempt capability is missing or ambiguous")
    path, capability = matches[0]
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = json.loads(path.read_text(encoding="utf-8"))
        item = (state.get("tickets") or {}).get(ticket) or {}
        current = item.get("attempt_capability") or {}
        expected = {
            "token": token, "repository": repository, "sprint": sprint,
            "ticket": ticket, "role": role, "run_id": run_id, "worker": worker,
        }
        if item.get("state") != "running" or any(current.get(k) != v for k, v in expected.items()):
            raise AttemptCapabilityError("attempt capability is stale or does not match the active lane")
        if current.get("attempt") != item.get("attempts"):
            raise AttemptCapabilityError("attempt capability is not bound to the current attempt number")

        if route is not None and item.get("reserved_route") is not None:
            from provider_health import route_identity
            if route_identity(route) != route_identity(item["reserved_route"]):
                raise AttemptCapabilityError("worker route differs from its reserved attempt")
        yield


def validate(
    *, state_dir: Path, token: str, repository: str, sprint: str,
    ticket: str, role: str, run_id: str, worker: str, route: dict | None = None,
) -> None:
    with locked_validation(
        state_dir=state_dir,
        token=token,
        repository=repository,
        sprint=sprint,
        ticket=ticket,
        role=role,
        run_id=run_id,
        worker=worker,
        route=route,
    ):
        pass
