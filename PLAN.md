# Checklist v2 — Rebuild Plan

Rebuild of `foogar/checklist-webapp` on a `v2` branch of the same repository.
Goal: remove third-party dependencies without giving up a production-grade
server, and close the gap between the app's name and its behaviour.

Status: **planning**. Nothing in the existing repository has been changed.

---

## Decisions locked in

| Decision | Choice |
|---|---|
| Python runtime deps | `uvicorn` only (pulls `click`, `h11`). No FastAPI, Starlette, pydantic, authlib, httpx, itsdangerous, Jinja2, python-dotenv. |
| Frontend | Hand-written CSS and vanilla JS. No Tailwind build step, no Alpine, no CDN. |
| Auth | Google OIDC, implemented with `urllib.request` + PKCE. |
| Location | Same repo, new `v2` branch. `main` and the live site stay untouched until cutover. |
| Scope | Parity + accessibility/polish + invite links, roles, scheduled reset, offline writes. |
| Database | Reads the existing `checklist.db` unchanged. Additive migrations only. |
| Favicon | The existing app icon (`static/icon-192.png`). |
| Sharing | Invite **links** replace email invites. One way to grant access. |

### Dependency budget

Before: 7 Python packages, a Tailwind CLI binary, a Node-free but manual CSS
build step, and a 56 KB vendored Alpine bundle.

After: 1 declared Python package, 0 build steps, 0 vendored JS.

```
requirements.txt
  uvicorn==<pin>
```

`uvicorn` is kept deliberately. The alternatives were evaluated and rejected:
`http.server` (documented as unsuitable for production, no slowloris
protection), `wsgiref` (single-threaded), and a hand-rolled socket server
(reimplements h11 badly). Uvicorn gives us a battle-tested HTTP/1.1 parser,
timeouts, graceful shutdown, and optional multi-worker support without bringing
in a web framework.

---

## Architecture

A plain ASGI callable. No framework, no middleware stack — a small pipeline of
explicit functions.

```
request
  └─ rate_limit      (identity = session user, else client IP)
      └─ security_headers
          └─ session     (signed cookie → request state)
              └─ router  (method + path regex → handler)
                  └─ handler → Response
```

Handlers return a small `Response` dataclass (`status`, `headers`, `body`), or a
streaming response for static files. JSON encoding is `json.dumps` with a
`default=` for dates. Body reads are capped at 64 KB.

### File layout

Mirrors the current flat structure, so the diff stays reviewable.

```
main.py              ASGI app, router, middleware, CLI entry
config.py            .env loader, typed settings, fail-closed validation
database.py          connection, schema, PRAGMA user_version migrations
auth.py              Google OIDC, session cookies, CSRF, dev login
lists_api.py         list/task/member/invite/sync handlers
ratelimit.py         sliding window (port of current implementation)
security_headers.py  CSP + headers (no nonce, no unsafe-*)
static/
  index.html         static shell, no user data
  app.css            ~700 lines, custom properties, light/dark
  app.js             ~900 lines vanilla
  sw.js              shell + offline page, versioned cache
  icon-192.png       favicon, apple-touch-icon
  icon-512.png
tests/
  test_api.py        unittest, real server on an ephemeral port
  test_auth.py
  test_db.py
```

Rough total: ~1,400 lines Python, ~1,600 lines JS/CSS. Smaller in bytes than
today once Alpine and the generated Tailwind CSS are gone.

---

## Data model

Existing tables are preserved. Every change is an additive migration tracked
with `PRAGMA user_version`, replacing the ad-hoc `ALTER`/`PRAGMA table_info`
dance in `init_db()`.

### Added columns

`lists`
- `version INTEGER NOT NULL DEFAULT 1` — bumped on every mutation
- `updated_at TEXT`
- `reset_hour INTEGER NULL`, `reset_tz TEXT NULL`, `next_reset_at TEXT NULL`

`tasks`
- `created_by INTEGER NULL` → users(id)
- `completed_at TEXT NULL`, `completed_by INTEGER NULL`
- `updated_at TEXT`
- `deleted_at TEXT NULL` — soft delete, powers undo
- `position` stays INTEGER, but new rows use `max(position) + 1024` and reorder
  writes `1024 * i`. Fixes today's "everything is position 0, ties broken by
  id DESC" accident.

`list_shares`
- `role TEXT NOT NULL DEFAULT 'editor'` (`editor` | `viewer`)
- `created_at TEXT`, `joined_at TEXT`
- `via_token_id INTEGER NULL` — which link let them in, so the owner can see it

`list_invites` (retired)
- No longer written. Left in place so an old database opens without a
  destructive migration; the app ignores it. Pending invites cannot be
  converted, because a token has no way to reach the person — they have to be
  re-sent as links once.

### New tables

