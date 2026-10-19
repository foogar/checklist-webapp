import os
import secrets
import hashlib
from urllib.parse import parse_qs

from dotenv import load_dotenv

# Load environment variables from a .env file if present
load_dotenv()

# ... (rest of the imports and code)
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from authlib.integrations.starlette_client import OAuth
from authlib.integrations.base_client.errors import OAuthError
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
import sqlite3
import sys
from typing import Literal

from database import BASE_DIR, connect, init_db
import rate_limit
import security_headers

def static_version(relative_path):
    """Short content hash, used to bust the service worker's cached copy.

    The worker caches /static/* by URL with cache-first, so a rebuilt file at
    an unchanged path would be served stale until VERSION was bumped by hand.
    Varying the URL by content makes a change a new cache entry instead, so
    rebuilding the CSS needs no matching worker edit. Computed once at startup:
    the file only changes across a restart, which is the deploy cycle anyway.
    """
    try:
        return hashlib.sha256((BASE_DIR / relative_path).read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


CSS_VERSION = static_version("static/app.css")

# Initialize database on startup
init_db()

app = FastAPI(title="AI Checklist Webapp")

# Mount the static directory so FastAPI can serve images, CSS, etc.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Secure session middleware
# Fail closed: an unset, empty, or known-weak SESSION_SECRET must stop the app
# rather than fall back to a hardcoded key, which is public in the repository.
INSECURE_SECRETS = {
    "change_me_to_something_super_secure",
    "super-secret-homelab-key",
    "change_me",
    "changeme",
    "secret",
}

SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET must be set in .env")
if SESSION_SECRET.strip().lower() in INSECURE_SECRETS:
    raise RuntimeError(
        "SESSION_SECRET is a known placeholder. Generate a real one with: "
        'python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
if len(SESSION_SECRET) < 32:
    raise RuntimeError(
        f"SESSION_SECRET must be at least 32 characters (got {len(SESSION_SECRET)})."
    )

# Set SESSION_COOKIE_SECURE=1 once the app is served over HTTPS. Without it
# the browser will still send the session cookie over plain HTTP, which on a
# LAN-reachable deployment (e.g. uvicorn --host 0.0.0.0) exposes it in cleartext.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

# Rate limiting is registered first so that Starlette runs it innermost, after
# SessionMiddleware has already populated request.session. Registering it after
# the session middleware would place it outermost, where the session is not yet
# available and every caller would collapse into a single IP-keyed bucket.
rate_limit.install(app)

# Outermost, so these headers are attached even to responses produced by a
# later middleware rather than only to successful handler returns.
CSP_ENABLED = os.getenv("CSP_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
security_headers.install(app, hsts_enabled=SESSION_COOKIE_SECURE, csp_enabled=CSP_ENABLED)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=SESSION_COOKIE_SECURE,
)

# OAuth Setup
oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID', ''),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET', ''),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# --- Pydantic Models ---
# Length caps stop a single request from writing an arbitrarily large blob into
# the database; without them any task title is accepted at any size.
class ListCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    is_resettable: bool = False

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)

class ShareRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)

class RevokeShareRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)

class TaskStatusUpdate(BaseModel):
    # Validated by pydantic, so a malformed body yields 422 rather than a 500.
    status: Literal['pending', 'completed']

class ReorderRequest(BaseModel):
    task_ids: list[int]

# --- Helper: Get Current User from Session ---
def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

def claim_invites(conn, cursor, user_id, email):
    """Turn any pending invites for this email into real shares.

    Called on every successful login. Moving them into list_shares here means
    the rest of the app only ever has to check list_shares, so access control
    has a single table to reason about.
    """
    cursor.execute("""
        INSERT OR IGNORE INTO list_shares (list_id, user_id)
        SELECT list_id, ? FROM list_invites WHERE LOWER(email) = ?
    """, (user_id, email))
    claimed = cursor.rowcount
    cursor.execute("DELETE FROM list_invites WHERE LOWER(email) = ?", (email,))
    conn.commit()
    return claimed


