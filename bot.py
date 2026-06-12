import telebot
import time
import random
import logging
import threading
import os
import requests
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, HTTPServer
from config import BOT_TOKEN, CHANNEL_ID
from fetcher import fetch_all_new_items, extract_image_url
from processor import generate_single_post_by_type, generate_market_analysis
from db import mark_as_published, get_connection, add_rss_feed, delete_rss_feed, get_rss_feeds

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Initialize Telegram Bot
bot = telebot.TeleBot(BOT_TOKEN)

# Thread-safe locks and global state
schedule_lock = threading.Lock()
scheduled_news = []          # Datetimes for News posts (6 per day)
scheduled_activities = []    # Datetimes for Activity/Earning posts (4 per day)
scheduled_analysis = []      # Datetime for Market Analysis column (1 per day)
scheduled_date = None        # date object tracking the current day of the schedule

# --- RENDER.COM WEB SERVER & KEEP ALIVE LOGIC ---

class HealthCheckHandler(SimpleHTTPRequestHandler):
    """Minimal HTTP handler to pass Render's health checks."""
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b"Crypto Publisher Bot is running!")
        else:
            self.send_response(404)
            self.end_headers()
            
    def log_message(self, format, *args):
        # Suppress logging of incoming health check pings to keep stdout logs clean
        pass

def run_web_server():
    """Runs a web server to bind to Render's port for health check verification."""
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logging.info(f"Web server started on port {port} for Render health checks.")
    server.serve_forever()

def keep_alive_thread():
    """Periodically pings the Render app's public URL to prevent it from sleeping."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        logging.info("RENDER_EXTERNAL_URL is not set. Self-pinging keep-alive is disabled.")
        return
        
    logging.info(f"Self-pinging keep-alive loop started for: {url}")
    while True:
        try:
            time.sleep(600)  # Ping every 10 minutes
            response = requests.get(url)
            logging.info(f"Self-ping sent to {url}, response status: {response.status_code}")
        except Exception as e:
            logging.error(f"Error in self-ping loop: {e}")

# --- COINGECKO PRICES FETCHER ---

def fetch_coingecko_prices() -> dict:
    """Fetches real-time prices for BTC, ETH, and SOL using CoinGecko's simple price API."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logging.error(f"Error fetching CoinGecko prices: {e}")
    # Return zeroed placeholders in case the free API is rate-limited
    return {
        "bitcoin": {"usd": 0.0, "usd_24h_change": 0.0},
        "ethereum": {"usd": 0.0, "usd_24h_change": 0.0},
        "solana": {"usd": 0.0, "usd_24h_change": 0.0}
    }

# --- SCHEDULER & PUBLISHING LOGIC ---

def generate_daily_schedule():
    """
    Generates 3 independent schedules: 6 News posts, 4 Activity posts,
    and 1 Market Analysis column randomly placed in the 10:00 - 22:00 window.
    """
    global scheduled_news, scheduled_activities, scheduled_analysis, scheduled_date
    with schedule_lock:
        now = datetime.now()
        today = now.date()
        
        # 10:00 AM to 10:00 PM is 12 hours = 720 minutes.
        
        # 1. News Queue: 6 posts (spaced out in 2-hour segments)
        news_times = []
        news_segment = 720.0 / 6
        for i in range(6):
            offset = random.randint(int(i * news_segment), int((i + 1) * news_segment) - 1)
            news_times.append(datetime.combine(today, datetime.min.time()) + timedelta(minutes=600 + offset))
        news_times.sort()
        scheduled_news = news_times
        
        # 2. Activity Queue: 4 posts (spaced out in 3-hour segments)
        activity_times = []
        activity_segment = 720.0 / 4
        for i in range(4):
            offset = random.randint(int(i * activity_segment), int((i + 1) * activity_segment) - 1)
            activity_times.append(datetime.combine(today, datetime.min.time()) + timedelta(minutes=600 + offset))
        activity_times.sort()
        scheduled_activities = activity_times
        
        # 3. Market Analysis Column: 1 post (randomly between 11:00 AM and 1:00 PM)
        # 11:00 AM is 660 mins from midnight, 1:00 PM is 780 mins from midnight
        analysis_offset = random.randint(660, 780)
        scheduled_analysis = [datetime.combine(today, datetime.min.time()) + timedelta(minutes=analysis_offset)]
        
        scheduled_date = today
        
        logging.info(f"Daily schedules generated for {today}:")
        logging.info(f"  News Queue (6): {[t.strftime('%H:%M') for t in scheduled_news]}")
        logging.info(f"  Activity Queue (4): {[t.strftime('%H:%M') for t in scheduled_activities]}")
        logging.info(f"  Analysis Column (1): {[t.strftime('%H:%M') for t in scheduled_analysis]}")

