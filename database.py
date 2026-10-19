import sqlite3
import os
from pathlib import Path

# Project root, derived from this file's location. Every other path in the app
# is anchored here rather than to the process working directory, so the app
# finds its database, templates, and static assets no matter where it was
# launched from. A relative working directory is the normal state under a
# service manager, a container, or an IDE, and it silently produced a second
# empty database before this was made absolute.
BASE_DIR = Path(__file__).resolve().parent


def _resolve_db_path() -> Path:
    """Absolute path to the SQLite file.

    Defaults to checklist.db beside this module. Set DATABASE_PATH to place it
    elsewhere, which is what you want for a mounted volume or a backup-friendly
    directory on a real deployment. A relative override is resolved against
    BASE_DIR so it means the same thing regardless of the working directory.
    """
    configured = os.getenv("DATABASE_PATH", "").strip()
    path = Path(configured).expanduser() if configured else Path("checklist.db")
    if not path.is_absolute():
        path = BASE_DIR / path
    # Only needed for an override that points somewhere new; the default's
    # parent always exists. Without this, SQLite reports the unhelpful
    # "unable to open database file" for a missing directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


DB_FILE = str(_resolve_db_path())

def connect():
    """Open a connection with the pragmas this app depends on.

    SQLite ignores foreign keys unless they are enabled per connection, so
    without the PRAGMA below every ON DELETE CASCADE in the schema is a
    silent no-op and deleting a list leaves orphaned tasks and shares.
    """
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    conn = connect()
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Added is_resettable flag for manual daily list behavior
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        is_resettable BOOLEAN DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS list_shares (
        list_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY(list_id, user_id),
        FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Shares for people who have not signed up yet. Holding an email rather
    # than a user_id means any address can be invited without revealing whether
    # it already has an account. Invites are claimed into list_shares the first
    # time that email signs in, so the access-control queries only ever need to
    # consider list_shares. Emails are stored lowercased for matching.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS list_invites (
        list_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(list_id, email),
        FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        list_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        status TEXT CHECK(status IN ('pending', 'completed')) DEFAULT 'pending',
        position INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(list_id) REFERENCES lists(id) ON DELETE CASCADE
    );
    """)
    
    # SQLite compares TEXT with BINARY collation, so 'John@x.com' and
    # 'john@x.com' are two different values as far as the UNIQUE constraint on
    # users.email is concerned. Rows written before emails were normalised would
    # therefore miss a case-insensitive lookup and produce a second, empty
    # account for a returning user. Fold them to lowercase once, on startup.
    cursor.execute("SELECT id, LOWER(email) FROM users WHERE email != LOWER(email)")
    for row in cursor.fetchall():
        try:
            cursor.execute("UPDATE users SET email = ? WHERE id = ?", (row[1], row[0]))
        except sqlite3.IntegrityError:
            # Another row already holds the lowercase form. Merging them would
            # mean guessing which one owns the lists, so leave both alone; the
            # case-insensitive lookups still find the original.
            pass

    # Belt and braces against a case-variant duplicate appearing later. Skipped
    # if genuine duplicates already exist, since the index cannot be built then.
    try:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_nocase ON users(LOWER(email))"
        )
    except sqlite3.IntegrityError:
        pass

    # users.google_sub is the stable identity from Google. The email is not:
    # a Workspace administrator can rename a user, which changes the address
    # but never the sub, and keying on email would hand them a new empty
    # account and orphan everything they owned. Added as an ALTER rather than
    # in the CREATE above so existing databases pick it up. SQLite has no
    # ADD COLUMN IF NOT EXISTS, so the check has to be explicit.
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
    if "google_sub" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")

    # A UNIQUE index permits many NULLs, so accounts that have not logged in
    # since this change are all still valid here and get backfilled one at a
    # time. Skipped if two rows already share a sub, which would mean a
    # duplicate we should surface rather than paper over.
    try:
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub "
            "ON users(google_sub) WHERE google_sub IS NOT NULL"
        )
    except sqlite3.IntegrityError:
        pass

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