def upsert_user(email, name, google_sub=None):
    """Find or create a user.

    Identity is Google's `sub`, stable for the life of the account. The email is
    only a bridge for accounts predating the google_sub column, and is
    refreshed on every login so a renamed Workspace user keeps their lists
    instead of appearing as a new person.
    """
    email = email.strip().lower()
    conn = connect()
    cursor = conn.cursor()
    user_id = None

    if google_sub:
        cursor.execute("SELECT id, email FROM users WHERE google_sub = ?", (google_sub,))
        row = cursor.fetchone()
        if row:
            user_id = row[0]
            if name:
                cursor.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
            if row[1] != email:
                # Another row may already hold this address, which happens when
                # a different person claimed it before the rename. Keeping the
                # old address beats failing the login and losing access.
                cursor.execute(
                    "SELECT 1 FROM users WHERE LOWER(email) = ? AND id != ?",
                    (email, user_id),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        "UPDATE users SET email = ? WHERE id = ?", (email, user_id)
                    )
                else:
                    print(
                        f"upsert_user: {email!r} already belongs to another account; "
                        f"leaving user {user_id} on {row[1]!r}",
                        file=sys.stderr,
                    )

    if user_id is None:
        cursor.execute(
            "SELECT id, google_sub FROM users WHERE LOWER(email) = ?", (email,)
        )
        row = cursor.fetchone()
        if row:
            user_id = row[0]
            if google_sub and not row[1]:
                # Backfill: first login since the column was added.
                cursor.execute(
                    "UPDATE users SET google_sub = ? WHERE id = ?", (google_sub, user_id)
                )
        else:
            cursor.execute(
                "INSERT INTO users (email, name, google_sub) VALUES (?, ?, ?)",
                (email, name, google_sub),
            )
            user_id = cursor.lastrowid

    conn.commit()

    # First sign-in for this address: pick up anything shared with them while
    # they did not yet have an account.
    claim_invites(conn, cursor, user_id, email)
    conn.close()
    return user_id


def start_session(request, user_id, email, name):
    request.session['user'] = {
        'id': user_id,
        'email': email,
        'name': name
    }
    # Issued with the session and checked on the one state-changing form, so a
    # cross-site POST cannot drive it. The session cookie is already
    # SameSite=Lax, which covers this today, but leaning on a cookie attribute
    # alone makes the logout fix load-bearing on that single setting.
    request.session['csrf_token'] = secrets.token_urlsafe(32)


