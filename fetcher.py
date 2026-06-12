import feedparser
import logging
import calendar
import time
import re
from datetime import datetime
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from db import is_already_published, get_setting
from config import requests_get_with_retry, get_berlin_now

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
    """Fetches and parses a single RSS feed or scrapes custom HTML source, returning new, unposted items."""
    logging.info(f"Fetching feed: {source_name} ({feed_url})")
    items = []
    
    try:
        # Fetch blacklist words
        blacklist_str = get_setting("blacklist_words", "")
        blacklist = [w.strip().lower() for w in blacklist_str.split(",") if w.strip()]
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        # 1. Custom Binance Scraper / RSSHub Fallback
        if "binance" in feed_url.lower():
            binance_urls = [
                "https://rsshub.app/binance/announcement",
                "https://rsshub.app/binance/announcement/en",
                "https://rss.feeded.xyz/binance/announcement",
            ]
            parsed_feed = None
            for b_url in binance_urls:
                try:
                    res = requests_get_with_retry(b_url, headers=headers, timeout=5)
                    if res.status_code == 200:
                        parsed_feed = feedparser.parse(res.content)
                        if parsed_feed.entries:
                            logging.info(f"Successfully fetched Binance feed from RSSHub mirror: {b_url}")
                            break
                except Exception as e:
                    logging.warning(f"Failed to fetch Binance feed from {b_url}: {e}")
                    
            if parsed_feed and parsed_feed.entries:
                for entry in parsed_feed.entries:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    summary = entry.get("summary", entry.get("description", "")).strip()
                    
                    title_lower = title.lower()
                    if any(word in title_lower for word in blacklist):
                        continue
                    if is_already_published(link):
                        continue
                        
                    # Check age: news must not be older than 24 hours (86,400 seconds)
                    pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub_parsed:
                        try:
                            pub_ts = calendar.timegm(pub_parsed)
                            if time.time() - pub_ts > 86400:
                                logging.info(f"Skipping old Binance entry: '{title}' (age: {int((time.time() - pub_ts)/3600)}h)")
                                continue
                        except Exception as e:
                            logging.warning(f"Error checking age for Binance entry {title}: {e}")
                            
                    items.append({
                        "title": title,
                        "link": link,
                        "summary": clean_html(summary),
                        "source": source_name
                    })
            return items
            
        # 2. Fetch HTML/content for other URLs
        response = requests_get_with_retry(feed_url, headers=headers, timeout=5)
        if response.status_code != 200:
            logging.error(f"Failed to fetch feed {source_name} from {feed_url}, status code: {response.status_code}")
            return []
            
        # 3. Custom Airdrops.io Scraper
        if "airdrops.io" in feed_url.lower():
            soup = BeautifulSoup(response.text, "html.parser")
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith("https://airdrops.io/") and href != "https://airdrops.io/":
                    skip = ['/speculative/', '/hot/', '/potential/', '/about/', '/contact/', '/category/', '/tag/', '/stay-safe/']
                    if not any(k in href for k in skip):
                        title = a.get_text(strip=True)
                        if title and len(title) > 2:
                            links.append((title, href))
            seen = set()
            unique_links = []
            for title, href in links:
                if href not in seen:
                    seen.add(href)
                    unique_links.append((title, href))
            for title, href in unique_links:
                title_lower = title.lower()
                if any(word in title_lower for word in blacklist):
                    continue
                if is_already_published(href):
                    continue
                items.append({
                    "title": f"Airdrop: {title}",
                    "link": href,
                    "summary": f"Нова роздача (Airdrop) від проекту {title}. Дізнайтеся деталі та кроки для участі на сторінці проекту.",
                    "source": source_name
                })
            return items
            
        # 4. Custom CryptoRank Drop Hunting Scraper
        elif "cryptorank.io/drophunting" in feed_url.lower():
            soup = BeautifulSoup(response.text, "html.parser")
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if "/drophunting/" in href and href != "/drophunting":
                    full_url = href
                    if href.startswith("/"):
                        full_url = "https://cryptorank.io" + href
                    title = a.get_text(strip=True)
                    title = title.replace("New", "").strip()
                    if title and len(title) > 2:
                        links.append((title, full_url))
            seen = set()
            unique_links = []
            for title, href in links:
                if href not in seen:
                    seen.add(href)
                    unique_links.append((title, href))
            for title, href in unique_links:
                title_lower = title.lower()
                if any(word in title_lower for word in blacklist):
                    continue
                if is_already_published(href):
                    continue
                items.append({
                    "title": f"CryptoRank Drop: {title}",
                    "link": href,
                    "summary": f"Нова активність та airdrop-кампанія {title} додана на CryptoRank. Інвестиції, дедлайни та гайд щодо проходження.",
                    "source": source_name
                })
            return items
            
        # 5. Custom Bybit Announcements Scraper
        elif "bybit" in feed_url.lower() or "bybit.com" in feed_url.lower():
            soup = BeautifulSoup(response.text, "html.parser")
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if "/article/" in href or "article" in href:
                    full_url = href
                    if href.startswith("/"):
                        full_url = "https://announcements.bybit.com" + href
                    
                    full_text = a.get_text(strip=True)
                    
                    # Extract date using regex (e.g. "Jun 11, 2026")
                    date_match = re.search(r'([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})', full_text)
                    is_old = False
                    if date_match:
                        date_str = date_match.group(0)
                        try:
                            ann_date = datetime.strptime(date_str, "%b %d, %Y").date()
                            berlin_today = get_berlin_now().date()
                            if (berlin_today - ann_date).days > 1:
                                logging.info(f"Skipping old Bybit article: '{full_text[:50]}...' (date: {date_str})")
                                is_old = True
                        except Exception as de:
                            logging.warning(f"Error parsing Bybit date {date_str}: {de}")
                            
                    if is_old:
                        continue
                        
                    title = full_text
                    if "lg..." in title:
                        title = title.split("lg...")[0].strip()
                    if title and len(title) > 2:
                        links.append((title, full_url))
            seen = set()
            unique_links = []
            for title, href in links:
                if href not in seen:
                    seen.add(href)
                    unique_links.append((title, href))
            for title, href in unique_links:
                title_lower = title.lower()
                if any(word in title_lower for word in blacklist):
                    continue
                if is_already_published(href):
                    continue
                items.append({
                    "title": title,
                    "link": href,
                    "summary": f"Анонс події/лістингу/промо-акції від біржі Bybit: {title}.",
                    "source": source_name
                })
            return items
            
        # 6. Standard RSS Feed Parsing
        feed = feedparser.parse(response.content)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            
            if not title or not link:
                continue
                
            title_lower = title.lower()
            if any(word in title_lower for word in blacklist):
                logging.info(f"Skipping article due to blacklist match: '{title}'")
                continue
                
            if is_already_published(link):
                continue
                
            # Check age: news must not be older than 24 hours (86,400 seconds)
            pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub_parsed:
                try:
                    pub_ts = calendar.timegm(pub_parsed)
                    if time.time() - pub_ts > 86400:
                        logging.info(f"Skipping old RSS entry: '{title}' (age: {int((time.time() - pub_ts)/3600)}h)")
                        continue
                except Exception as e:
                    logging.warning(f"Error checking age for entry {title}: {e}")
                    
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
