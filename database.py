import sqlite3
import os

DB_FILE = "checklist.db"

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
