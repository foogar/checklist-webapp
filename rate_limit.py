"""Per-identity request rate limiting.

Keyed on the signed-in user's id when there is a session, and on the client IP
otherwise. That distinction is the point: an IP-keyed limit cannot tell one
account hammering the API apart from a whole household behind one router, which
is how this app is actually used.

Counters live in memory, so each uvicorn worker keeps its own. Run a single
worker (the default) or the effective limit becomes N times what is configured.
"""
import os
import threading
import time
from collections import deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def _env_flag(name, default):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name, default):
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Request paths that are cheap to abuse and worth watching more closely.
AUTH_PREFIX = "/auth/"

# Prune idle buckets periodically so a flood of unique keys cannot grow the
# dict without bound.
SWEEP_INTERVAL_SECONDS = 300


class SlidingWindowLimiter:
    """Fixed-memory sliding window counter.

    One deque of request timestamps per key. Old timestamps are dropped as they
    age out, so a key's memory is bounded by its own rate inside the window.
    """

    def __init__(self):
        self._buckets = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check(self, tier, key, limit, window):
        """Return (allowed, retry_after_seconds).

        The tier is part of the bucket key. Without it the auth and general
        limits would draw on one shared counter, so a burst of ordinary API
        traffic would exhaust the much smaller auth allowance and lock the
        user out of logging in.
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

    def _sweep(self, now):
        """Drop buckets that have gone quiet.

        Each bucket carries the window it was last used with, so idle entries
        are reclaimed without scanning every timestamp.
        """
        stale = [
            key
            for key, bucket in self._buckets.items()
            if not bucket or now - bucket[-1] > SWEEP_INTERVAL_SECONDS * 4
        ]
        for key in stale:
            del self._buckets[key]

    def reset(self):
        with self._lock:
            self._buckets.clear()
            self._last_sweep = time.monotonic()


limiter = SlidingWindowLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Applies the general limit to everything, with a tighter one on /auth/.

    Must be added before SessionMiddleware so that the session is already
    populated by the time this runs; Starlette runs the most recently added
    middleware outermost.
    """

    def __init__(self, app, enabled, general_limit, general_window,
                 auth_limit, auth_window):
        super().__init__(app)
        self.enabled = enabled
        self.general_limit = general_limit
        self.general_window = general_window
        self.auth_limit = auth_limit
        self.auth_window = auth_window

    async def dispatch(self, request, call_next):
        if not self.enabled:
            return await call_next(request)

        path = request.url.path
        if path.startswith(AUTH_PREFIX):
            tier, limit, window = "auth", self.auth_limit, self.auth_window
        else:
            tier, limit, window = "general", self.general_limit, self.general_window

        allowed, retry_after = limiter.check(tier, self._identity(request), limit, window)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        return response

    @staticmethod
    def _identity(request):
        """Prefer the signed-in user; fall back to the caller's address.

        Reading the raw cookie instead would let a caller reset their own
        counter just by sending a different value.
        """
        user = request.session.get("user")
        if isinstance(user, dict) and user.get("id") is not None:
            return f"user:{user['id']}"
        client = request.client
        return f"ip:{client.host}" if client else "ip:unknown"


def install(app):
    """Register the middleware with configuration from the environment."""
    app.add_middleware(
        RateLimitMiddleware,
        enabled=_env_flag("RATE_LIMIT_ENABLED", True),
        general_limit=_env_int("RATE_LIMIT_REQUESTS", 600),
        general_window=_env_int("RATE_LIMIT_WINDOW", 60),
        auth_limit=_env_int("RATE_LIMIT_AUTH_REQUESTS", 20),
        auth_window=_env_int("RATE_LIMIT_AUTH_WINDOW", 60),
    )
