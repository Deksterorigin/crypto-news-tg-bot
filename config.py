import os
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables from .env file
load_dotenv(dotenv_path=BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
POST_LANGUAGE = os.getenv("POST_LANGUAGE", "uk")
OWNER_ID = os.getenv("OWNER_ID")

# Ensure required configurations are present
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in the .env file.")
if not CHANNEL_ID:
    raise ValueError("TELEGRAM_CHANNEL_ID is missing in the .env file.")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in the .env file.")

# Local sqlite db path
DB_PATH = BASE_DIR / "data.db"

import requests
import logging

def requests_get_with_retry(url: str, headers: dict = None, timeout: int = 5) -> requests.Response:
    """Attempts requests.get with proxy rotation, falling back to direct connection on failure."""
    try:
        from db import get_setting
        proxies_str = get_setting("proxies", "").strip()
    except Exception as e:
        # Fallback if DB is not initialized yet during initial imports
        proxies_str = ""
        
    if not proxies_str:
        return requests.get(url, headers=headers, timeout=timeout)
        
    # Split by comma or newline
    proxies_list = [p.strip() for p in proxies_str.replace("\n", ",").split(",") if p.strip()]
    if not proxies_list:
        return requests.get(url, headers=headers, timeout=timeout)
        
    import random
    random.shuffle(proxies_list)
    
    for proxy in proxies_list:
        try:
            logging.info(f"Attempting request to {url} using proxy: {proxy}")
            response = requests.get(
                url,
                headers=headers,
                proxies={"http": proxy, "https": proxy},
                timeout=timeout
            )
            return response
        except Exception as e:
            logging.warning(f"Proxy {proxy} failed for {url}: {e}. Trying next proxy...")
            
    # Final fallback: direct connection
    logging.info(f"All proxies failed. Falling back to direct connection for {url}")
    return requests.get(url, headers=headers, timeout=timeout)

def get_berlin_now() -> datetime:
    """Returns the current naive datetime in Europe/Berlin timezone."""
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
    except Exception:
        # Fallback to local time in case of zoneinfo issues
        from datetime import datetime as dt
        return dt.now()
