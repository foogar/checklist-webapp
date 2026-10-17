# Collaborative Checklist PWA

A self-hosted, mobile-friendly collaborative checklist web application built with FastAPI, SQLite, Tailwind CSS, and Alpine.js, featuring PWA support.

## Features
- PWA Compliant for full-screen mobile usage
- Secure Google OAuth authentication
- Account sharing by email
- Daily reset mode and task reordering

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

