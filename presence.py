"""Who is looking at a list right now.

In-memory on purpose: presence is ephemeral, worthless after a restart, and a
table would mean a write on every poll. A single worker is the documented
deployment, so a process-local map is the whole state.

Entries expire on their own. A client that stops polling (closed tab, lost
connection) disappears within the TTL without anyone cleaning up.
"""

from __future__ import annotations

import threading
import time
from typing import Any

TTL_SECONDS = 45.0

_lock = threading.Lock()
# list_id -> {client_id: (user_id, name, last_seen_monotonic)}
_presence: dict[int, dict[str, tuple[int, str, float]]] = {}


def touch(list_id: int, client_id: str, user_id: int, name: str) -> None:
    now = time.monotonic()
    with _lock:
        bucket = _presence.setdefault(list_id, {})
        bucket[client_id] = (user_id, name, now)
        for key, (_, _, seen) in list(bucket.items()):
            if now - seen > TTL_SECONDS:
                del bucket[key]
        if not bucket:
            _presence.pop(list_id, None)


def snapshot(list_id: int) -> list[dict[str, Any]]:
    now = time.monotonic()
    with _lock:
        bucket = _presence.get(list_id, {})
        return [
            {"client_id": client_id, "user_id": user_id, "name": name}
            for client_id, (user_id, name, seen) in bucket.items()
            if now - seen <= TTL_SECONDS
        ]


def clear() -> None:
    """Only used by tests."""
    with _lock:
        _presence.clear()
