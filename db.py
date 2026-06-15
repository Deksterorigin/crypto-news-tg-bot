import sqlite3
import os
from datetime import datetime
from config import DB_PATH, os as config_os, CHANNEL_ID as DEFAULT_CHANNEL_ID, get_berlin_now

def get_connection():
    """Returns a connection to the SQLite database with WAL mode and timeout set."""
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
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
        
        # Create channels table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                name TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create channel_stats table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_stats (
                channel_id TEXT,
                date TEXT,
                member_count INTEGER,
                PRIMARY KEY (channel_id, date)
            )
        """)
        
        # Create daily_schedule table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_time TEXT,
                post_type TEXT,
                is_executed INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        
        # Migration: Check if was_posted column exists in published_posts table, if not add it
        try:
            conn.execute("ALTER TABLE published_posts ADD COLUMN was_posted INTEGER DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            # Column already exists
            pass

    # Populate default feeds if empty or missing
    default_feeds = [
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt", "https://decrypt.co/feed"),
        ("AirdropAlert", "https://airdropalert.com/feed/rssfeed"),
        ("Vitalik's Blog", "https://vitalik.ca/feed/"),
        ("a16z Crypto", "https://a16zcrypto.substack.com/feed"),
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"),
        ("Blockworks", "https://blockworks.co/feed"),
        ("CryptoBriefing", "https://cryptobriefing.com/feed/"),
        ("NewsBTC", "https://www.newsbtc.com/feed/"),
        ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed"),
        ("CryptoRank DropHunting", "https://cryptorank.io/drophunting"),
        ("Airdrops.io", "https://airdrops.io/"),
        ("Binance Blog", "https://www.binance.com/en/blog"),
        ("Bybit Blog", "https://announcements.bybit.com/en-US/"),
        ("Coinbase Blog", "https://medium.com/feed/@coinbase")
    ]
    
    with get_connection() as conn:
        for name, url in default_feeds:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM rss_feeds WHERE url = ?", (url,))
            if cursor.fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO rss_feeds (name, url) VALUES (?, ?)",
                    (name, url)
                )
        conn.commit()
            
    # Populate default channel if empty
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM channels")
        if cursor.fetchone()[0] == 0 and DEFAULT_CHANNEL_ID:
            conn.execute(
                "INSERT INTO channels (channel_id, name) VALUES (?, ?)",
                (str(DEFAULT_CHANNEL_ID), "Основний канал")
            )
            conn.commit()

    # Populate default settings if missing
    default_settings = [
        ("blacklist_words", "presale, pre-sale, 10000%, 1000x, scam, скандал"),
        ("breaking_keywords", "massive, hack, halving, sec, approved, exploit, bankrupt, liquidation"),
        ("proxies", "")
    ]
    with get_connection() as conn:
        for key, val in default_settings:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM settings WHERE key = ?", (key,))
            if cursor.fetchone()[0] == 0:
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val))
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

# --- GENERIC SETTINGS DYNAMIC MANAGEMENT ---

def get_setting(key: str, default: str = "") -> str:
    """Gets a setting value from SQLite by key."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else default

def set_setting(key: str, value: str):
    """Saves or updates a setting key-value pair in SQLite."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )
        conn.commit()

# --- CHANNELS DYNAMIC MANAGEMENT ---

def get_channels() -> list:
    """Returns a list of all Telegram channels where the bot posts."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, name, added_at FROM channels")
        return [dict(row) for row in cursor.fetchall()]

def add_channel(channel_id: str, name: str) -> bool:
    """Adds a target Telegram channel. Returns True if successful."""
    try:
        # Standardize channel ID (usually starts with -100)
        ch_id = channel_id.strip()
        if not ch_id.startswith("-") and not ch_id.startswith("@"):
            ch_id = "-100" + ch_id
            
        with get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO channels (channel_id, name) VALUES (?, ?)",
                (ch_id, name)
            )
            conn.commit()
        return True
    except Exception:
        return False

def delete_channel(channel_id: str) -> bool:
    """Removes a target Telegram channel. Returns True if successful."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id.strip(),))
            conn.commit()
            return cursor.rowcount > 0
    except Exception:
        return False

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

def mark_as_published(url: str, title: str, source: str, was_posted: int = 0):
    """Marks a URL as processed, optionally specifying if it was actually posted."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO published_posts (url, title, source, was_posted) VALUES (?, ?, ?, ?)",
            (url, title, source, was_posted)
        )
        conn.commit()

def get_recent_posted_titles(days: int = 7) -> list:
    """Returns a list of titles of posts that were actually published (was_posted=1) in the last N days."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT title FROM published_posts WHERE was_posted = 1 AND published_at >= datetime('now', ?)",
            (f"-{days} days",)
        )
        return [row[0] for row in cursor.fetchall() if row[0]]

def record_channel_stats(channel_id: str, member_count: int):
    """Records the subscriber count for a channel for the current day."""
    today = get_berlin_now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO channel_stats (channel_id, date, member_count) VALUES (?, ?, ?)",
            (channel_id, today, member_count)
        )
        conn.commit()

def get_channel_analytics(channel_id: str) -> dict:
    """Returns analytics data (current subscribers, growth over 7 days, etc.)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT date, member_count FROM channel_stats WHERE channel_id = ? ORDER BY date DESC LIMIT 8",
            (channel_id,)
        )
        rows = cursor.fetchall()
        
        if not rows:
            return {"current": 0, "growth_7d": 0, "history": []}
            
        current = rows[0]["member_count"]
        oldest = rows[-1]["member_count"]
        growth_7d = current - oldest
        
        return {
            "current": current,
            "growth_7d": growth_7d,
            "history": [dict(r) for r in rows]
        }

# Automatically initialize database when the module is imported
init_db()