# Development login bypass. Off unless explicitly enabled, and the route is a
# 404 while off so it cannot be discovered. It exists so the app can be driven
# and tested without a Google round trip; it must never be enabled on a
# deployment other people can reach.
DEV_AUTH_ENABLED = os.getenv("DEV_AUTH_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

if DEV_AUTH_ENABLED:
    print(
        "\n"
        "  *** DEV_AUTH_ENABLED is ON ***\n"
        "  /auth/dev-login can sign you in as ANY email address, with no\n"
        "  password and no Google check. Never enable this on a deployment\n"
        "  reachable by anyone else.\n",
        file=sys.stderr,
    )

# --- Auth Routes ---
@app.route('/auth/login')
async def login(request: Request):
    redirect_uri = request.url_for('auth_callback')
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.route('/auth/callback')
async def auth_callback(request: Request):
    # Without this, every OAuth failure surfaced as a bare "Internal Server
    # Error" with the real reason buried in the server log: an unhandled
    # exception is a 500 whether the cause was a declined consent screen, an
    # expired code, a redirect_uri mismatch, or a CSRF state failure.
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        print(
            f"OAuth callback failed: error={exc.error!r} description={exc.description!r}",
            file=sys.stderr,
        )
        reason = exc.description or exc.error or "unknown error"
        raise HTTPException(status_code=400, detail=f"Google sign-in failed: {reason}")

    user_info = token.get('userinfo')
    if not user_info:
        print("OAuth callback: token contained no userinfo", file=sys.stderr)
        raise HTTPException(status_code=400, detail="Google did not return account information")

    # Read with .get rather than [] so a missing field gives a real message
    # instead of a KeyError traceback.
    email = user_info.get('email')
    if not email:
        print(f"OAuth callback: userinfo has no email, keys={sorted(user_info)}", file=sys.stderr)
        raise HTTPException(status_code=400, detail="Google did not return an email address")

    # Google's `sub` is the stable identity; email can change under an
    # administrator's control and must not be what identifies the account.
    google_sub = user_info.get('sub')
    if not google_sub:
        print("OAuth callback: userinfo has no sub claim", file=sys.stderr)
        raise HTTPException(status_code=400, detail="Google did not return an account identifier")

    name = user_info.get('name') or email

    try:
        user_id = upsert_user(email, name, google_sub)
    except sqlite3.Error as exc:
        print(f"OAuth callback: database error for {email!r}: {exc}", file=sys.stderr)
        raise HTTPException(status_code=500, detail="Could not record your account. Please try again.")

    start_session(request, user_id, email, name)
    return RedirectResponse(url='/', status_code=303)

@app.get('/auth/dev-login')
async def dev_login(request: Request, email: str = "dev@localhost"):
    """Log in as an arbitrary email for local testing.

    Populates the same rows as the OAuth callback, so lists, shares, and tasks
    behave identically. Returns 404 unless DEV_AUTH_ENABLED is set.
    """
    if not DEV_AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    email = email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")

    # No sub: this bypass never touches Google. The account is matched by
    # email and picks up a real sub the first time they sign in properly.
    user_id = upsert_user(email, email.split("@")[0], None)
    start_session(request, user_id, email, email.split("@")[0])
    return RedirectResponse(url='/', status_code=303)


@app.post('/auth/logout')
async def logout(request: Request):
    """POST with a CSRF token.

    This used to be a GET, which meant any page on the internet could log the
    user out with an image tag pointing here.

    The body is parsed directly rather than through request.form(), which
    would require adding python-multipart for a single hidden field. Browsers
    submit this form as application/x-www-form-urlencoded, and anything else
    simply fails to produce a matching token, so the failure mode is a 403.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    csrf_token = (parse_qs(raw).get("csrf_token") or [""])[0]
    expected = request.session.get("csrf_token")
    if not expected or not secrets.compare_digest(csrf_token, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")
    request.session.clear()
    return RedirectResponse(url='/', status_code=303)


# --- Web App Manifest ---
@app.get('/manifest.json')
async def manifest():
    return {
        "name": "Collaborative Checklists",
        "short_name": "Checklists",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#111827",
        "theme_color": "#2563eb",
        "icons": [
            {
                "src": "/static/icon-192.png",
                "sizes": "192x192",
                "type": "image/png"
            },
            {
                "src": "/static/icon-512.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    }

@app.get('/health')
async def health():
    """Liveness probe for the tunnel or an external monitor.

    Deliberately returns 200 even if the database is unreachable. This is used
    as an origin health check, so letting a transient lock or a brief write
    failure flip it to an error would take a working site offline. The database
    result is included in the body so a monitor can alert on it without the
    probe governing availability.
    """
    try:
        conn = connect()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        database = "ok"
    except sqlite3.Error as exc:
        database = f"error: {exc}"
    return {"status": "ok", "database": database}

# --- Service Worker ---
@app.get('/sw.js')
async def service_worker():
    """Serve the worker from the root so its scope covers the whole app.

    A worker served from /static/sw.js would default to a scope of /static/ and
    would never see page navigations or API calls. no-cache makes the browser
    revalidate this file, which is how a changed worker replaces the old one.
    """
    path = BASE_DIR / "static" / "sw.js"
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )

# --- Frontend Route ---
@app.get('/')
async def index(request: Request):
    user = request.session.get('user')
    # The nonce is minted per request by SecurityHeadersMiddleware and matched
    # against the CSP header on this response. Inline scripts without it are
    # blocked, which is the whole point of the nonce.
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "csrf_token": request.session.get("csrf_token", ""),
        "css_version": CSS_VERSION,
    })

# --- API: Lists ---
@app.get('/api/lists')
async def get_lists(request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()
    
# Fetch owned lists + shared lists (now including is_resettable!)
    cursor.execute("""
        SELECT DISTINCT l.id, l.title, l.user_id, l.is_resettable, u.name as owner_name 
        FROM lists l
        JOIN users u ON l.user_id = u.id
        LEFT JOIN list_shares ls ON l.id = ls.list_id
        WHERE l.user_id = ? OR ls.user_id = ?
        ORDER BY l.created_at DESC
    """, (user['id'], user['id']))
    
    lists = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return lists

@app.post('/api/lists')
async def create_list(payload: ListCreate, request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO lists (user_id, title, is_resettable) VALUES (?, ?, ?)", (user['id'], payload.title, 1 if payload.is_resettable else 0))
    conn.commit()
    list_id = cursor.lastrowid
    conn.close()
    return {"id": list_id, "title": payload.title, "is_resettable": payload.is_resettable, "user_id": user['id']}

@app.delete('/api/lists/{list_id}')
async def delete_list(list_id: int, request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()
    # Ensure user owns the list
    cursor.execute("DELETE FROM lists WHERE id = ? AND user_id = ?", (list_id, user['id']))
    conn.commit()
    # The ownership filter makes a non-matching delete a no-op. Without this
    # check the endpoint reports success even when nothing was removed, so a
    # caller is told a list is gone while it still exists. 404 is used for both
    # "no such list" and "not yours" so ownership cannot be probed.
    deleted = cursor.rowcount
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="List not found")
    return {"success": True}

# --- API: Tasks ---
@app.get('/api/lists/{list_id}/tasks')
async def get_tasks(list_id: int, request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()
    
    # Verify access (owner or shared)
    cursor.execute("""
        SELECT 1 FROM lists l 
        LEFT JOIN list_shares ls ON l.id = ls.list_id 
        WHERE l.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (list_id, user['id'], user['id']))
    
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cursor.execute("SELECT id, title, status, position FROM tasks WHERE list_id = ? ORDER BY position ASC, id DESC", (list_id,))
    tasks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return tasks

