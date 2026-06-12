import sqlite3
from datetime import datetime
from config import DB_PATH

def get_connection():
    """Returns a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database schema."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS published_posts (
                url TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def is_already_published(url: str) -> bool:
    """Checks if the URL has already been published."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM published_posts WHERE url = ?", (url,))
        return cursor.fetchone() is not None

def mark_as_published(url: str, title: str, source: str):
    """Marks a URL as published to prevent posting it again."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO published_posts (url, title, source) VALUES (?, ?, ?)",
            (url, title, source)
        )
        conn.commit()

# Automatically initialize database when the module is imported
init_db()
