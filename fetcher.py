import feedparser
import logging
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from db import is_already_published, get_setting
from config import requests_get_with_retry

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Feeds are loaded dynamically from the SQLite database

def extract_image_url(article_url: str) -> str:
    """Scrapes the article page to find the Open Graph og:image URL."""
    try:
        # Ignore non-http links
        if not article_url.startswith("http"):
            return None
            
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        response = requests_get_with_retry(article_url, headers=headers, timeout=5)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            # Try to get Open Graph image tag
            og_image = soup.find("meta", property="og:image")
            if og_image and og_image.get("content"):
                return og_image["content"].strip()
                
            # Fallback: check twitter:image
            twitter_image = soup.find("meta", name="twitter:image")
            if twitter_image and twitter_image.get("content"):
                return twitter_image["content"].strip()
                
    except Exception as e:
        logging.error(f"Error extracting image from {article_url}: {e}")
    return None

def clean_html(raw_html: str) -> str:
    """Helper to remove HTML tags and clean up text."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    # Get text and clean spaces
    text = soup.get_text(separator=" ")
    return " ".join(text.split())

def fetch_feed(source_name: str, feed_url: str) -> List[Dict[str, Any]]:
    """Fetches and parses a single RSS feed, returning new, unposted items."""
    logging.info(f"Fetching feed: {source_name} ({feed_url})")
    items = []
    
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests_get_with_retry(feed_url, headers=headers, timeout=5)
        
        if response.status_code != 200:
            logging.error(f"Failed to fetch feed {source_name} from {feed_url}, status code: {response.status_code}")
            return []
            
        feed = feedparser.parse(response.content)
        
        # Fetch blacklist words
        blacklist_str = get_setting("blacklist_words", "")
        blacklist = [w.strip().lower() for w in blacklist_str.split(",") if w.strip()]
        
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            
            # Basic validation
            if not title or not link:
                continue
                
            # Content Filter (Blacklist)
            title_lower = title.lower()
            if any(word in title_lower for word in blacklist):
                logging.info(f"Skipping article due to blacklist match: '{title}'")
                continue
                
            # Check if already published
            if is_already_published(link):
                continue
                
            items.append({
                "title": title,
                "link": link,
                "summary": clean_html(summary),
                "source": source_name
            })
            
    except Exception as e:
        logging.error(f"Error fetching feed {source_name}: {e}")
        
    logging.info(f"Found {len(items)} new items from {source_name}")
    return items

def fetch_all_new_items() -> List[Dict[str, Any]]:
    """Fetches all new items from all database-configured RSS feeds."""
    from db import get_rss_feeds
    feeds = get_rss_feeds()
    all_items = []
    for feed in feeds:
        all_items.extend(fetch_feed(feed["name"], feed["url"]))
    return all_items

if __name__ == "__main__":
    # Test script execution
    print("Fetching new items...")
    new_items = fetch_all_new_items()
    print(f"Total new items found: {len(new_items)}")
    for i, item in enumerate(new_items[:5], 1):
        print(f"\n{i}. [{item['source']}] {item['title']}\n   Link: {item['link']}\n   Summary snippet: {item['summary'][:150]}...")