@app.post('/api/lists/{list_id}/tasks')
async def create_task(list_id: int, payload: TaskCreate, request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()
    
    # Verify access
    cursor.execute("""
        SELECT 1 FROM lists l 
        LEFT JOIN list_shares ls ON l.id = ls.list_id 
        WHERE l.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (list_id, user['id'], user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cursor.execute("INSERT INTO tasks (list_id, title) VALUES (?, ?)", (list_id, payload.title))
    conn.commit()
    task_id = cursor.lastrowid
    conn.close()
    return {"id": task_id, "title": payload.title, "status": "pending"}

@app.patch('/api/tasks/{task_id}')
async def update_task_status(task_id: int, payload: TaskStatusUpdate, request: Request):
    user = get_current_user(request)
    new_status = payload.status
        
    conn = connect()
    cursor = conn.cursor()
    
    # Verify user has access to the list containing this task via ownership or share
    cursor.execute("""
        SELECT 1 FROM tasks t
        JOIN lists l ON t.list_id = l.id
        LEFT JOIN list_shares ls ON l.id = ls.list_id
        WHERE t.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (task_id, user['id'], user['id']))
    
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cursor.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
    conn.commit()
    conn.close()
    return {"success": True}

@app.delete('/api/tasks/{task_id}')
async def delete_task(task_id: int, request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 1 FROM tasks t
        JOIN lists l ON t.list_id = l.id
        LEFT JOIN list_shares ls ON l.id = ls.list_id
        WHERE t.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (task_id, user['id'], user['id']))
    
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post('/api/lists/{list_id}/reset')
async def reset_list(list_id: int, request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()
    
    # Verify owner access for resetting
    cursor.execute("SELECT 1 FROM lists WHERE id = ? AND user_id = ?", (list_id, user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Only list owner can reset daily tasks")

    cursor.execute("UPDATE tasks SET status = 'pending' WHERE list_id = ?", (list_id,))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post('/api/lists/{list_id}/share')
async def share_list(list_id: int, payload: ShareRequest, request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()

    # Verify owner access
    cursor.execute("SELECT 1 FROM lists WHERE id = ? AND user_id = ?", (list_id, user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Only list owner can share this list")

    email = payload.email.strip().lower()

    # A registered address is shared immediately. An unregistered one becomes a
    # pending invite and is claimed when that address next signs in. Both return
    # the same status and shape, so this endpoint cannot be used to discover
    # which email addresses have accounts on this server.
    cursor.execute("SELECT id FROM users WHERE LOWER(email) = ?", (email,))
    target = cursor.fetchone()

    if target:
        target_user_id = target[0]
        if target_user_id == user['id']:
            conn.close()
            raise HTTPException(status_code=400, detail="You already own this list")
        cursor.execute("INSERT OR IGNORE INTO list_shares (list_id, user_id) VALUES (?, ?)", (list_id, target_user_id))
        conn.commit()
        already = cursor.rowcount == 0
        conn.close()
        return {"success": True, "status": "shared", "already": already}

    cursor.execute("INSERT OR IGNORE INTO list_invites (list_id, email) VALUES (?, ?)", (list_id, email))
    conn.commit()
    already = cursor.rowcount == 0
    conn.close()
    return {"success": True, "status": "invited", "already": already}

@app.get('/api/lists/{list_id}/shares')
async def get_list_shares(list_id: int, request: Request):
    """Owner-only. Shows who this list is shared with, plus any pending
    invites for addresses that have not signed in yet."""
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()

    cursor.execute("SELECT 1 FROM lists WHERE id = ? AND user_id = ?", (list_id, user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="List not found")

    cursor.execute("""
        SELECT u.id AS user_id, u.email, u.name, 'confirmed' AS status
        FROM list_shares ls
        JOIN users u ON u.id = ls.user_id
        WHERE ls.list_id = ?
    """, (list_id,))
    shares = [dict(row) for row in cursor.fetchall()]

    cursor.execute("""
        SELECT NULL AS user_id, i.email, i.email AS name, 'pending' AS status
        FROM list_invites i
        WHERE i.list_id = ?
    """, (list_id,))
    shares.extend(dict(row) for row in cursor.fetchall())

    shares.sort(key=lambda s: (s["status"] != "confirmed", (s["name"] or "").lower()))
    conn.close()
    return shares


@app.delete('/api/lists/{list_id}/shares')
async def revoke_list_share(list_id: int, payload: RevokeShareRequest, request: Request):
    """Owner-only. Takes back access, whether it was granted directly or is
    still a pending invite. Keyed by email so one endpoint covers both."""
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()

    # Same 404 for "no such list" and "not the owner" so ownership cannot be probed.
    cursor.execute("SELECT 1 FROM lists WHERE id = ? AND user_id = ?", (list_id, user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="List not found")

    email = payload.email.strip().lower()

    # Clearing the invite too covers an address that was invited and has since
    # signed up, which would otherwise become a share again on their next login.
    cursor.execute("DELETE FROM list_invites WHERE list_id = ? AND email = ?", (list_id, email))
    revoked_invites = cursor.rowcount

    cursor.execute("""
        DELETE FROM list_shares
        WHERE list_id = ?
          AND user_id = (SELECT id FROM users WHERE LOWER(email) = ?)
    """, (list_id, email))
    revoked_shares = cursor.rowcount
    conn.commit()
    conn.close()

    if not (revoked_invites or revoked_shares):
        raise HTTPException(status_code=404, detail="That address does not have this list")
    return {"success": True}


@app.post('/api/lists/{list_id}/reorder')
async def reorder_tasks(list_id: int, payload: ReorderRequest, request: Request):
    user = get_current_user(request)
    conn = connect()
    cursor = conn.cursor()
    
    # Verify access (owner or shared)
    cursor.execute("""
        SELECT 1 FROM lists l 
        LEFT JOIN list_shares ls ON l.id = ls.list_id 
        WHERE l.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (list_id, user['id'], user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Update task positions based on array index
    for index, task_id in enumerate(payload.task_ids):
        cursor.execute("UPDATE tasks SET position = ? WHERE id = ? AND list_id = ?", (index, task_id, list_id))
    conn.commit()
    conn.close()
    return {"success": True}
