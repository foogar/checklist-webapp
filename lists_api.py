"""Lists, tasks, reordering, and reset.

Access is decided here, on every request, from the database. The frontend
hiding a button is a convenience, never the control.

Every mutation bumps ``lists.version``. Nothing reads that yet; it exists so the
sync layer in the next milestone can answer "has this list changed since N?"
with a single integer instead of comparing rows.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import auth
import presence
from config import settings
from database import connection, transaction
from util import utcnow
from web import HTTPError, Request, Response, json_response, no_content, redirect

MAX_LIST_TITLE = 200
MAX_TASK_TITLE = 500
POSITION_STEP = 1024

LIST_COLUMNS = """
    l.id, l.title, l.is_resettable, l.version, l.created_at, l.user_id,
    l.reset_hour, l.reset_tz, l.next_reset_at,
    u.name AS owner_name,
    CASE WHEN l.user_id = :uid THEN 'owner' ELSE COALESCE(ls.role, 'viewer') END AS role,
    1 + (SELECT COUNT(*) FROM list_shares s WHERE s.list_id = l.id) AS member_count,
    (SELECT COUNT(*) FROM tasks t
      WHERE t.list_id = l.id AND t.deleted_at IS NULL) AS task_count,
    (SELECT COUNT(*) FROM tasks t
      WHERE t.list_id = l.id AND t.deleted_at IS NULL AND t.status = 'pending') AS pending_count
"""

LIST_FROM = """
    FROM lists l
    JOIN users u ON u.id = l.user_id
    LEFT JOIN list_shares ls ON ls.list_id = l.id AND ls.user_id = :uid
"""

# A blocked user is treated as having no access even if a share row somehow
# still exists. Removal deletes both, so this is defence in depth.
LIST_ACCESS = """
    (l.user_id = :uid OR ls.user_id = :uid)
    AND NOT EXISTS (
        SELECT 1 FROM list_blocks b
        WHERE b.list_id = l.id AND b.user_id = :uid
    )
"""

TASK_SELECT = """
    SELECT t.id, t.title, t.status, t.position, t.created_at, t.updated_at,
           t.completed_at, t.created_by, cu.name AS created_by_name,
           t.completed_by, du.name AS completed_by_name
    FROM tasks t
    LEFT JOIN users cu ON cu.id = t.created_by
    LEFT JOIN users du ON du.id = t.completed_by
    WHERE t.list_id = :list_id AND t.deleted_at IS NULL
    ORDER BY t.position ASC, t.id ASC
