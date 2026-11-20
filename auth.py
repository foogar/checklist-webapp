"""Google sign-in, session cookies, and CSRF.

Three deliberate choices:

* The OAuth flow uses the userinfo endpoint over TLS instead of verifying an
  ID-token signature. That removes the need for any RSA code, and the response
  is authenticated by the TLS connection to Google either way.
* Sessions are a signed cookie (HMAC-SHA256) rather than a server-side store,
  so a restart does not sign everyone out and there is nothing to garbage
  collect. The cookie carries only a user id and a CSRF token; the name and
  email are read from the database per request, so a rename or a deleted
  account takes effect immediately.
* CSRF is checked twice: a per-session token in a header, and the Origin /
  Sec-Fetch-Site headers the browser sets. Either alone would do; both together
  do not depend on one browser behaviour staying the same.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import sys
import time
from typing import Any
from urllib.error import HTTPError as UrlError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from config import SESSION_MAX_AGE, settings
from web import HTTPError, Request, Response

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

OAUTH_STATE_COOKIE = "checklist_oauth"
OAUTH_STATE_MAX_AGE = 600
MAX_COOKIE_LENGTH = 4096
HTTP_TIMEOUT = 10


# --------------------------------------------------------------------------
# Signed values
# --------------------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _signature(payload: bytes) -> bytes:
    return hmac.new(
        settings.session_secret.encode("utf-8"), payload, hashlib.sha256
    ).digest()


def sign_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{_b64encode(raw)}.{_b64encode(_signature(raw))}"


def unsign_json(token: str) -> dict[str, Any] | None:
    """Return the payload, or None for anything malformed or tampered with."""
    if not token or len(token) > MAX_COOKIE_LENGTH:
        return None
    try:
        payload_part, signature_part = token.split(".", 1)
        raw = _b64decode(payload_part)
        signature = _b64decode(signature_part)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(signature, _signature(raw)):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


def load_session(request: Request) -> None:
    token = request.cookies.get(settings.cookie_name, "")
    payload = unsign_json(token)
    if not payload:
        return
    if int(payload.get("exp", 0)) <= int(time.time()):
        return
    if not isinstance(payload.get("uid"), int):
        return
    request.session = payload


def start_session(response: Response, user_id: int) -> None:
    now = int(time.time())
    payload = {
        "uid": user_id,
        "csrf": secrets.token_urlsafe(32),
        "iat": now,
        "exp": now + SESSION_MAX_AGE,
    }
    response.set_cookie(
        settings.cookie_name,
        sign_json(payload),
        max_age=SESSION_MAX_AGE,
        secure=settings.session_cookie_secure,
        same_site="Lax",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(settings.cookie_name, secure=settings.session_cookie_secure)
    response.delete_cookie(OAUTH_STATE_COOKIE, secure=settings.session_cookie_secure)


def current_user(request: Request) -> dict[str, Any] | None:
    """Look up the signed-in user once per request.

    Reading from the database rather than trusting a name stored in the cookie
    means a deleted account stops working immediately and a rename shows up on
    the next request.
    """
    if request.state.get("_user_loaded"):
        return request.user

    request.state["_user_loaded"] = True
    uid = request.session.get("uid")
    if not isinstance(uid, int):
        return None

    from database import connect  # local import avoids a cycle at import time

    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, email, name FROM users WHERE id = ?", (uid,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        request.session = {}
        return None

    request.user = {"id": row["id"], "email": row["email"], "name": row["name"]}
    return request.user


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if user is None:
        raise HTTPError(401, "Not authenticated")
    return user


def require_csrf(request: Request) -> None:
    token = request.header("x-csrf-token")
    expected = request.session.get("csrf", "")
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise HTTPError(403, "Invalid or missing CSRF token")
    check_origin(request)


def check_origin(request: Request) -> None:
    origin = request.header("origin")
    if origin:
        host = request.header("host")
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise HTTPError(403, "Invalid Origin header") from None
        if not host or parsed.netloc.lower() != host.lower():
            raise HTTPError(403, "Cross-origin request refused")

    # Chromium sends this on every request; "same-site" is not enough because a
    # sibling subdomain is same-site but not same-origin.
    fetch_site = request.header("sec-fetch-site")
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        raise HTTPError(403, "Cross-origin request refused")


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


def upsert_user(
    conn: sqlite3.Connection, email: str, name: str, google_sub: str | None = None
) -> int:
    """Find or create a user, returning their id.

    Identity is Google's `sub`, which is stable for the life of the account.
    Email is only a bridge for accounts predating the google_sub column, and is
    refreshed on every login so a renamed Workspace user keeps their lists.
    """
    email = email.strip().lower()
    user_id: int | None = None

    conn.execute("BEGIN IMMEDIATE")
    try:
        if google_sub:
            row = conn.execute(
                "SELECT id, email FROM users WHERE google_sub = ?", (google_sub,)
            ).fetchone()
            if row:
                user_id = row[0]
                if name:
                    conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
                if row[1] != email:
                    taken = conn.execute(
                        "SELECT 1 FROM users WHERE LOWER(email) = ? AND id != ?",
                        (email, user_id),
                    ).fetchone()
                    if taken is None:
                        conn.execute(
                            "UPDATE users SET email = ? WHERE id = ?", (email, user_id)
                        )
                    else:
                        print(
                            f"upsert_user: {email!r} already belongs to another "
                            f"account; leaving user {user_id} on {row[1]!r}",
                            file=sys.stderr,
                        )

        if user_id is None:
            row = conn.execute(
                "SELECT id, google_sub FROM users WHERE LOWER(email) = ?", (email,)
            ).fetchone()
            if row:
                user_id = row[0]
                if google_sub and not row[1]:
                    conn.execute(
                        "UPDATE users SET google_sub = ? WHERE id = ?",
                        (google_sub, user_id),
                    )
            else:
                cursor = conn.execute(
                    "INSERT INTO users (email, name, google_sub) VALUES (?, ?, ?)",
                    (email, name, google_sub),
                )
                user_id = cursor.lastrowid

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return int(user_id)


# --------------------------------------------------------------------------
# Google OAuth
# --------------------------------------------------------------------------


def _http_json(request: UrlRequest) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except UrlError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise HTTPError(400, f"Google sign-in failed ({exc.code}): {body[:200]}") from None
    except (OSError, ValueError) as exc:
        raise HTTPError(502, f"Could not reach Google: {exc}") from None
    if not isinstance(payload, dict):
        raise HTTPError(502, "Google returned an unexpected response")
    return payload


def _redirect_uri() -> str:
    return f"{settings.public_base_url}/auth/callback"


def safe_next(value: str | None) -> str:
    """Only allow same-origin relative paths, so ?next= cannot be an open redirect."""
    if not value or not value.startswith("/"):
        return "/"
    # "//host" is protocol-relative, and "/\host" is treated the same way by
    # some browsers. Neither is a path this app ever produces.
    if value[1:2] in {"", "/", "\\"}:
        return "/"
    return value


def build_login(request: Request) -> tuple[str, str]:
    """Return (authorize_url, state_cookie_value)."""
    if not settings.google_configured:
        raise HTTPError(503, "Google sign-in is not configured")

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64encode(hashlib.sha256(verifier.encode("ascii")).digest())

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    url = f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"

    cookie_value = sign_json(
        {
            "state": state,
            "verifier": verifier,
            "next": safe_next(request.query.get("next", [""])[0]),
            "exp": int(time.time()) + OAUTH_STATE_MAX_AGE,
        }
    )
    return url, cookie_value


async def exchange_code(request: Request) -> tuple[dict[str, Any], str]:
    """Complete the authorization-code flow.

    Returns (userinfo, next_path). The network calls run in a worker thread so
    a slow Google response cannot stall the event loop for other users.
    """
    if not settings.google_configured:
        raise HTTPError(503, "Google sign-in is not configured")

    error = request.query.get("error", [""])[0]
    if error:
        description = request.query.get("error_description", [""])[0] or error
        raise HTTPError(400, f"Google sign-in failed: {description}")

    code = request.query.get("code", [""])[0]
    state = request.query.get("state", [""])[0]
    if not code or not state:
        raise HTTPError(400, "Google sign-in failed: missing code or state")

    state_payload = unsign_json(request.cookies.get(OAUTH_STATE_COOKIE, ""))
    if not state_payload or int(state_payload.get("exp", 0)) <= int(time.time()):
        raise HTTPError(400, "Google sign-in failed: the sign-in attempt expired")
    if not hmac.compare_digest(str(state_payload.get("state", "")), state):
        raise HTTPError(400, "Google sign-in failed: state mismatch")

    token_request = UrlRequest(
        TOKEN_ENDPOINT,
        data=urlencode(
            {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "code_verifier": state_payload.get("verifier", ""),
                "grant_type": "authorization_code",
                "redirect_uri": _redirect_uri(),
            }
        ).encode("ascii"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    token = await asyncio.to_thread(_http_json, token_request)

    access_token = token.get("access_token")
    if not access_token:
        raise HTTPError(400, "Google sign-in failed: no access token returned")

    userinfo_request = UrlRequest(
        USERINFO_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    userinfo = await asyncio.to_thread(_http_json, userinfo_request)

    return userinfo, safe_next(str(state_payload.get("next", "/")))


def user_from_userinfo(userinfo: dict[str, Any]) -> tuple[str, str, str]:
    """Validate the userinfo claims and return (email, name, sub)."""
    email = str(userinfo.get("email") or "").strip().lower()
    if not email:
        raise HTTPError(400, "Google did not return an email address")

    verified = userinfo.get("email_verified")
    if verified is not True and str(verified).lower() != "true":
        raise HTTPError(400, "Google has not verified that email address")

    sub = str(userinfo.get("sub") or "").strip()
    if not sub:
        raise HTTPError(400, "Google did not return an account identifier")

    name = str(userinfo.get("name") or email).strip()
    return email, name, sub
