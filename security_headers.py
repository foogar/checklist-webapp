"""Security response headers, applied to every response including errors.

These live in the application rather than in proxy configuration so they are
version controlled and testable. A proxy in front still needs its own copy:
this only covers responses the app actually produced, not a 502 from a dead
origin.
"""

from __future__ import annotations

from config import settings
from web import Request, Response

# With no inline scripts and no eval-based template engine, the policy needs no
# nonce and no unsafe-* escape hatch. Everything the page loads is same-origin.
#
# require-trusted-types-for is Chrome-only and simply ignored elsewhere. It is
# safe here because the frontend never assigns strings to innerHTML: rows are
# cloned from <template> and filled with textContent.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "require-trusted-types-for 'script'",
    ]
)

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-DNS-Prefetch-Control": "off",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def apply(request: Request, response: Response) -> Response:
    for name, value in HEADERS.items():
        response.headers.setdefault(name, value)

    if settings.session_cookie_secure:
        # Tied to the secure-cookie flag: emitting HSTS over plain HTTP would
        # make a local or http-only deployment unreachable.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )

    if settings.csp_enabled:
        response.headers.setdefault("Content-Security-Policy", CSP)

    # Per-user JSON must not sit in a browser or intermediary cache.
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")

    return response