def run_publish_cycle_by_type(post_type: str, test_chat_id=None) -> bool:
    """
    Executes a news or activity publishing cycle.
    """
    logging.info(f"Running publish cycle for type: {post_type}")
    try:
        items = fetch_all_new_items()
        if not items:
            logging.info("No new items found in feeds.")
            if test_chat_id:
                bot.send_message(test_chat_id, "❌ Не знайдено нових матеріалів для сбору.")
            return False
            
        selected_link, post_text = generate_single_post_by_type(items, post_type)
        
        target = test_chat_id if test_chat_id else CHANNEL_ID
        
        if post_text and selected_link:
            img_url = extract_image_url(selected_link)
            
            if img_url:
                try:
                    bot.send_photo(chat_id=target, photo=img_url, caption=post_text, parse_mode="HTML")
                    logging.info(f"Photo post for {post_type} published.")
                except Exception as pe:
                    logging.error(f"Failed to post photo: {pe}. Falling back to text.")
                    bot.send_message(chat_id=target, text=post_text, parse_mode="HTML", disable_web_page_preview=False)
            else:
                bot.send_message(chat_id=target, text=post_text, parse_mode="HTML", disable_web_page_preview=False)
                logging.info(f"Text post for {post_type} published.")
                
            # If not in test mode, mark all fetched items as processed
            if not test_chat_id:
                for item in items:
                    mark_as_published(item["link"], item["title"], item["source"])
            return True
        else:
            logging.info(f"No suitable post of type {post_type} selected.")
            if test_chat_id:
                bot.send_message(test_chat_id, f"⚠️ Gemini відфільтрував усі новини як невідповідні для типу '{post_type}'.")
            
            # Clean items list in SQLite even if skipped to keep feeds fresh
            if not test_chat_id:
                for item in items:
                    mark_as_published(item["link"], item["title"], item["source"])
            return False
            
    except Exception as e:
        logging.error(f"Error in run_publish_cycle_by_type ({post_type}): {e}")
        if test_chat_id:
            bot.send_message(test_chat_id, f"❌ Помилка: {e}")
        return False