```sql
invite_tokens(id, token_hash, list_id, role, created_by, created_at,
              expires_at, uses, max_uses)
list_blocks(list_id, user_id, blocked_by, created_at)
activity(id, list_id, user_id, action, task_id, detail, created_at)
idempotency(key PRIMARY KEY, user_id, status, response_json, created_at)
```

- `invite_tokens` stores only a SHA-256 hash; the token itself exists once, in
  the link. `max_uses` defaults to 1, so a link admits one person unless the
  owner deliberately makes it multi-use. Expiry default 7 days.
- `list_blocks` makes removal sticky. Removing a member deletes their share and
  records a block, so a still-live link cannot be used to walk back in. The
  owner can unblock from the members panel.
- `activity` is an append-only feed, capped per list (keep last 200).
- `idempotency` makes offline-queue replays safe; rows expire after 24 h.

Presence is **in-memory only** — `dict[list_id][client_id] = (user, name, ts)`
with a 30 s TTL. No table, no writes.

### Scheduled reset

Lazy, not a background job: on any read of a list with `next_reset_at <= now`,
set every task back to `pending`, append an activity row, and advance
`next_reset_at` to the next occurrence. Survives restarts, needs no scheduler,
and can't drift. Timezone via `zoneinfo` + IANA name (Linux has tzdata; Windows
dev needs the `tzdata` wheel, which is dev-only and not a runtime requirement).

---

## API

