"""SQLite access and schema migrations.

The schema is versioned with ``PRAGMA user_version`` instead of the previous
"try an ALTER and catch the error" approach, so a deploy can tell exactly which
migrations have run. Every migration is additive: an older binary can still
open a newer database, which is what makes a rollback safe.

The existing production database is a valid starting point. Version 1 describes
what is already there; versions 2 and 3 add the v2 columns and backfill them.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from config import settings

DB_FILE = str(settings.database_path)
SCHEMA_VERSION = 3


def connect() -> sqlite3.Connection:
    """Open a connection with the pragmas this app depends on.

    Foreign keys are off by default in SQLite, so without the pragma every
    ON DELETE CASCADE is a silent no-op. WAL lets readers continue while a
    write is in progress; busy_timeout turns lock contention into a short wait
    instead of an immediate "database is locked".
    """
    conn = sqlite3.connect(DB_FILE, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run several statements atomically.

    Connections use autocommit, so a multi-statement mutation (insert then bump
    the version, or renumber every position) needs an explicit transaction.
    BEGIN IMMEDIATE takes the write lock up front, which turns a concurrent
    writer into a short wait instead of a failure halfway through.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------

BASE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT,
        google_sub TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        is_resettable BOOLEAN DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS list_shares (
        list_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY(list_id, user_id),
        FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS list_invites (
        list_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(list_id, email),
        FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        status TEXT CHECK(status IN ('pending', 'completed')) DEFAULT 'pending',
        position INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE
    )
    """,
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    """Add a column if it is missing.

    SQLite has no ADD COLUMN IF NOT EXISTS, and an existing deployment may have
    picked up some columns by hand, so the check is explicit. Table and column
    names are literals in this module, never user input.
    """
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_1(conn: sqlite3.Connection) -> None:
    """The schema as it stood before v2, made safe to re-run."""
    for statement in BASE_SCHEMA:
        conn.execute(statement)

    # SQLite compares TEXT with BINARY collation, so 'John@x.com' and
    # 'john@x.com' are different values to the UNIQUE constraint. Rows written
    # before emails were normalised would miss a case-insensitive lookup and
    # produce a second, empty account for a returning user.
    for row in conn.execute(
        "SELECT id, LOWER(email) FROM users WHERE email != LOWER(email)"
    ).fetchall():
        try:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (row[1], row[0]))
        except sqlite3.IntegrityError:
            # Another row already holds the lowercase form. Merging would mean
            # guessing which one owns the lists, so leave both alone.
            pass

    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_nocase "
            "ON users(LOWER(email))"
        )
    except sqlite3.IntegrityError:
        pass

    _add_column(conn, "users", "google_sub", "TEXT")
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub "
            "ON users(google_sub) WHERE google_sub IS NOT NULL"
        )
    except sqlite3.IntegrityError:
        pass


def _migrate_2(conn: sqlite3.Connection) -> None:
    """Columns and tables introduced by v2."""
    _add_column(conn, "lists", "version", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "lists", "updated_at", "TEXT")
    _add_column(conn, "lists", "reset_hour", "INTEGER")
    _add_column(conn, "lists", "reset_tz", "TEXT")
    _add_column(conn, "lists", "next_reset_at", "TEXT")

    _add_column(conn, "tasks", "created_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL")
    _add_column(conn, "tasks", "completed_at", "TEXT")
    _add_column(conn, "tasks", "completed_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL")
    _add_column(conn, "tasks", "updated_at", "TEXT")
    _add_column(conn, "tasks", "deleted_at", "TEXT")

    _add_column(conn, "list_shares", "role", "TEXT NOT NULL DEFAULT 'editor'")
    _add_column(conn, "list_shares", "created_at", "TEXT")
    _add_column(conn, "list_shares", "joined_at", "TEXT")
    _add_column(conn, "list_shares", "via_token_id", "INTEGER")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invite_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            list_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'editor',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            uses INTEGER NOT NULL DEFAULT 0,
            max_uses INTEGER NOT NULL DEFAULT 1,
            revoked_at TEXT,
            FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE,
            FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_invite_tokens_list ON invite_tokens(list_id)"
    )

    # Removal has to be sticky: without this, someone removed while an invite
    # link is still circulating could simply open it again.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS list_blocks (
            list_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            blocked_by INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (list_id, user_id),
            FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL,
            user_id INTEGER,
            action TEXT NOT NULL,
            task_id INTEGER,
            detail TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_list ON activity(list_id, id DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency (
            key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            status INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_list_position "
        "ON tasks(list_id, deleted_at, position)"
    )


def _migrate_3(conn: sqlite3.Connection) -> None:
    """Backfill values the new columns need to be meaningful.

    Positions: the old code inserted every task at position 0 and broke ties
    with ``id DESC``. Rewriting them as 1024, 2048, ... in the order the app
    actually displayed them preserves what users see and leaves room to insert
    between two rows without renumbering everything.
    """
    list_ids = [row[0] for row in conn.execute("SELECT id FROM lists")]
    for list_id in list_ids:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE list_id = ? ORDER BY position ASC, id DESC",
            (list_id,),
        ).fetchall()
        for index, (task_id,) in enumerate(rows, start=1):
            conn.execute(
                "UPDATE tasks SET position = ? WHERE id = ?", (index * 1024, task_id)
            )

    conn.execute("UPDATE lists SET updated_at = COALESCE(updated_at, created_at)")
    conn.execute("UPDATE tasks SET updated_at = COALESCE(updated_at, created_at)")


MIGRATIONS = {1: _migrate_1, 2: _migrate_2, 3: _migrate_3}


def init_db() -> int:
    """Apply pending migrations and return the resulting schema version.

    Idempotent, so it is safe to call on every start. Each migration runs in
    its own transaction; a failure rolls that one back and leaves the recorded
    version untouched, so the next start retries rather than half-applying it.
    """
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)

    conn = connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for target in sorted(MIGRATIONS):
            if version >= target:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[target](conn)
                # PRAGMA cannot be parameterised, and target is a literal from
                # this module's own dict.
                conn.execute(f"PRAGMA user_version = {int(target)}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            version = target
        return version
    finally:
        conn.close()


if __name__ == "__main__":
    print(f"schema version {init_db()}")