def run_market_analysis_cycle(test_chat_id=None) -> bool:
    """
    Executes a market analysis review. Fetches CoinGecko prices,
    gets today's news headlines, and generates an analyst commentary.
    """
    logging.info("Running market analysis cycle...")
    try:
        # Fetch data
        prices = fetch_coingecko_prices()
        items = fetch_all_new_items()
        headlines = [item["title"] for item in items[:8]] if items else ["No major breaking news headlines reported today."]
        
        # Generate post
        analysis_text = generate_market_analysis(prices, headlines)
        
        target = test_chat_id if test_chat_id else CHANNEL_ID
        
        if analysis_text:
            bot.send_message(
                chat_id=target,
                text=analysis_text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            logging.info("Market analysis published successfully.")
            return True
        else:
            logging.warning("Market analysis generation returned empty string.")
            if test_chat_id:
                bot.send_message(test_chat_id, "❌ Не вдалося згенерувати аналіз ринку.")
            return False
            
    except Exception as e:
        logging.error(f"Error in run_market_analysis_cycle: {e}")
        if test_chat_id:
            bot.send_message(test_chat_id, f"❌ Помилка аналітики: {e}")
        return False

def scheduler_thread():
    """Background thread that manages and executes the schedule."""
    global scheduled_news, scheduled_activities, scheduled_analysis, scheduled_date
    logging.info("Scheduler thread started.")
    
    # Init schedule
    generate_daily_schedule()
    
    executed_news = set()
    executed_activities = set()
    executed_analysis = set()
    
    # Mark past slots as executed on boot
    now = datetime.now()
    with schedule_lock:
        for t in scheduled_news:
            if t < now:
                executed_news.add(t)
        for t in scheduled_activities:
            if t < now:
                executed_activities.add(t)
        for t in scheduled_analysis:
            if t < now:
                executed_analysis.add(t)
                
    while True:
        try:
            now = datetime.now()
            today = now.date()
            
            # Check for new day
            if today != scheduled_date:
                generate_daily_schedule()
                executed_news.clear()
                executed_activities.clear()
                executed_analysis.clear()
                
            trigger_news = []
            trigger_activities = []
            trigger_analysis = []
            
            with schedule_lock:
                for t in scheduled_news:
                    if t <= now and t not in executed_news:
                        trigger_news.append(t)
                for t in scheduled_activities:
                    if t <= now and t not in executed_activities:
                        trigger_activities.append(t)
                for t in scheduled_analysis:
                    if t <= now and t not in executed_analysis:
                        trigger_analysis.append(t)
                        
            # Trigger News
            for t in trigger_news:
                logging.info(f"Triggering scheduled news post ({t.strftime('%H:%M')})")
                run_publish_cycle_by_type("news")
                executed_news.add(t)
                
            # Trigger Activities
            for t in trigger_activities:
                logging.info(f"Triggering scheduled activity post ({t.strftime('%H:%M')})")
                run_publish_cycle_by_type("activity")
                executed_activities.add(t)
                
            # Trigger Market Analysis
            for t in trigger_analysis:
                logging.info(f"Triggering scheduled market analysis post ({t.strftime('%H:%M')})")
                run_market_analysis_cycle()
                executed_analysis.add(t)
                
            time.sleep(30)
        except Exception as e:
            logging.error(f"Error in scheduler_thread: {e}")
            time.sleep(60)

# --- TELEGRAM BOT COMMAND HANDLERS ---

@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    help_text = (
        "👋 <b>Привіт! Я про-версія Telegram-бота автопублікації крипто-новин та аналітики.</b>\n\n"
        "Я підтримую динамічні джерела новин, 3 типи черг публікацій та ІІ-суммаризацію.\n\n"
        "📅 <b>Мій розклад:</b>\n"
        "📰 Новини — 6 постів на день (авто-вибір)\n"
        "🎁 Активності / Аірдропи — 4 пости на день\n"
        "📊 Огляд ринку (Колонка аналітика) — 1 пост на день\n\n"
        "📋 <b>Адміністрування джерел (RSS):</b>\n"
        "/list_feeds — Показати всі активні RSS-джерела\n"
        "/add_feed [Назва] [URL] — Додати нову RSS-ленту\n"
        "/delete_feed [URL] — Видалити RSS-ленту\n\n"
        "⏳ <b>Публікація вручну у канал:</b>\n"
        "/publish_news — Опублікувати новину зараз\n"
        "/publish_activity — Опублікувати активність зараз\n"
        "/publish_analysis — Опублікувати аналіз ринку зараз\n\n"
        "📝 <b>Тестування (в цей чат):</b>\n"
        "/test_news — Тест новини\n"
        "/test_activity — Тест активності\n"
        "/test_analysis — Тест колонки аналітика\n"
        "/status — Переглянути розклад на сьогодні"
    )
    bot.reply_to(message, help_text, parse_mode="HTML")

@bot.message_handler(commands=["status"])
def handle_status(message):
    global scheduled_news, scheduled_activities, scheduled_analysis, scheduled_date
    try:
        now = datetime.now()
        
        # Fetch stats
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM published_posts")
            db_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM rss_feeds")
            feed_count = cursor.fetchone()[0]
            
        with schedule_lock:
            news_str = ", ".join([f"{t.strftime('%H:%M')}{'✓' if t in scheduled_news and t < now else ''}" for t in scheduled_news])
            act_str = ", ".join([f"{t.strftime('%H:%M')}{'✓' if t in scheduled_activities and t < now else ''}" for t in scheduled_activities])
            an_str = ", ".join([f"{t.strftime('%H:%M')}{'✓' if t in scheduled_analysis and t < now else ''}" for t in scheduled_analysis])
            curr_date = scheduled_date
            
        status_msg = (
            f"🤖 <b>Статус Crypto Publisher Bot (v3):</b>\n\n"
            f"📊 Посилань в БД: <code>{db_count}</code> | Активних стрічок: <code>{feed_count}</code>\n"
            f"📢 ID каналу: <code>{CHANNEL_ID}</code>\n"
            f"📅 Розклад на сьогодні ({curr_date}):\n"
            f"📰 <b>Новини (6):</b> {news_str}\n"
            f"🎁 <b>Активності (4):</b> {act_str}\n"
            f"📊 <b>Аналітика (1):</b> {an_str}\n\n"
            f"Поточний системний час: <code>{now.strftime('%H:%M:%S')}</code>"
        )
        bot.reply_to(message, status_msg, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка статусу: {e}")

# --- RSS FEEDS DYNAMIC MANAGEMENT ---

@bot.message_handler(commands=["list_feeds"])
def handle_list_feeds(message):
    try:
        feeds = get_rss_feeds()
        if not feeds:
            bot.reply_to(message, "📭 У базі даних немає підключених RSS-стрічок.")
            return
        lines = []
        for idx, f in enumerate(feeds, 1):
            lines.append(f"{idx}. <b>{f['name']}</b>\n   <code>{f['url']}</code>")
        bot.reply_to(message, "📋 <b>Активні RSS-джерела:</b>\n\n" + "\n\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")

@bot.message_handler(commands=["add_feed"])
def handle_add_feed(message):
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "❌ Неправильний формат. Використання:\n`/add_feed Назва_Джерела URL_Стрічки`", parse_mode="Markdown")
            return
        name = parts[1].strip()
        url = parts[2].strip()
        
        if add_rss_feed(name, url):
            bot.reply_to(message, f"✅ Джерело <b>{name}</b> успішно додано до бази даних!", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ Не вдалося додати джерело (можливо, URL вже існує).")
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")

@bot.message_handler(commands=["delete_feed"])
def handle_delete_feed(message):
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "❌ Неправильний формат. Використання:\n`/delete_feed URL_Стрічки`", parse_mode="Markdown")
            return
        url = parts[1].strip()
        
        if delete_rss_feed(url):
            bot.reply_to(message, "✅ Джерело успішно видалено з бази даних.")
        else:
            bot.reply_to(message, "❌ Джерело з таким URL не знайдено.")
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")

# --- MANUAL PUBLISH TRIGGERS ---

@bot.message_handler(commands=["publish_news"])
def handle_publish_news(message):
    bot.reply_to(message, "⏳ Починаю збір та публікацію Новини в канал...")
    threading.Thread(target=run_publish_cycle_by_type, args=("news",), kwargs={"test_chat_id": None}).start()

@bot.message_handler(commands=["publish_activity"])
def handle_publish_activity(message):
    bot.reply_to(message, "⏳ Починаю збір та публікацію Активності в канал...")
    threading.Thread(target=run_publish_cycle_by_type, args=("activity",), kwargs={"test_chat_id": None}).start()

@bot.message_handler(commands=["publish_analysis"])
def handle_publish_analysis(message):
    bot.reply_to(message, "⏳ Починаю генерацію та публікацію Аналізу Ринку в канал...")
    threading.Thread(target=run_market_analysis_cycle, kwargs={"test_chat_id": None}).start()

# --- TEST TRIGGERS (TO USER CHAT) ---

@bot.message_handler(commands=["test_news"])
def handle_test_news(message):
    bot.reply_to(message, "⏳ Тест: Збір та підготовка новини (буде надіслано сюди, канал та БД не зміняться)...")
    threading.Thread(target=run_publish_cycle_by_type, args=("news", message.chat.id)).start()

@bot.message_handler(commands=["test_activity"])
def handle_test_activity(message):
    bot.reply_to(message, "⏳ Тест: Збір та підготовка активності (буде надіслано сюди, канал та БД не зміняться)...")
    threading.Thread(target=run_publish_cycle_by_type, args=("activity", message.chat.id)).start()

@bot.message_handler(commands=["test_analysis"])
def handle_test_analysis(message):
    bot.reply_to(message, "⏳ Тест: Збір ринкових даних та генерація аналітики (буде надіслано сюди, канал не зміниться)...")
    threading.Thread(target=run_market_analysis_cycle, kwargs={"test_chat_id": message.chat.id}).start()

if __name__ == "__main__":
    logging.info("Starting bot services (v3 with Render support)...")
    
    # 1. Start Web Server for Render Health Checks
    web_t = threading.Thread(target=run_web_server, daemon=True)
    web_t.start()
    
    # 2. Start Self-pinging keep-alive loop to prevent sleeping
    ping_t = threading.Thread(target=keep_alive_thread, daemon=True)
    ping_t.start()
    
    # 3. Start background scheduler thread
    sched_t = threading.Thread(target=scheduler_thread, daemon=True)
    sched_t.start()
    
    # 4. Start telegram bot polling
    logging.info("Telegram Bot starts polling...")
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        logging.info("Stopping bot...")
