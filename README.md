# Collaborative Checklists

A self-hosted checklist app for a household. Lists are shared, changes appear
live, and the app still opens when the connection is down.

It runs on Python and **one** third-party package: `uvicorn`. Routing, sessions,
Google sign-in, validation, templates and the `.env` reader are all standard
library. The frontend is hand-written CSS and JavaScript with no build step.

## Features

- **Shared lists** with live sync, presence ("Also here: Sam"), and an activity
  feed showing who changed what.
- **Invite links** with a role, an expiry, and a use limit. No email is sent;
  send the link however you like.
- **Roles** — editors can change the list, viewers can only read. Enforced on
  every request, not just hidden in the UI.
- **Removal is sticky** — a removed person is blocked, so a link still
  circulating cannot be used to get back in.
- **Daily reset** — a Reset button for daily lists, or a scheduled reset at a
  chosen hour in a chosen time zone.
- **Categories** — a free-text category per item (Dairy, Tesco, …), with the
  list grouped into sections and autocomplete from categories already in use.
- **Bulk actions** — select several items to set a category, complete, reopen,
  or delete them together, with one undo for a batch delete.
- **Offline** — the shell is cached, changes queue in the browser, and replay
  exactly once when the connection returns.
- **Undo** for deleted items, singly or as a batch.
- **Export and import** as JSON.
- **Google sign-in**, dark mode, installable PWA with the existing app icon.

## Setup

Requires Python 3.11 or newer.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
cp .env.example .env
```

Fill in `.env`:

1. **Google OAuth** — create a Web application client in the Google Cloud
   console. Add `PUBLIC_BASE_URL/auth/callback` as an authorised redirect URI.
   Put the client id and secret in `GOOGLE_CLIENT_ID` and
   `GOOGLE_CLIENT_SECRET`.
2. **`PUBLIC_BASE_URL`** — the URL you actually visit, with no trailing slash.
   This builds the redirect URI, so it must match Google exactly.
3. **`SESSION_SECRET`** — generate one:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   The app refuses to start without a real one.

Run it:

```bash
python main.py
# or: uvicorn main:app --host 127.0.0.1 --port 8000
```

Then visit `http://127.0.0.1:8000`.

### Testing without Google

Set `DEV_AUTH_ENABLED=1` and open:

```
http://127.0.0.1:8000/auth/dev-login?email=you@example.com
```

That signs you in as any address with no password. It also accepts
`&next=/join/<token>` so the invite flow can be walked end to end. The route is
a 404 while the flag is off. **Never enable it on a deployment other people can
reach.**

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL, used for OAuth and invite links |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | Google OAuth credentials |
| `SESSION_SECRET` | — | Required, at least 32 characters |
| `SESSION_COOKIE_SECURE` | `0` | Set to `1` behind HTTPS; also enables HSTS |
| `CSP_ENABLED` | `1` | Escape hatch for the Content-Security-Policy |
| `DEV_AUTH_ENABLED` | `0` | Enables `/auth/dev-login` |
| `DATABASE_PATH` | `checklist.db` beside the code | SQLite file |
| `RATE_LIMIT_ENABLED` | `1` | Master switch |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW` | `600` / `60` | General limit |
| `RATE_LIMIT_AUTH_REQUESTS` / `RATE_LIMIT_AUTH_WINDOW` | `20` / `60` | `/auth/*` limit |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address for `python main.py` |

## Deployment

The target is a small Linux host behind Cloudflare Tunnel. Bind uvicorn to
loopback and let the tunnel terminate TLS.

A hardened unit file lives at `deploy/checklist.service`. It assumes:

- the checkout is `/opt/checklist`,
- the database is `/var/lib/checklist/checklist.db` (`DATABASE_PATH`),
- the service runs as the `checklist` user.

```bash
sudo cp deploy/checklist.service /etc/systemd/system/
sudo install -d -o checklist -g checklist /var/lib/checklist
sudo systemctl daemon-reload
sudo systemctl enable --now checklist
```

Updating is still one command, because migrations run at startup:

```bash
git pull origin main && sudo systemctl restart checklist
```

`--proxy-headers` is set explicitly so the rate limiter sees real client
addresses rather than the tunnel's.

### Backups

```bash
.venv/bin/python main.py backup --out /var/lib/checklist/backup-$(date +%F).db
```

Uses SQLite's online backup API, so it is consistent while the app is running.
`deploy/checklist-backup.service` and `.timer` run it weekly.

Restore with the app stopped:

```bash
.venv/bin/python main.py restore --from /var/lib/checklist/backup-2026-11-20.db
```

## How it works

```
browser ── HTTP ── uvicorn ── main.py (ASGI app)
                                ├─ auth.py             sessions, OAuth, CSRF
                                ├─ lists_api.py        lists, tasks, sharing, sync
                                ├─ ratelimit.py        sliding-window limits
                                ├─ security_headers.py CSP and friends
                                └─ database.py         SQLite + migrations
```

- **Sessions** are a signed cookie (HMAC-SHA256) holding a user id and a CSRF
  token. Name and email are read from the database per request, so a rename or
  a deleted account takes effect immediately.
- **Google sign-in** uses the authorization-code flow with PKCE, then reads the
  userinfo endpoint over TLS. No JWT verification, so no crypto code.
- **Sync** is a version integer per list. `GET /api/lists/{id}/state?since=N`
  returns `304` when nothing changed. Polling runs only for shared lists, only
  while the tab is visible, and backs off when idle.
- **Offline writes** queue in `localStorage`. `POST` carries an
  `Idempotency-Key`; the server stores the response and replays it, so a lost
  reply cannot create a duplicate.
- **Invite links** store only a SHA-256 hash of the token. Expiry, use count
  and role are checked at claim time.

## Security

- Content-Security-Policy with no `unsafe-inline`, no `unsafe-eval`, and no
  nonce. The page has no inline scripts or styles.
- CSRF token **and** `Origin` / `Sec-Fetch-Site` checks on every write.
- Rate limiting per user, falling back to client IP.
- Every access decision is made server-side from the database.
- Invite tokens are hashed at rest and single-use by default.
- The app sets HSTS (when secure), `X-Frame-Options: DENY`, `nosniff`, a
  referrer policy, and `Cache-Control: no-store` on API responses.
- Error pages escape their detail text; OAuth errors carry text from Google.

## Tests

```bash
python tests/test_api.py
# or: python -m unittest discover -s tests -v
```

85 checks, standard library only. They run against a temporary database and
cover authentication, access control, roles, blocks, invite links, scheduled
resets, categories and bulk actions, idempotent retries, validation, and
migrations from a v1 database.

## Upgrading from the FastAPI version

The schema migrates in place. Two things to know:

1. **Pending email invites are not converted.** A token has no way to reach
   someone who was invited by address. Re-share those lists with a link once.
2. **Scheduled resets need time-zone data.** Linux has it. On Windows,
   `pip install tzdata` in the virtualenv, or schedules will fall back to UTC.

## Project layout

```
main.py               ASGI app, routes, static files, CLI
auth.py               sessions, Google OAuth, CSRF
lists_api.py          lists, tasks, sharing, sync, export/import
database.py           schema and PRAGMA user_version migrations
presence.py           in-memory "who is viewing"
ratelimit.py          sliding-window limiter
security_headers.py   CSP and response headers
web.py                Request/Response primitives
config.py             .env reader and settings
util.py               UTC timestamps
static/               shell, CSS, JS, service worker, icons
tests/                stdlib unittest suite and a tiny ASGI client
deploy/               systemd units
PLAN.md               the rebuild plan and design notes
```

## Licence

MIT. See `LICENSE`.
