"""Small shared helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> str:
    """ISO 8601 UTC, second precision, with a Z suffix.

    Consistent with what the frontend's Date parser expects. SQLite's
    CURRENT_TIMESTAMP uses a space separator and no zone, which browsers
    interpret as local time, so rows are written from here instead.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
