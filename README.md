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

## Tests
```
.\.venv\Scripts\python.exe tests\test_api.py
```
Covers authentication, per-list access control, request validation, and
database cascade behaviour. Runs against a temporary copy of the database, so
it never touches your real `checklist.db`.

