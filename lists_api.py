"""Lists, tasks, reordering, and reset.

Access is decided here, on every request, from the database. The frontend
hiding a button is a convenience, never the control.

Every mutation bumps ``lists.version``. Nothing reads that yet; it exists so the
sync layer in the next milestone can answer "has this list changed since N?"
with a single integer instead of comparing rows.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import auth
import presence
from database import connection, transaction
from util import utcnow
from web import HTTPError, Request, Response, json_response, no_content

MAX_LIST_TITLE = 200
MAX_TASK_TITLE = 500
POSITION_STEP = 1024

LIST_COLUMNS = """
    l.id, l.title, l.is_resettable, l.version, l.created_at, l.user_id,
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
# Routes
# --------------------------------------------------------------------------


def register(route) -> None:
    @route("GET", r"/api/lists")
    async def get_lists(request: Request) -> Response:
        user = auth.require_user(request)
        with connection() as conn:
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
        return json_response(payload, headers={"ETag": etag})
