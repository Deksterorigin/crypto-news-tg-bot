import sqlite3
import os
from datetime import datetime
from config import DB_PATH, os as config_os

def get_connection():
    """Returns a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database schema."""
    with get_connection() as conn:
        # Create published posts table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS published_posts (
                url TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create dynamic RSS feeds table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rss_feeds (
                url TEXT PRIMARY KEY,
                name TEXT
            )
        """)
        
        # Create admins table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create settings table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

    # Populate default feeds if empty
    default_feeds = [
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt", "https://decrypt.co/feed"),
        ("AirdropAlert", "https://airdropalert.com/feed/rssfeed"),
        ("Vitalik's Blog", "https://vitalik.ca/feed/"),
        ("a16z Crypto", "https://a16zcrypto.substack.com/feed")
    ]
    
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM rss_feeds")
        if cursor.fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO rss_feeds (name, url) VALUES (?, ?)",
                default_feeds
            )
            conn.commit()

# --- ADMIN & OWNER MANAGEMENT ---

def get_owner_id() -> int:
    """Gets the owner Telegram ID. Priority: .env variable -> SQLite settings."""
    env_owner = config_os.getenv("OWNER_ID")
    if env_owner:
        try:
            return int(env_owner)
        except ValueError:
            pass
            
    # Fallback to database settings
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = 'owner_id'")
        row = cursor.fetchone()
        if row:
            return int(row[0])
    return None

def set_owner_id(user_id: int):
    """Saves the owner Telegram ID in SQLite settings."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('owner_id', ?)",
            (str(user_id),)
        )
        conn.commit()

def is_admin(user_id: int) -> bool:
    """Checks if a Telegram user is an admin or the owner."""
    owner_id = get_owner_id()
    if owner_id and user_id == owner_id:
        return True
        
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def add_admin(user_id: int, username: str = "") -> bool:
    """Adds a new user ID to the authorized admins list."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO admins (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            conn.commit()
        return True
    except Exception:
        return False

def delete_admin(user_id: int) -> bool:
    """Deletes an admin by user ID."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception:
        return False

def get_admins() -> list:
    """Returns a list of all authorized admins."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, added_at FROM admins")
        return [dict(row) for row in cursor.fetchall()]

# --- FEED & POSTS MANAGEMENT ---

def get_rss_feeds() -> list:
    """Returns a list of all active RSS feeds in the database."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, url FROM rss_feeds")
        return [dict(row) for row in cursor.fetchall()]

def add_rss_feed(name: str, url: str) -> bool:
    """Adds a new RSS feed to the database. Returns True if successful."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rss_feeds (name, url) VALUES (?, ?)",
                (name, url)
            )
            conn.commit()
        return True
    except Exception:
        return False

def delete_rss_feed(url: str) -> bool:
    """Deletes an RSS feed by URL. Returns True if successful."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM rss_feeds WHERE url = ?", (url,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception:
        return False

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
