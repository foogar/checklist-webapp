"""Per-identity request rate limiting.

Keyed on the signed-in user's id when there is a session, and on the client IP
otherwise. An IP-keyed limit cannot tell one account hammering the API apart
from a whole household behind one router, which is how this app is used.

Counters live in process memory, so each uvicorn worker keeps its own. Run a
single worker, or the effective limit multiplies by the worker count.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from config import settings
from web import Request, Response, json_response

AUTH_PREFIX = "/auth/"
SWEEP_INTERVAL_SECONDS = 300


class SlidingWindowLimiter:
    """Fixed-memory sliding window counter.

    One deque of timestamps per key. Old timestamps are dropped as they age
    out, so a key's memory is bounded by its own rate inside the window.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check(self, tier: str, key: str, limit: int, window: int) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        The tier is part of the bucket key: otherwise a burst of ordinary API
        traffic would exhaust the much smaller auth allowance and lock the user
        out of logging in.
        """
        now = time.monotonic()
        bucket_key = f"{tier}:{key}"

        with self._lock:
            if now - self._last_sweep > SWEEP_INTERVAL_SECONDS:
                self._sweep(now)
                self._last_sweep = now

            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                bucket = deque()
                self._buckets[bucket_key] = bucket

            while bucket and now - bucket[0] > window:
                bucket.popleft()

            if len(bucket) >= limit:
                # Not a lockout: the window slides, so the caller recovers on
                # their own once it drains.
                retry_after = max(1, int(window - (now - bucket[0])) + 1)
                return False, retry_after

            bucket.append(now)
            return True, 0

    def _sweep(self, now: float) -> None:
        stale = [
            key
            for key, bucket in self._buckets.items()
            if not bucket or now - bucket[-1] > SWEEP_INTERVAL_SECONDS * 4
        ]
        for key in stale:
            del self._buckets[key]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._last_sweep = time.monotonic()


limiter = SlidingWindowLimiter()


def _identity(request: Request) -> str:
    uid = request.session.get("uid")
    if isinstance(uid, int):
        return f"user:{uid}"
    return f"ip:{request.client_host}"


def check(request: Request) -> Response | None:
    """Return a 429 response when over the limit, otherwise None."""
    if not settings.rate_limit_enabled:
        return None

    if request.path.startswith(AUTH_PREFIX):
        tier = "auth"
        limit = settings.rate_limit_auth_requests
        window = settings.rate_limit_auth_window
    else:
        tier = "general"
        limit = settings.rate_limit_requests
        window = settings.rate_limit_window

    allowed, retry_after = limiter.check(tier, _identity(request), limit, window)
    request.state["ratelimit_limit"] = limit
    if allowed:
        return None

    return json_response(
        {"detail": "Too many requests. Please slow down."},
        status=429,
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
        },
    )
