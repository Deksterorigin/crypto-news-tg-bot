import sqlite3
import os
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
