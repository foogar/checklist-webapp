"""ASGI application: routing, middleware, static files, and the CLI.

No web framework. The request path is:

    read body -> session -> rate limit -> route -> handler
    -> security headers -> send

Uvicorn supplies HTTP parsing, timeouts, and graceful shutdown; everything
above that is here, in a few hundred readable lines.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import mimetypes
import re
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Awaitable, Callable
from urllib.parse import parse_qs

import auth
import database
import ratelimit
import security_headers
from config import settings
from web import (
    HTTPError,
    Request,
    Response,
    html_response,
    json_response,
    redirect,
    text_response,
)

MAX_BODY_BYTES = 64 * 1024
STATIC_DIR = settings.base_dir / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

Handler = Callable[[Request], Awaitable[Response]]

ROUTES: list[tuple[str, re.Pattern[str], Handler]] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    compiled = re.compile(pattern)

    def register(handler: Handler) -> Handler:
        ROUTES.append((method, compiled, handler))
        return handler

    return register


def utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------

# path -> (mtime_ns, size, bytes, etag). Reading from disk on every request
# would be wasteful, but the cache is keyed on mtime so editing a file during
# development still takes effect without a restart.
_static_cache: dict[str, tuple[int, int, bytes, str]] = {}


def _read_static(relative: str) -> tuple[bytes, str, str] | None:
    base = STATIC_DIR.resolve()
    try:
        target = (base / relative).resolve()
    except OSError:
        return None
    if not target.is_relative_to(base) or not target.is_file():
        return None

    stat = target.stat()
    key = str(target)
    cached = _static_cache.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        data, etag = cached[2], cached[3]
    else:
        data = target.read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest()[:16] + '"'
        _static_cache[key] = (stat.st_mtime_ns, stat.st_size, data, etag)

    suffix = target.suffix.lower()
    content_type = CONTENT_TYPES.get(suffix) or mimetypes.guess_type(target.name)[0]
    return data, content_type or "application/octet-stream", etag


def serve_static(
    request: Request, relative: str, cache_control: str = "public, max-age=86400"
) -> Response:
    found = _read_static(relative)
    if found is None:
        raise HTTPError(404, "Not found")
    data, content_type, etag = found

    if request.header("if-none-match") == etag:
        return Response(304, headers={"ETag": etag, "Cache-Control": cache_control})

    return Response(
        status=200,
        body=data,
        headers={
            "Content-Type": content_type,
            "ETag": etag,
            "Cache-Control": cache_control,
        },
    )


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@route("GET", r"/")
async def index(request: Request) -> Response:
    # The shell is static: it contains no user data, which is what lets the
    # service worker cache it and makes an offline start possible at all.
    #
    # Asset URLs carry a content hash so a deploy changes the URL rather than
    # leaving a stale copy in the browser or the worker's cache. That is why
    # the stylesheet and script can be served with a long max-age.
    found = _read_static("index.html")
    if found is None:
        raise HTTPError(404, "Not found")
    html = found[0].decode("utf-8")
    html = html.replace("__ASSET_CSS__", _asset_version("app.css"))
    html = html.replace("__ASSET_JS__", _asset_version("app.js"))
    return Response(
        status=200,
        body=html.encode("utf-8"),
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache",
        },
    )


def _asset_version(relative: str) -> str:
    found = _read_static(relative)
    return found[2].strip('"') if found else "0"


@route("GET", r"/static/(?P<path>.+)")
async def static_file(request: Request) -> Response:
    return serve_static(request, request.params["path"])


@route("GET", r"/offline\.html")
async def offline_page(request: Request) -> Response:
    return serve_static(request, "offline.html", "no-cache")


@route("GET", r"/favicon.ico")
async def favicon(request: Request) -> Response:
    return serve_static(request, "icon-192.png")


@route("GET", r"/manifest.json")
async def manifest(request: Request) -> Response:
    return json_response(
        {
            "id": "/",
            "name": "Collaborative Checklists",
            "short_name": "Checklists",
            "description": "Shared checklists for the household.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait-primary",
            "background_color": "#111827",
            "theme_color": "#2563eb",
            "icons": [
                {
                    "src": "/static/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/static/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
            ],
        },
        headers={"Cache-Control": "no-cache"},
    )


@route("GET", r"/sw.js")
async def service_worker(request: Request) -> Response:
    # Served from the root so its scope covers the whole app. no-cache makes
    # the browser revalidate it, which is how a changed worker replaces an old
    # one without the user clearing site data.
    response = serve_static(request, "sw.js", "no-cache")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@route("GET", r"/robots.txt")
async def robots(request: Request) -> Response:
    # A private household app: nothing here is meant to be indexed.
    return text_response("User-agent: *\nDisallow: /\n")


@route("GET", r"/health")
async def health(request: Request) -> Response:
    """Liveness probe. Returns 200 even if the database is unreachable.

    This is an origin health check for the tunnel, so a transient lock must not
    take a working site out of rotation. The database result is reported in the
    body for a monitor to alert on.
    """
    try:
        with database.connection() as conn:
            conn.execute("SELECT 1").fetchone()
        database_status = "ok"
    except sqlite3.Error as exc:
        database_status = f"error: {exc}"
    return json_response({"status": "ok", "database": database_status})


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------


@route("GET", r"/api/me")
async def api_me(request: Request) -> Response:
    user = auth.current_user(request)
    return json_response(
        {
            "user": user,
            "csrf": request.session.get("csrf", ""),
            "server_time": utcnow(),
        }
    )


@route("GET", r"/auth/login")
async def login(request: Request) -> Response:
    url, state_cookie = auth.build_login(request)
    response = redirect(url, status=302)
    response.set_cookie(
        auth.OAUTH_STATE_COOKIE,
        state_cookie,
        max_age=auth.OAUTH_STATE_MAX_AGE,
        secure=settings.session_cookie_secure,
        same_site="Lax",
    )
    return response


@route("GET", r"/auth/callback")
async def auth_callback(request: Request) -> Response:
    userinfo, next_path = await auth.exchange_code(request)
    email, name, sub = auth.user_from_userinfo(userinfo)

    try:
        with database.connection() as conn:
            user_id = auth.upsert_user(conn, email, name, sub)
    except sqlite3.Error as exc:
        print(f"OAuth callback: database error for {email!r}: {exc}", file=sys.stderr)
        raise HTTPError(
            500, "Could not record your account. Please try again."
        ) from None

    response = redirect(next_path)
    auth.start_session(response, user_id)
    response.delete_cookie(
        auth.OAUTH_STATE_COOKIE, secure=settings.session_cookie_secure
    )
    return response


@route("GET", r"/auth/dev-login")
async def dev_login(request: Request) -> Response:
    """Sign in as any address, for local testing only.

    Returns 404 unless DEV_AUTH_ENABLED is set, so it cannot be reached by
    accident on a real deployment.
    """
    if not settings.dev_auth_enabled:
        raise HTTPError(404, "Not found")

    email = request.query.get("email", ["dev@localhost"])[0].strip().lower()
    if not email or "@" not in email or len(email) > 254:
        raise HTTPError(400, "A valid email is required")

    with database.connection() as conn:
        user_id = auth.upsert_user(conn, email, email.split("@")[0], None)

    response = redirect("/")
    auth.start_session(response, user_id)
    return response


@route("POST", r"/auth/logout")
async def logout(request: Request) -> Response:
    """POST with a CSRF token.

    A GET logout can be triggered by any page on the internet with an image
    tag, which is why this is not one.
    """
    form = parse_qs(request.body.decode("utf-8", "replace"))
    token = (form.get("csrf_token") or [""])[0]
    expected = request.session.get("csrf", "")
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise HTTPError(403, "Invalid or missing CSRF token")
    auth.check_origin(request)

    response = redirect("/")
    auth.clear_session(response)
    return response


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


async def dispatch(request: Request) -> Response:
    is_head = request.method == "HEAD"
    for method, pattern, handler in ROUTES:
        if method != request.method and not (is_head and method == "GET"):
            continue
        match = pattern.fullmatch(request.path)
        if not match:
            continue
        request.params = match.groupdict()
        response = await handler(request)
        if is_head:
            response.headers.setdefault("Content-Length", str(len(response.body)))
            response.body = b""
        return response
    raise HTTPError(404, "Not found")


def error_response(request: Request, error: HTTPError) -> Response:
    if request.path.startswith("/api/"):
        return json_response({"detail": error.detail}, status=error.status, headers=error.headers)
    # The detail can contain text from Google's error response, so it must be
    # escaped before it goes anywhere near HTML.
    detail = html_escape(error.detail)
    if error.status == 404:
        return html_response(
            '<!doctype html><meta charset="utf-8"><title>Not found</title>'
            '<link rel="stylesheet" href="/static/app.css">'
            '<main class="page"><h1>Not found</h1>'
            '<p><a href="/">Back to checklists</a></p></main>',
            status=404,
        )
    return html_response(
        '<!doctype html><meta charset="utf-8"><title>Something went wrong</title>'
        '<link rel="stylesheet" href="/static/app.css">'
        '<main class="page"><h1>Something went wrong</h1>'
        f"<p>{detail}</p>"
        '<p><a href="/">Back to checklists</a></p></main>',
        status=error.status,
    )


async def handle(request: Request) -> Response:
    auth.load_session(request)

    limited = ratelimit.check(request)
    if limited is not None:
        return limited

    return await dispatch(request)


async def read_body(receive: Callable[[], Awaitable[dict]]) -> bytes:
    chunks = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise HTTPError(400, "Client disconnected")
        chunks.extend(message.get("body", b""))
        if len(chunks) > MAX_BODY_BYTES:
            raise HTTPError(413, "Request body too large")
        if not message.get("more_body", False):
            return bytes(chunks)


async def send_response(send: Callable[[dict], Awaitable[None]], response: Response) -> None:
    headers = dict(response.headers)
    if (
        response.body
        and "Content-Length" not in headers
        and response.status not in (204, 304)
    ):
        headers["Content-Length"] = str(len(response.body))

    raw_headers = [
        (name.lower().encode("latin-1"), str(value).encode("latin-1"))
        for name, value in headers.items()
    ]
    raw_headers.extend(
        (b"set-cookie", cookie.encode("latin-1")) for cookie in response.cookies
    )

    await send(
        {"type": "http.response.start", "status": response.status, "headers": raw_headers}
    )
    await send({"type": "http.response.body", "body": response.body})


async def lifespan(receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                database.init_db()
            except Exception as exc:  # noqa: BLE001 - reported to uvicorn
                print(f"Startup failed: {exc}", file=sys.stderr)
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
                return
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def app(scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
    if scope["type"] == "lifespan":
        await lifespan(receive, send)
        return
    if scope["type"] != "http":
        return

    request = Request.from_scope(scope, b"")
    try:
        request.body = await read_body(receive)
    except HTTPError as error:
        response = error_response(request, error)
        security_headers.apply(request, response)
        await send_response(send, response)
        return

    try:
        response = await handle(request)
    except HTTPError as error:
        response = error_response(request, error)
    except Exception:  # noqa: BLE001 - a bug must not take the process down
        traceback.print_exc()
        response = error_response(request, HTTPError(500, "Internal server error"))

    security_headers.apply(request, response)

    limit = request.state.get("ratelimit_limit")
    if limit is not None:
        response.headers.setdefault("X-RateLimit-Limit", str(limit))

    await send_response(send, response)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _backup(out_path: str) -> int:
    source = sqlite3.connect(database.DB_FILE)
    try:
        destination = sqlite3.connect(out_path)
        try:
            # The online backup API produces a consistent copy while the app is
            # running, which a plain file copy of a WAL database does not.
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    print(f"Backed up {database.DB_FILE} to {out_path}")
    return 0


def _restore(in_path: str) -> int:
    source = sqlite3.connect(in_path)
    try:
        destination = sqlite3.connect(database.DB_FILE)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    print(f"Restored {in_path} to {database.DB_FILE}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Collaborative Checklists")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="run the server (default)")
    backup_parser = subparsers.add_parser("backup", help="write a consistent copy")
    backup_parser.add_argument("--out", required=True)
    restore_parser = subparsers.add_parser("restore", help="replace the database")
    restore_parser.add_argument("--from", dest="source", required=True)
    subparsers.add_parser("migrate", help="apply pending migrations and exit")

    args = parser.parse_args()

    if args.command == "backup":
        return _backup(args.out)
    if args.command == "restore":
        return _restore(args.source)
    if args.command == "migrate":
        print(f"schema version {database.init_db()}")
        return 0

    if settings.dev_auth_enabled:
        print(
            "\n"
            "  *** DEV_AUTH_ENABLED is ON ***\n"
            "  /auth/dev-login can sign you in as ANY email address, with no\n"
            "  password and no Google check. Never enable this on a deployment\n"
            "  reachable by anyone else.\n",
            file=sys.stderr,
        )

    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
