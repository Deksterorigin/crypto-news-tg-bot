import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables from .env file
load_dotenv(dotenv_path=BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
POST_LANGUAGE = os.getenv("POST_LANGUAGE", "uk")

# Ensure required configurations are present
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN is missing in the .env file.")
if not CHANNEL_ID:
    raise ValueError("TELEGRAM_CHANNEL_ID is missing in the .env file.")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in the .env file.")

# Local sqlite db path
DB_PATH = BASE_DIR / "data.db"