Same shape as today where possible, so the frontend port is mechanical.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/me` | Replaces server-side user inlining |
| GET | `/api/lists` | Includes task counts, role, owner name |
| POST | `/api/lists` | Idempotency-Key supported |
| PATCH | `/api/lists/{id}` | Rename, resettable, schedule; `If-Match` |
| DELETE | `/api/lists/{id}` | Owner only; soft-delete window |
| GET | `/api/lists/{id}/state?since=N` | **New.** 304 or full state |
| POST | `/api/lists/{id}/tasks` | Idempotency-Key supported |
| PATCH | `/api/tasks/{id}` | Status; sets `completed_by/at` |
| DELETE | `/api/tasks/{id}` | Soft delete |
| POST | `/api/tasks/{id}/restore` | **New.** Undo |
| POST | `/api/lists/{id}/reorder` | `If-Match` on `version` |
| POST | `/api/lists/{id}/reset` | Owner only |
| POST | `/api/lists/{id}/invite-links` | **New.** Role, expiry, `max_uses` |
| GET | `/api/lists/{id}/invite-links` | **New.** Active links with use counts |
| DELETE | `/api/lists/{id}/invite-links/{link_id}` | **New.** Revoke a link |
| PATCH | `/api/lists/{id}/members/{user_id}` | **New.** Change role |
| DELETE | `/api/lists/{id}/members/{user_id}` | **New.** Remove access and block |
| DELETE | `/api/lists/{id}/blocks/{user_id}` | **New.** Unblock |
| GET | `/join/{token}` | **New.** Claim, or sign in then claim |
| GET | `/api/lists/{id}/export` | **New.** JSON |
| POST | `/api/import` | **New.** JSON |

`/state` response:

```json
{
  "version": 42,
  "list":   { "id": 3, "title": "...", "role": "owner", ... },
  "tasks":  [ ... ],
  "members": [ ... ],
  "blocks":  [ ... ],
  "activity": [ ... ],
  "presence": [ { "name": "Sam", "client_id": "..." } ],
  "server_time": "2026-11-20T20:31:00Z"
}
```

At family scale the full payload is under 10 KB, so no delta encoding. The win
comes from the 304, not from partial bodies.

### Polling policy

- Enabled only when `list.share_count > 0` **or** an invite link exists.
- Only the open list, only when `document.visibilityState === 'visible'`.
- 4 s while active, 20 s after 2 minutes idle, paused when hidden.
- `ETag: W/"<version>"`; `304` carries no body.
- A mutation returns the new version, so the client skips the next poll.

---

## Auth and sessions

Google OIDC authorization-code flow with PKCE (S256), entirely via
`urllib.request`:

1. `/auth/login` → random `state` + `code_verifier`, stored in a short-lived
   signed cookie; redirect to Google.
2. `/auth/callback` → verify `state`, POST the code to the token endpoint,
   then `GET` the userinfo endpoint with the access token over TLS.
3. Trust `email`, `email_verified`, `sub`, `name` from that TLS-authenticated
   response, and reject the sign-in if `email_verified` is not true. No JWT
   signature verification needed, so no RSA code in the project.
4. `upsert_user` keeps today's behaviour: identity is `sub`, email is refreshed.
   A pending `/join/<token>` is claimed immediately after sign-in, which is how
   someone who has never used the app ends up on the shared list.

Session cookie: `base64url(payload).base64url(HMAC-SHA256)`, with `iat`, `exp`
(30 days, sliding), `csrf`, and the user record. `HttpOnly`, `SameSite=Lax`,
`Secure` when `SESSION_COOKIE_SECURE=1`, `__Host-` prefix when secure.

Every state-changing request must pass **both** an `Origin`/`Sec-Fetch-Site`
check and a session CSRF token (sent as `X-CSRF-Token`). This is stronger than
today, where only logout is protected.

`/auth/dev-login` is ported unchanged and still 404s unless explicitly enabled.

---

## Security posture

With no Alpine there is no `new Function`, and with no server-rendered user
there is no per-request nonce. The policy becomes genuinely strict:

```
default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none';
form-action 'self'; frame-ancestors 'none'; require-trusted-types-for 'script'
```

No `unsafe-inline`, no `unsafe-eval`, no nonce. Consequences:

- The Cloudflare Web Analytics beacon currently injected into the HTML will be
  blocked. That is the correct outcome: it is a third-party script and the
  README's "no third-party script origins" claim is only true because it
  carries the nonce. Either disable CF Web Analytics or accept the block.
- The offline page moves to a precached `/offline.html` so no inline script is
  needed anywhere.

Also added: `Cache-Control: no-store` on `/api/*` and the shell;
`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
`X-DNS-Prefetch-Control`, HSTS as today; `Permissions-Policy`.

**Resolved by removal:** `/api/lists/{id}/share` currently returns
`status: "shared" | "invited"` and an `already` flag, which discloses whether an
address has an account — contradicting the README and the test named
"email enumeration is closed". v2 has no endpoint that accepts an email
address, so the surface is gone rather than merely hidden. Members are
addressed by `user_id`; links are bearer tokens.

---

## Collaboration model

1. **Sync** — version + conditional poll, scoped as above.
2. **Attribution** — `created_by`, `completed_by`, `completed_at`, rendered as
   "Sam · 2m ago" on completed rows.
3. **Activity** — last 20 events per list, shown in a collapsible panel.
4. **Conflict safety** — `If-Match` on structural edits, `409` + fresh state on
   mismatch, optimistic UI with rollback. Status toggles are last-write-wins.
5. **Joining** — invite links carrying a role, expiry, and a use limit (one by
   default). Owner/editor/viewer roles are enforced server-side in every
   handler, never in the UI. A visitor who opens a link without a session is
   sent through Google sign-in and then lands on the list.
6. **Leaving** — removing a member deletes the share *and* blocks them, so a
   link that is still circulating cannot be used to rejoin. Revoking a link and
   removing a person are independent: revoking the link stops new joins,
   removing the person takes away existing access.
7. **Presence** — "Also here: Sam" from the poll, TTL 30 s.
8. **Offline** — IndexedDB queue with `Idempotency-Key`, flushed on `online`;
   cached last-known state renders immediately, edits are queued, and the header
   shows "3 changes pending".

Explicitly **out of scope**: websockets, web push (needs VAPID + ECDSA
verification, i.e. a crypto dependency), real-time cursors, comments.

---

## Frontend

- Static `index.html`, no user data. The service worker may cache `/` safely,
  which is what makes a usable offline mode possible at all.
- One small render layer: a `<template>` element is cloned per row and filled
  with `textContent` only. No `innerHTML` with user data, and no string
  concatenation of untrusted values.
- Events via delegation on the list container, so re-renders don't rebind.
- Replaces `alert()`/`confirm()` with an in-page toast + undo, and a proper
  `<dialog>` for destructive list deletion.
- Drag to reorder via Pointer Events (mouse + touch), keeping ▲/▼ as a
  keyboard-accessible fallback.
- Typography and targets: 16 px base, 24 px checkboxes, ≥44 px rows, visible
  focus rings, `aria-live` for errors, `role="list"`, labelled controls,
  `prefers-reduced-motion`, `color-scheme: light dark`.
- Dark mode: follows `prefers-color-scheme` on first visit, manual toggle
  persisted after that.
- `<link rel="icon" href="/static/icon-192.png">`, `apple-touch-icon`, and
  `theme-color` per scheme.

### PWA / installability

Nothing about the rebuild weakens installability; it improves it. The
requirements are unchanged and all still met: HTTPS, a manifest with `name`,
`short_name`, `start_url`, `display: standalone`, 192 px and 512 px icons, and a
service worker with a `fetch` handler. The manifest and both icons stay exactly
as they are.

Budget, uncompressed: HTML ~6 KB, CSS ~9 KB, JS ~22 KB, service worker ~4 KB.
That is roughly 41 KB where today's page pulls 16 KB of CSS **plus** 56 KB of
Alpine. The app gets smaller, boots offline, and installs to the home screen
with the same icon.

Invite links add no frontend weight beyond one small dialog — they are
server-side records and a `/join/<token>` route.

---

## Testing

`unittest`, stdlib only. `tests/test_api.py` starts the real ASGI app through
uvicorn on an ephemeral port against a temporary database, then drives it with
`http.client` and forged session cookies.

Ported and extended from today's suite:
- auth rejection, per-list access control, validation limits, cascades
- invite link claim, revocation, expiry, single-use, and case-insensitive email
  matching for the account that claims it
- **new:** viewer cannot mutate; `If-Match` conflict returns 409; reorder is
  atomic; soft-delete + restore; idempotent replay returns the first response;
  scheduled reset fires exactly once; `/state` returns 304; activity rows are
  written for each mutation; an expired or already-used link is refused; a
  removed member cannot rejoin through a live link, and unblocking restores
  that ability.

---

## Deployment

Target: the existing Proxmox LXC, with systemd running uvicorn from the repo
checkout and Cloudflare Tunnel in front. The current update command stays
valid, because migrations run transactionally at startup and are idempotent:

```
git pull origin main && systemctl restart checklist
```

### Service unit

Ship a `deploy/checklist.service` in the repo so the configuration is version
controlled rather than remembered:

```ini
[Unit]
Description=Collaborative Checklists
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=checklist
WorkingDirectory=/opt/checklist
ExecStart=/opt/checklist/.venv/bin/uvicorn main:app \
    --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=2

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/checklist
ProtectKernelTunables=yes
ProtectKernelModules=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
```

`--proxy-headers` is spelled out rather than relying on uvicorn's default: with
it off, rate limiting would key on the tunnel's address and every user would
share one bucket. It is only safe because uvicorn is bound to `127.0.0.1`,
which is also the default `--forwarded-allow-ips`.

`config.py` loads `.env` from the checkout, as today, so there is no
`EnvironmentFile` to keep in sync. Note that `ProtectSystem=strict` makes the
checkout read-only, so this unit assumes the database has been moved to
`/var/lib/checklist`. If you keep `checklist.db` beside the code, add the
checkout to `ReadWritePaths` instead.

### Cutover from the current deployment

1. Tag the current state: `git tag pre-v2`.
2. Back up: `.venv/bin/python main.py backup --out /var/lib/checklist/backup-$(date +%F).db`.
   Uses `sqlite3.Connection.backup()`, so it is consistent while the app runs.
3. Deploy `v2` on a second port (`8001`) with a second unit and a staging
   hostname in the tunnel. Point it at a **copy** of the live database.
4. Walk through both accounts: lists, shares, reset, install prompt, offline.
5. Merge `v2` into `main`, `git pull origin main`, `systemctl restart checklist`.
6. Keep the old checkout and the backup for at least a week.

Rollback is `git checkout pre-v2 && systemctl restart checklist` plus restoring
the backup if a migration needs to be undone. Migrations only add columns and
tables, so an old binary can still read the migrated database — the one
exception is that `list_invites` is ignored rather than dropped, precisely so a
rollback does not lose data.

### Optional: move the database out of the checkout

Today the default is `checklist.db` beside the code, which survives `git pull`
but not `git clean -fdx`, and puts the database inside a directory that a
deploy touches. Setting `DATABASE_PATH=/var/lib/checklist/checklist.db` and
adding that path to `ReadWritePaths` is a one-line improvement. It is optional
and orthogonal to the rebuild; I would do it during the cutover while a backup
already exists.

### Backups

`python main.py backup --out <path>` and `python main.py restore --from <path>`,
both built on the stdlib `sqlite3` backup API. A weekly systemd timer is enough;
the README will include the unit.

---

## Milestones

| # | Deliverable |
|---|---|
| 1 | Skeleton: config, migrations, ASGI router, sessions, headers, rate limit, static serving, `/api/me`, tests green |
| 2 | Parity: lists, tasks, members, reset, PWA shell, CSS/JS port |
| 3 | Collaboration: versioning, `/state`, polling, presence, activity, attribution, 409 handling |
| 4 | Features: invite links, roles, scheduled reset, undo, offline queue, export/import |
| 5 | Polish: a11y pass, drag-and-drop, search, toasts, README rewrite |

---

## Resolved

- Deployment is a Proxmox LXC running systemd from the checkout; see above.
- Email invites are replaced by links. `list_invites` is retired, not dropped,
  so a rollback still opens the database.
- Viewers can read the activity feed; they cannot see invite links or manage
  members.
- The favicon is the existing app icon.

## Open questions

1. Relocate the database to `/var/lib/checklist` during cutover, or leave it
   beside the code?
2. Should completed tasks stay until cleared, or auto-archive after N days?
   (Proposed: stay, with a "clear completed" action; only soft-deleted rows are
   purged, after 30 days.)