"""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _body(request: Request) -> dict[str, Any]:
    data = request.json()
    if not isinstance(data, dict):
        raise HTTPError(422, "A JSON object is required")
    return data


def _text(data: dict[str, Any], field: str, max_length: int, required: bool = True) -> str:
    value = data.get(field)
    if value is None:
        if required:
            raise HTTPError(422, f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise HTTPError(422, f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise HTTPError(422, f"{field} cannot be empty")
    if len(value) > max_length:
        raise HTTPError(422, f"{field} must be {max_length} characters or fewer")
    return value


def _flag(data: dict[str, Any], field: str, default: bool = False) -> bool:
    value = data.get(field, default)
    if not isinstance(value, bool):
        raise HTTPError(422, f"{field} must be true or false")
    return value


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------


def _role(conn: sqlite3.Connection, list_id: int, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM lists WHERE id = ?", (list_id,)
    ).fetchone()
    if row is None:
        return None
    if row["user_id"] == user_id:
        return "owner"

    blocked = conn.execute(
        "SELECT 1 FROM list_blocks WHERE list_id = ? AND user_id = ?",
        (list_id, user_id),
    ).fetchone()
    if blocked:
        return None

    share = conn.execute(
        "SELECT role FROM list_shares WHERE list_id = ? AND user_id = ?",
        (list_id, user_id),
    ).fetchone()
    return share["role"] if share else None


def _require(
    conn: sqlite3.Connection, list_id: int, user_id: int, write: bool = False
) -> str:
    """Return the caller's role, or raise.

    404 rather than 403 when there is no access at all: whether a list exists
    is not something a stranger needs to learn.
    """
    role = _role(conn, list_id, user_id)
    if role is None:
        raise HTTPError(404, "List not found")
    if write and role == "viewer":
        raise HTTPError(403, "This list is read-only")
    return role


def _task_list_id(conn: sqlite3.Connection, task_id: int) -> int:
    row = conn.execute(
        "SELECT list_id FROM tasks WHERE id = ? AND deleted_at IS NULL", (task_id,)
    ).fetchone()
    if row is None:
        raise HTTPError(404, "Task not found")
    return row["list_id"]


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def _list_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "is_resettable": bool(row["is_resettable"]),
        "role": row["role"],
        "owner_name": row["owner_name"],
        "member_count": row["member_count"],
        "version": row["version"],
        "task_count": row["task_count"],
        "pending_count": row["pending_count"],
        "created_at": row["created_at"],
        "reset_hour": row["reset_hour"],
        "reset_tz": row["reset_tz"],
        "next_reset_at": row["next_reset_at"],
    }


def _task_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "position": row["position"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
        "created_by_name": row["created_by_name"],
        "completed_at": row["completed_at"],
        "completed_by": row["completed_by"],
        "completed_by_name": row["completed_by_name"],
    }


def _fetch_list(conn: sqlite3.Connection, list_id: int, user_id: int) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT {LIST_COLUMNS} {LIST_FROM} WHERE l.id = :list_id AND {LIST_ACCESS}",
        {"uid": user_id, "list_id": list_id},
    ).fetchone()
    if row is None:
        raise HTTPError(404, "List not found")
    return _list_json(row)


def _tasks(conn: sqlite3.Connection, list_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(TASK_SELECT, {"list_id": list_id}).fetchall()
    return [_task_json(row) for row in rows]


def _version(conn: sqlite3.Connection, list_id: int) -> int:
    row = conn.execute("SELECT version FROM lists WHERE id = ?", (list_id,)).fetchone()
    return int(row["version"]) if row else 0


def _touch(conn: sqlite3.Connection, list_id: int) -> None:
    conn.execute(
        "UPDATE lists SET version = version + 1, updated_at = ? WHERE id = ?",
        (utcnow(), list_id),
    )


def _fetch_task(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    row = conn.execute(
        TASK_SELECT.replace("WHERE t.list_id = :list_id", "WHERE t.id = :task_id"),
        {"task_id": task_id},
    ).fetchone()
    if row is None:
        raise HTTPError(404, "Task not found")
    return _task_json(row)


def _record(
    conn: sqlite3.Connection,
    list_id: int,
    user_id: int,
    action: str,
    task_id: int | None = None,
    detail: str | None = None,
) -> None:
    """Append to the list's activity feed, keeping the last 200 rows.

    The user's name is not stored: it is joined at read time, so a rename
    updates history rather than leaving stale names behind.
    """
    conn.execute(
        "INSERT INTO activity (list_id, user_id, action, task_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (list_id, user_id, action, task_id, detail, utcnow()),
    )
    conn.execute(
        "DELETE FROM activity WHERE list_id = ? AND id NOT IN "
        "(SELECT id FROM activity WHERE list_id = ? ORDER BY id DESC LIMIT 200)",
        (list_id, list_id),
    )


def _activity(conn: sqlite3.Connection, list_id: int, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.id, a.action, a.task_id, a.detail, a.created_at,
               a.user_id, u.name AS user_name
        FROM activity a
        LEFT JOIN users u ON u.id = a.user_id
        WHERE a.list_id = ?
        ORDER BY a.id DESC
        LIMIT ?
        """,
        (list_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _members(
    conn: sqlite3.Connection, list_id: int, include_email: bool = False
) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []

    owner = conn.execute(
        "SELECT u.id, u.name, u.email FROM lists l "
        "JOIN users u ON u.id = l.user_id WHERE l.id = ?",
        (list_id,),
    ).fetchone()
    if owner:
        entry: dict[str, Any] = {
            "user_id": owner["id"],
            "name": owner["name"],
            "role": "owner",
            "joined_at": None,
        }
        if include_email:
            entry["email"] = owner["email"]
        members.append(entry)

    for row in conn.execute(
        """
        SELECT u.id, u.name, u.email, ls.role, ls.joined_at
        FROM list_shares ls
        JOIN users u ON u.id = ls.user_id
        WHERE ls.list_id = ?
        ORDER BY LOWER(u.name), u.id
        """,
        (list_id,),
    ):
        entry = {
            "user_id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "joined_at": row["joined_at"],
        }
        if include_email:
            entry["email"] = row["email"]
        members.append(entry)

    return members


def _if_match(request: Request) -> int | None:
    """Read an expected version from If-Match, or None if absent.

    Accepts both `12` and the weak ETag form `W/"12"`, so the client can send
    whatever it already has without a second convention.
    """
    raw = request.header("if-match").strip()
    if not raw:
        return None
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip('"')
    try:
        return int(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Sharing and scheduling helpers
# --------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _invite_links(conn: sqlite3.Connection, list_id: int) -> list[dict[str, Any]]:
    now = utcnow()
    rows = conn.execute(
        "SELECT id, role, created_at, expires_at, uses, max_uses, revoked_at "
        "FROM invite_tokens WHERE list_id = ? ORDER BY id DESC",
        (list_id,),
    ).fetchall()
    links: list[dict[str, Any]] = []
    for row in rows:
        active = (
            row["revoked_at"] is None
            and (row["expires_at"] is None or row["expires_at"] > now)
            and row["uses"] < row["max_uses"]
        )
        links.append(
            {
                "id": row["id"],
                "role": row["role"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "uses": row["uses"],
                "max_uses": row["max_uses"],
                "revoked_at": row["revoked_at"],
                "active": active,
            }
        )
    return links


def _blocks(conn: sqlite3.Connection, list_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT b.user_id, u.name, u.email, b.created_at "
        "FROM list_blocks b JOIN users u ON u.id = b.user_id "
        "WHERE b.list_id = ? ORDER BY LOWER(u.name), u.id",
        (list_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        # Windows ships no tz database unless the optional `tzdata` wheel is
        # installed. Production is Linux; falling back to UTC keeps the app
        # usable in development instead of refusing to save a schedule.
        return None


def _next_reset(hour: int, tz_name: str) -> str:
    tz = _zone(tz_name) or dt_timezone.utc
    now = datetime.now(tz)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return (
        candidate.astimezone(dt_timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _apply_due_reset(conn: sqlite3.Connection, list_id: int) -> bool:
    """Run a scheduled reset if one is due, exactly once.

    The cheap check happens outside a transaction so ordinary reads do not take
    a write lock. The authoritative check is repeated inside it: two clients
    polling in the same second must not both advance next_reset_at.
    """
    row = conn.execute(
        "SELECT next_reset_at FROM lists WHERE id = ?", (list_id,)
    ).fetchone()
    if row is None or not row["next_reset_at"] or row["next_reset_at"] > utcnow():
        return False

    with transaction(conn):
        row = conn.execute(
            "SELECT next_reset_at, reset_hour, reset_tz FROM lists WHERE id = ?",
            (list_id,),
        ).fetchone()
        now = utcnow()
        if row is None or not row["next_reset_at"] or row["next_reset_at"] > now:
            return False

        conn.execute(
            "UPDATE tasks SET status = 'pending', completed_at = NULL, "
            "completed_by = NULL, updated_at = ? "
            "WHERE list_id = ? AND deleted_at IS NULL",
            (now, list_id),
        )
        _touch(conn, list_id)
        _record(conn, list_id, None, "list.auto_reset")
        next_at = _next_reset(int(row["reset_hour"] or 0), row["reset_tz"] or "UTC")
        conn.execute(
            "UPDATE lists SET next_reset_at = ? WHERE id = ?", (next_at, list_id)
        )
    return True


def _claim_invite(conn: sqlite3.Connection, token: str, user_id: int) -> int:
    row = conn.execute(
        "SELECT * FROM invite_tokens WHERE token_hash = ?", (_hash_token(token),)
    ).fetchone()
    if row is None:
        raise HTTPError(404, "This invite link is not valid.")

    list_id = row["list_id"]
    owner = conn.execute(
        "SELECT user_id FROM lists WHERE id = ?", (list_id,)
    ).fetchone()
    if owner is None:
        raise HTTPError(404, "This list no longer exists.")
    if owner["user_id"] == user_id:
        return list_id

    blocked = conn.execute(
        "SELECT 1 FROM list_blocks WHERE list_id = ? AND user_id = ?",
        (list_id, user_id),
    ).fetchone()
    if blocked:
        raise HTTPError(403, "You no longer have access to this list.")

    existing = conn.execute(
        "SELECT role FROM list_shares WHERE list_id = ? AND user_id = ?",
        (list_id, user_id),
    ).fetchone()
    if existing:
        # Opening a link again is not a new join, so it must not consume a use.
        return list_id

    with transaction(conn):
        fresh = conn.execute(
            "SELECT uses, max_uses, revoked_at, expires_at, role "
            "FROM invite_tokens WHERE id = ?",
            (row["id"],),
        ).fetchone()
        now = utcnow()
        if fresh is None or fresh["revoked_at"] is not None:
            raise HTTPError(410, "This invite link is no longer valid.")
        if fresh["expires_at"] and fresh["expires_at"] <= now:
            raise HTTPError(410, "This invite link has expired.")
        if fresh["uses"] >= fresh["max_uses"]:
            raise HTTPError(410, "This invite link has already been used.")

        conn.execute(
            "INSERT INTO list_shares "
            "(list_id, user_id, role, created_at, joined_at, via_token_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (list_id, user_id, fresh["role"], now, now, row["id"]),
        )
        conn.execute(
            "UPDATE invite_tokens SET uses = uses + 1 WHERE id = ?", (row["id"],)
        )
        _touch(conn, list_id)
        _record(conn, list_id, user_id, "member.joined", detail=fresh["role"])

    return list_id


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def register(route) -> None:
    @route("GET", r"/api/lists")
    async def get_lists(request: Request) -> Response:
        user = auth.require_user(request)
        with connection() as conn:
            # A scheduled reset is applied when the list is next read, so the
            # overview counts are never a day behind.
            for row in conn.execute(
                f"SELECT l.id {LIST_FROM} WHERE {LIST_ACCESS}", {"uid": user["id"]}
            ):
                _apply_due_reset(conn, row["id"])

            rows = conn.execute(
                f"SELECT {LIST_COLUMNS} {LIST_FROM} WHERE {LIST_ACCESS} "
                "ORDER BY l.created_at DESC, l.id DESC",
                {"uid": user["id"]},
            ).fetchall()
        return json_response({"lists": [_list_json(row) for row in rows]})

    @route("POST", r"/api/lists")
    async def create_list(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        data = _body(request)
        title = _text(data, "title", MAX_LIST_TITLE)
        resettable = _flag(data, "is_resettable")

        with connection() as conn:
            with transaction(conn):
                cursor = conn.execute(
                    "INSERT INTO lists (user_id, title, is_resettable, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user["id"], title, 1 if resettable else 0, utcnow(), utcnow()),
                )
                list_id = cursor.lastrowid
                _record(conn, list_id, user["id"], "list.created", detail=title)
            result = _fetch_list(conn, list_id, user["id"])
        return json_response(result, status=201)

    @route("GET", r"/api/lists/(?P<list_id>\d+)")
    async def get_list(request: Request) -> Response:
        user = auth.require_user(request)
        list_id = int(request.params["list_id"])
        with connection() as conn:
            _require(conn, list_id, user["id"])
            return json_response(_fetch_list(conn, list_id, user["id"]))

    @route("PATCH", r"/api/lists/(?P<list_id>\d+)")
    async def update_list(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])
        data = _body(request)

        assignments: list[str] = []
        values: list[Any] = []
        new_title: str | None = None
        if "title" in data:
            new_title = _text(data, "title", MAX_LIST_TITLE)
            assignments.append("title = ?")
            values.append(new_title)
        if "is_resettable" in data:
            assignments.append("is_resettable = ?")
            values.append(1 if _flag(data, "is_resettable") else 0)
        if "reset_hour" in data:
            hour = data["reset_hour"]
            if hour is None:
                assignments.extend(
                    ["reset_hour = NULL", "reset_tz = NULL", "next_reset_at = NULL"]
                )
            else:
                if (
                    not isinstance(hour, int)
                    or isinstance(hour, bool)
                    or not 0 <= hour <= 23
                ):
                    raise HTTPError(
                        422, "reset_hour must be an hour from 0 to 23, or null"
                    )
                tz_name = data.get("reset_tz") or "UTC"
                if not isinstance(tz_name, str) or not re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_+\-/]{0,63}", tz_name
                ):
                    raise HTTPError(422, "reset_tz must be an IANA time zone name")
                assignments.extend(
                    ["reset_hour = ?", "reset_tz = ?", "next_reset_at = ?"]
                )
                values.extend([hour, tz_name, _next_reset(hour, tz_name)])
        if not assignments:
            raise HTTPError(422, "Nothing to update")

        expected = _if_match(request)
        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can change this list")
            if expected is not None and expected != _version(conn, list_id):
                raise HTTPError(409, "The list changed; reload and try again")
            with transaction(conn):
                values.extend([utcnow(), list_id])
                conn.execute(
                    f"UPDATE lists SET {', '.join(assignments)}, "
                    "version = version + 1, updated_at = ? WHERE id = ?",
                    values,
                )
                if new_title is not None:
                    _record(conn, list_id, user["id"], "list.renamed", detail=new_title)
                else:
                    _record(conn, list_id, user["id"], "list.settings")
            result = _fetch_list(conn, list_id, user["id"])
        return json_response(result)

    @route("DELETE", r"/api/lists/(?P<list_id>\d+)")
    async def delete_list(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])

        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can delete this list")
            with transaction(conn):
                # ON DELETE CASCADE removes tasks, shares, blocks, tokens and
                # activity, provided foreign_keys is on. It is, in connect().
                conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        return no_content()

    # -- tasks ------------------------------------------------------------

    @route("GET", r"/api/lists/(?P<list_id>\d+)/tasks")
    async def get_tasks(request: Request) -> Response:
        user = auth.require_user(request)
        list_id = int(request.params["list_id"])
        with connection() as conn:
            _require(conn, list_id, user["id"])
            _apply_due_reset(conn, list_id)
            return json_response(
                {"version": _version(conn, list_id), "tasks": _tasks(conn, list_id)}
            )

    @route("POST", r"/api/lists/(?P<list_id>\d+)/tasks")
    async def create_task(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])
        data = _body(request)
        title = _text(data, "title", MAX_TASK_TITLE)

        with connection() as conn:
            _require(conn, list_id, user["id"], write=True)
            with transaction(conn):
                position = conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + ? FROM tasks WHERE list_id = ?",
                    (POSITION_STEP, list_id),
                ).fetchone()[0]
                cursor = conn.execute(
                    "INSERT INTO tasks "
                    "(list_id, title, position, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (list_id, title, position, user["id"], utcnow(), utcnow()),
                )
                task_id = cursor.lastrowid
                _touch(conn, list_id)
                _record(conn, list_id, user["id"], "task.added", task_id, title)
            result = _fetch_task(conn, task_id)
            version = _version(conn, list_id)
        return json_response({"task": result, "version": version}, status=201)

    @route("PATCH", r"/api/tasks/(?P<task_id>\d+)")
    async def update_task(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        task_id = int(request.params["task_id"])
        data = _body(request)

        assignments: list[str] = []
        values: list[Any] = []
        new_title: str | None = None

        if "title" in data:
            new_title = _text(data, "title", MAX_TASK_TITLE)
            assignments.append("title = ?")
            values.append(new_title)

        if "status" in data:
            status = data["status"]
            if status not in {"pending", "completed"}:
                raise HTTPError(422, "status must be 'pending' or 'completed'")
            assignments.extend(["status = ?", "completed_at = ?", "completed_by = ?"])
            if status == "completed":
                values.extend([status, utcnow(), user["id"]])
            else:
                values.extend([status, None, None])

        if not assignments:
            raise HTTPError(422, "Nothing to update")

        with connection() as conn:
            list_id = _task_list_id(conn, task_id)
            _require(conn, list_id, user["id"], write=True)
            row = conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            activity_title = new_title if new_title is not None else (row["title"] if row else "")
            with transaction(conn):
                values.extend([utcnow(), task_id])
                conn.execute(
                    f"UPDATE tasks SET {', '.join(assignments)}, updated_at = ? "
                    "WHERE id = ?",
                    values,
                )
                _touch(conn, list_id)
                if "status" in data:
                    action = (
                        "task.completed"
                        if data["status"] == "completed"
                        else "task.reopened"
                    )
                else:
                    action = "task.renamed"
                _record(conn, list_id, user["id"], action, task_id, activity_title)
            result = _fetch_task(conn, task_id)
            version = _version(conn, list_id)
        return json_response({"task": result, "version": version})

    @route("DELETE", r"/api/tasks/(?P<task_id>\d+)")
    async def delete_task(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        task_id = int(request.params["task_id"])

        with connection() as conn:
            list_id = _task_list_id(conn, task_id)
            _require(conn, list_id, user["id"], write=True)
            row = conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            title = row["title"] if row else ""
            with transaction(conn):
                # Soft delete, so an accidental tap can be undone.
                conn.execute(
                    "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ?",
                    (utcnow(), utcnow(), task_id),
                )
                _touch(conn, list_id)
                _record(conn, list_id, user["id"], "task.deleted", task_id, title)
            version = _version(conn, list_id)
        return json_response({"deleted": True, "version": version})

    @route("POST", r"/api/tasks/(?P<task_id>\d+)/restore")
    async def restore_task(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        task_id = int(request.params["task_id"])

        with connection() as conn:
            row = conn.execute(
                "SELECT list_id, title FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise HTTPError(404, "Task not found")
            list_id = row["list_id"]
            _require(conn, list_id, user["id"], write=True)
            with transaction(conn):
                conn.execute(
                    "UPDATE tasks SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                    (utcnow(), task_id),
                )
                _touch(conn, list_id)
                _record(conn, list_id, user["id"], "task.restored", task_id, row["title"])
            result = _fetch_task(conn, task_id)
            version = _version(conn, list_id)
        return json_response({"task": result, "version": version})

    @route("POST", r"/api/lists/(?P<list_id>\d+)/reorder")
    async def reorder_tasks(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])
        data = _body(request)

        task_ids = data.get("task_ids")
        if not isinstance(task_ids, list) or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in task_ids
        ):
            raise HTTPError(422, "task_ids must be a list of integers")
        if len(set(task_ids)) != len(task_ids):
            raise HTTPError(422, "task_ids contains duplicates")

        with connection() as conn:
            _require(conn, list_id, user["id"], write=True)
            expected = _if_match(request)
            if expected is not None and expected != _version(conn, list_id):
                raise HTTPError(409, "The list changed; reload and try again")
            existing = {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM tasks WHERE list_id = ? AND deleted_at IS NULL",
                    (list_id,),
                )
            }
            if set(task_ids) != existing:
                # The caller's view is stale, or they are trying to move a task
                # from another list. Either way, refuse rather than guess.
                raise HTTPError(409, "The list changed; reload and try again")

            with transaction(conn):
                for index, task_id in enumerate(task_ids, start=1):
                    conn.execute(
                        "UPDATE tasks SET position = ?, updated_at = ? "
                        "WHERE id = ? AND list_id = ?",
                        (index * POSITION_STEP, utcnow(), task_id, list_id),
                    )
                _touch(conn, list_id)
            version = _version(conn, list_id)
        return json_response({"version": version})

    @route("POST", r"/api/lists/(?P<list_id>\d+)/reset")
    async def reset_list(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])

        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can reset this list")
            with transaction(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'pending', completed_at = NULL, "
                    "completed_by = NULL, updated_at = ? "
                    "WHERE list_id = ? AND deleted_at IS NULL",
                    (utcnow(), list_id),
                )
                _touch(conn, list_id)
                _record(conn, list_id, user["id"], "list.reset")
            result = {"version": _version(conn, list_id), "tasks": _tasks(conn, list_id)}
        return json_response(result)

    # -- sharing ----------------------------------------------------------

    @route("POST", r"/api/lists/(?P<list_id>\d+)/invite-links")
    async def create_invite_link(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])
        data = _body(request)

        role = data.get("role", "editor")
        if role not in {"editor", "viewer"}:
            raise HTTPError(422, "role must be 'editor' or 'viewer'")

        max_uses = data.get("max_uses", 1)
        if (
            not isinstance(max_uses, int)
            or isinstance(max_uses, bool)
            or not 1 <= max_uses <= 100
        ):
            raise HTTPError(422, "max_uses must be between 1 and 100")

        days = data.get("expires_in_days", 7)
        if (
            not isinstance(days, int)
            or isinstance(days, bool)
            or not 1 <= days <= 90
        ):
            raise HTTPError(422, "expires_in_days must be between 1 and 90")

        token = secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(dt_timezone.utc) + timedelta(days=days)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can create invite links")
            with transaction(conn):
                cursor = conn.execute(
                    "INSERT INTO invite_tokens "
                    "(token_hash, list_id, role, created_by, created_at, "
                    "expires_at, max_uses) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _hash_token(token),
                        list_id,
                        role,
                        user["id"],
                        utcnow(),
                        expires_at,
                        max_uses,
                    ),
                )
                link_id = cursor.lastrowid
                _record(conn, list_id, user["id"], "invite.created", detail=role)

        return json_response(
            {
                "id": link_id,
                "url": f"{settings.public_base_url}/join/{token}",
                "role": role,
                "expires_at": expires_at,
                "uses": 0,
                "max_uses": max_uses,
                "active": True,
            },
            status=201,
        )

    @route("GET", r"/api/lists/(?P<list_id>\d+)/invite-links")
    async def get_invite_links(request: Request) -> Response:
        user = auth.require_user(request)
        list_id = int(request.params["list_id"])
        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can see invite links")
            return json_response({"links": _invite_links(conn, list_id)})

    @route("DELETE", r"/api/lists/(?P<list_id>\d+)/invite-links/(?P<link_id>\d+)")
    async def revoke_invite_link(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])
        link_id = int(request.params["link_id"])

        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can revoke invite links")
            row = conn.execute(
                "SELECT 1 FROM invite_tokens WHERE id = ? AND list_id = ?",
                (link_id, list_id),
            ).fetchone()
            if row is None:
                raise HTTPError(404, "Invite link not found")
            with transaction(conn):
                conn.execute(
                    "UPDATE invite_tokens SET revoked_at = ? WHERE id = ?",
                    (utcnow(), link_id),
                )
                _touch(conn, list_id)
                _record(conn, list_id, user["id"], "invite.revoked")
        return no_content()

    @route("PATCH", r"/api/lists/(?P<list_id>\d+)/members/(?P<user_id>\d+)")
    async def update_member(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])
        target = int(request.params["user_id"])
        data = _body(request)

        role = data.get("role")
        if role not in {"editor", "viewer"}:
            raise HTTPError(422, "role must be 'editor' or 'viewer'")

        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can change roles")
            owner = conn.execute(
                "SELECT user_id FROM lists WHERE id = ?", (list_id,)
            ).fetchone()
            if owner is None:
                raise HTTPError(404, "List not found")
            if owner["user_id"] == target:
                raise HTTPError(400, "The owner's role cannot be changed")
            share = conn.execute(
                "SELECT 1 FROM list_shares WHERE list_id = ? AND user_id = ?",
                (list_id, target),
            ).fetchone()
            if share is None:
                raise HTTPError(404, "That person is not a member")
            with transaction(conn):
                conn.execute(
                    "UPDATE list_shares SET role = ? WHERE list_id = ? AND user_id = ?",
                    (role, list_id, target),
                )
                _touch(conn, list_id)
                _record(conn, list_id, user["id"], "member.role", detail=role)
        return json_response({"success": True})

    @route("DELETE", r"/api/lists/(?P<list_id>\d+)/members/(?P<user_id>\d+)")
    async def remove_member(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])
        target = int(request.params["user_id"])

        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can remove members")
            owner = conn.execute(
                "SELECT user_id FROM lists WHERE id = ?", (list_id,)
            ).fetchone()
            if owner is None:
                raise HTTPError(404, "List not found")
            if owner["user_id"] == target:
                raise HTTPError(400, "The owner cannot be removed")
            share = conn.execute(
                "SELECT 1 FROM list_shares WHERE list_id = ? AND user_id = ?",
                (list_id, target),
            ).fetchone()
            if share is None:
                raise HTTPError(404, "That person is not a member")

            person = conn.execute(
                "SELECT name FROM users WHERE id = ?", (target,)
            ).fetchone()
            with transaction(conn):
                conn.execute(
                    "DELETE FROM list_shares WHERE list_id = ? AND user_id = ?",
                    (list_id, target),
                )
                # Sticky removal: a link that is still circulating must not be
                # a way back in.
                conn.execute(
                    "INSERT OR REPLACE INTO list_blocks "
                    "(list_id, user_id, blocked_by, created_at) VALUES (?, ?, ?, ?)",
                    (list_id, target, user["id"], utcnow()),
                )
                _touch(conn, list_id)
                _record(
                    conn,
                    list_id,
                    user["id"],
                    "member.removed",
                    detail=person["name"] if person else None,
                )
        return no_content()

    @route("DELETE", r"/api/lists/(?P<list_id>\d+)/blocks/(?P<user_id>\d+)")
    async def unblock_member(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        list_id = int(request.params["list_id"])
        target = int(request.params["user_id"])

        with connection() as conn:
            if _require(conn, list_id, user["id"]) != "owner":
                raise HTTPError(403, "Only the owner can unblock people")
            row = conn.execute(
                "SELECT 1 FROM list_blocks WHERE list_id = ? AND user_id = ?",
                (list_id, target),
            ).fetchone()
            if row is None:
                raise HTTPError(404, "That person is not blocked")
            person = conn.execute(
                "SELECT name FROM users WHERE id = ?", (target,)
            ).fetchone()
            with transaction(conn):
                conn.execute(
                    "DELETE FROM list_blocks WHERE list_id = ? AND user_id = ?",
                    (list_id, target),
                )
                _touch(conn, list_id)
                _record(
                    conn,
                    list_id,
                    user["id"],
                    "member.unblocked",
                    detail=person["name"] if person else None,
                )
        return no_content()

    @route("GET", r"/join/(?P<token>[A-Za-z0-9_-]{20,64})")
    async def join(request: Request) -> Response:
        token = request.params["token"]
        user = auth.current_user(request)
        if user is None:
            # Sign in first, then come back here. `next` is carried in the
            # signed OAuth state cookie, so it cannot be tampered with.
            return redirect(f"/auth/login?next=/join/{token}", status=302)

        with connection() as conn:
            list_id = _claim_invite(conn, token, user["id"])
        return redirect(f"/#/list/{list_id}")

    # -- export / import --------------------------------------------------

    @route("GET", r"/api/lists/(?P<list_id>\d+)/export")
    async def export_list(request: Request) -> Response:
        user = auth.require_user(request)
        list_id = int(request.params["list_id"])

        with connection() as conn:
            _require(conn, list_id, user["id"])
            row = conn.execute(
                "SELECT title, is_resettable, reset_hour, reset_tz "
                "FROM lists WHERE id = ?",
                (list_id,),
            ).fetchone()
            tasks = _tasks(conn, list_id)

        payload = {
            "format": "checklist-v2",
            "exported_at": utcnow(),
            "list": {
                "title": row["title"],
                "is_resettable": bool(row["is_resettable"]),
                "reset_hour": row["reset_hour"],
                "reset_tz": row["reset_tz"],
            },
            "tasks": [
                {
                    "title": task["title"],
                    "status": task["status"],
                    "position": task["position"],
                    "created_at": task["created_at"],
                    "completed_at": task["completed_at"],
                }
                for task in tasks
            ],
        }
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", row["title"]).strip("-")
        filename = (safe_name or "checklist") + ".json"
        return json_response(
            payload,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @route("POST", r"/api/import")
    async def import_list(request: Request) -> Response:
        user = auth.require_user(request)
        auth.require_csrf(request)
        data = _body(request)

        source = data.get("list")
        if not isinstance(source, dict):
            raise HTTPError(422, "list must be an object")
        title = _text(source, "title", MAX_LIST_TITLE)
        resettable = source.get("is_resettable") is True

        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            raise HTTPError(422, "tasks must be a list")
        if len(tasks) > 1000:
            raise HTTPError(422, "An import is limited to 1000 items")

        with connection() as conn:
            with transaction(conn):
                cursor = conn.execute(
                    "INSERT INTO lists "
                    "(user_id, title, is_resettable, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user["id"], title, 1 if resettable else 0, utcnow(), utcnow()),
                )
                list_id = cursor.lastrowid

                for index, task in enumerate(tasks, start=1):
                    if not isinstance(task, dict):
                        raise HTTPError(422, "Every task must be an object")
                    task_title = _text(task, "title", MAX_TASK_TITLE)
                    status = task.get("status")
                    if status not in {"pending", "completed"}:
                        status = "pending"
                    conn.execute(
                        "INSERT INTO tasks "
                        "(list_id, title, status, position, created_by, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            list_id,
                            task_title,
                            status,
                            index * POSITION_STEP,
                            user["id"],
                            utcnow(),
                            utcnow(),
                        ),
                    )

                _record(conn, list_id, user["id"], "list.imported", detail=title)
            result = _fetch_list(conn, list_id, user["id"])

        return json_response({"list": result, "imported": len(tasks)}, status=201)

    @route("GET", r"/api/lists/(?P<list_id>\d+)/state")
    async def get_state(request: Request) -> Response:
        """Everything the workspace needs, or 304 if nothing changed.

        `since` is the version the caller already has. A 304 carries no body,
        which is the point: polling a shared list costs one integer comparison
        and an empty response when nobody has touched it.
        """
        user = auth.require_user(request)
        list_id = int(request.params["list_id"])

        since_raw = request.query.get("since", [""])[0].strip()
        try:
            since = int(since_raw) if since_raw else 0
        except ValueError:
            since = 0

        client_id = request.header("x-client-id").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", client_id):
            client_id = ""

        with connection() as conn:
            role = _require(conn, list_id, user["id"])
            _apply_due_reset(conn, list_id)
            version = _version(conn, list_id)
            etag = f'W/"{version}"'

            # Touch presence even when returning 304: the caller is still
            # looking, which is exactly what presence is for.
            if client_id:
                presence.touch(
                    list_id, client_id, user["id"], user["name"] or user["email"]
                )

            if (since and since == version) or request.header("if-none-match") == etag:
                return Response(304, headers={"ETag": etag})

            payload = {
                "version": version,
                "list": _fetch_list(conn, list_id, user["id"]),
                "tasks": _tasks(conn, list_id),
                "members": _members(conn, list_id, include_email=(role == "owner")),
                "activity": _activity(conn, list_id),
                "presence": presence.snapshot(list_id),
                "server_time": utcnow(),
            }
            if role == "owner":
                payload["invite_links"] = _invite_links(conn, list_id)
                payload["blocks"] = _blocks(conn, list_id)
        return json_response(payload, headers={"ETag": etag})
