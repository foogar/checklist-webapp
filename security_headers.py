"""Standard security response headers.

Applied at the application so they are version controlled, testable, and not
dependent on edge configuration. Set the same headers in a Cloudflare Transform
Rule as well, since these only cover responses the application actually
produced; a 502 or 521 from a proxy in front of a stopped app would not carry
them.
"""
import secrets

from starlette.middleware.base import BaseHTTPMiddleware

# Deliberately no Tailwind or Alpine CDN origins: both are served from here, so
# there is no third-party script origin to allow.
#
# 'unsafe-eval' is required and is the policy's real limitation. Alpine compiles
# expressions such as `editing = !editing` with `new Function`, and there is no
# way to run the standard build without it. It allows eval in scripts that are
# already permitted to run, so it is not itself an entry point, but it does mean
# this policy cannot stop a script-injection bug from executing code. Closing it
# needs Alpine's CSP build (@alpinejs/csp), which restricts expressions to
# named lookups and would mean rewriting every binding in the template.
CSP_TEMPLATE = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'nonce-{nonce}' 'unsafe-eval'",
        # Alpine writes inline styles for x-show and x-transition.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-DNS-Prefetch-Control": "off",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, hsts_enabled, csp_enabled=True):
        super().__init__(app)
        # HSTS tells the browser to refuse plaintext for this host from now on.
        # Emitting it over plain HTTP would make a local or http-only deployment
        # unreachable, so it is tied to the secure-cookie setting, which is the
        # flag that means "this is an HTTPS deployment".
        self.hsts_enabled = hsts_enabled
        self.csp_enabled = csp_enabled

    async def dispatch(self, request, call_next):
        # A fresh nonce per response. Generated here so that every response gets
        # a matching header, and read back by the index route to stamp onto the
        # inline scripts.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)

        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        if self.hsts_enabled:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        if self.csp_enabled:
            response.headers.setdefault(
                "Content-Security-Policy",
                CSP_TEMPLATE.format(nonce=nonce),
            )
        return response


def install(app, hsts_enabled, csp_enabled=True):
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_enabled=hsts_enabled,
        csp_enabled=csp_enabled,
    )
