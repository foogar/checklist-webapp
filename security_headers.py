"""Standard security response headers.

Applied at the application so they are version controlled, testable, and not
dependent on edge configuration. Set the same headers in a Cloudflare Transform
Rule as well, since these only cover responses the application actually
produced; a 502 or 521 from a proxy in front of a stopped app would not carry
them.
"""
from starlette.middleware.base import BaseHTTPMiddleware

# Deliberately no CSP here. The app loads Tailwind from a CDN that compiles
# CSS in the browser via eval, plus Alpine from a CDN, with inline scripts in
# the template. A policy tight enough to be worth having would need
# 'unsafe-inline' and 'unsafe-eval', which largely defeats the point. Adding
# one properly means building the CSS at deploy time instead of running the
# Tailwind CDN, which is a separate piece of work.
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-DNS-Prefetch-Control": "off",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, hsts_enabled):
        super().__init__(app)
        # HSTS tells the browser to refuse plaintext for this host from now on.
        # Emitting it over plain HTTP would make a local or http-only deployment
        # unreachable, so it is tied to the secure-cookie setting, which is the
        # flag that means "this is an HTTPS deployment".
        self.hsts_enabled = hsts_enabled

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        if self.hsts_enabled:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


def install(app, hsts_enabled):
    app.add_middleware(SecurityHeadersMiddleware, hsts_enabled=hsts_enabled)
