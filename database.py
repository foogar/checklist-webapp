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
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
