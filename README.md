# Collaborative Checklist PWA

A self-hosted, mobile-friendly collaborative checklist web application built with FastAPI, SQLite, Tailwind CSS, and Alpine.js, featuring PWA support.

## Features
- PWA Compliant for full-screen mobile usage
- Secure Google OAuth authentication
- Account sharing by email
- Daily reset mode and task reordering

## Security headers
The application sets these on every response, so they are version controlled
and testable rather than living in dashboard config:

| Header | Value |
|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `X-DNS-Prefetch-Control` | `off` |

HSTS is tied to `SESSION_COOKIE_SECURE`: it is only sent when that is on, since
emitting it over plain HTTP would make an http-only or local deployment
unreachable. The other four are harmless on any transport.

Set the same headers in a Cloudflare Transform Rule as well. These only cover
responses the application produced, so a 502 or 521 from a proxy in front of a
stopped app would not carry them.

There is deliberately no Content-Security-Policy. The app loads Tailwind from a
CDN that compiles CSS in the browser using `eval`, plus Alpine from a CDN, with
inline scripts in the template. A policy tight enough to be worth having would
need `unsafe-inline` and `unsafe-eval`, which largely defeats the purpose.
Doing it properly means building the CSS at deploy time instead of running the
Tailwind CDN.

## Offline behaviour
There is a service worker at `/sw.js`, so the app is installable from the
browser and launches in a standalone window with a home-screen icon.

It caches **only** public static assets — the icons, the web app manifest, and
the Tailwind and Alpine scripts from the CDN. It deliberately never caches the
HTML document or any `/api/` or `/auth/` response:

- The page is rendered server-side with the signed-in user inlined, so a cached
  copy would leave one person's name and email in Cache Storage, readable by
  whoever next uses the browser on that device.
- API responses are per-user; caching them would serve one person's checklists
  to another, and would keep serving them after logout.

Tasks live on the server, so they need a connection to load. Offline the app
opens with an amber banner saying so. A dead backend behind a proxy counts as
offline too, so you get that message rather than a raw 502 during a restart.

When you change `static/sw.js`, bump its `VERSION` constant. The worker then
updates itself and purges the previous cache on its own.

## Setup
1. Clone the repository
2. Create a virtual environment and install dependencies:
   ```
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env`, fill in your Google OAuth credentials, and
   generate a session secret:
   ```
   .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   The app refuses to start if `SESSION_SECRET` is missing, a known placeholder,
   or shorter than 32 characters.
4. Run with `.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000`
5. Once served over HTTPS, set `SESSION_COOKIE_SECURE=1` in `.env` so the
   browser will not send the session cookie over plain HTTP.

## Sharing
Only the list owner can share it, see who it is shared with, or take access
back. Type an email address and press Share.

If that address already has an account it gains access immediately. If it does
not, an invite is recorded and access is granted the first time that address
signs in — so you can share with someone before they have ever used the app,
without the response revealing whether they have an account here.

Invited addresses appear in greyed-out italics until they sign up, and the same
Remove button revokes either a confirmed share or a pending invite.

## Rate limiting
Requests are counted per signed-in user, falling back to the client IP for
anonymous callers. Requests over the limit get a `429` with a `Retry-After`
header. Counters are in memory and slide rather than lock out, so a caller
recovers on their own once the window drains.

| Setting | Default | Covers |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `1` | Master switch; `0` disables |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW` | `600` / `60` | All other paths |
| `RATE_LIMIT_AUTH_REQUESTS` / `RATE_LIMIT_AUTH_WINDOW` | `20` / `60` | `/auth/*` |

Per-user rather than per-IP is deliberate: an IP-keyed limit cannot tell one
account hammering the API from a whole household sharing one router.

Counters live in process memory, so each uvicorn worker keeps its own. Run the
default single worker, or the effective limit multiplies by the worker count.

## Data location
By default the SQLite database is `checklist.db` beside the application code,
found relative to the application itself rather than the working directory, so
it stays correct however the app is launched.

Set `DATABASE_PATH` in `.env` to put it elsewhere. On a real deployment this is
what you want, so the database lives on a persistent volume or in a directory
you back up:

```
DATABASE_PATH=/var/lib/checklist/checklist.db
```

Backing up is a straight file copy of that single file. Stop the app first, or
copy `checklist.db`, `checklist.db-wal`, and `checklist.db-shm` together, since
SQLite runs in WAL mode. Never put the database in a cloud-synced folder or on
a network share: WAL relies on sidecar files and memory-mapped access that do
not survive either.

## Tests
```
.\.venv\Scripts\python.exe tests\test_api.py
```
Covers authentication, per-list access control, request validation, and
database cascade behaviour. Runs against a temporary copy of the database, so
it never touches your real `checklist.db`.

## Testing without Google OAuth
Set `DEV_AUTH_ENABLED=1` in `.env` to enable `/auth/dev-login`, which signs you
in as any email address:

```
http://localhost:8000/auth/dev-login?email=you@example.com
```

It creates the same `users` row the OAuth callback would, so lists, shares, and
tasks behave exactly as they do in normal use. This is useful for testing on a
phone or offline, where a Google round trip is inconvenient.

While disabled the route returns 404, so it cannot be reached by accident, and
the app prints a warning at startup whenever it is on. **Never enable it on a
deployment anyone else can reach** — it grants access to any account with no
password.

