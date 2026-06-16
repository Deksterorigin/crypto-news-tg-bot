import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import telebot
from telebot.async_telebot import AsyncTeleBot
import asyncio
import time
import random
import logging
import threading
import os
import requests
from datetime import datetime, timedelta
from functools import wraps
from http.server import SimpleHTTPRequestHandler, HTTPServer
from config import BOT_TOKEN, CHANNEL_ID, get_berlin_now
from fetcher import fetch_all_new_items, extract_image_url
from processor import generate_single_post_by_type, generate_market_analysis, is_news_highly_urgent
from db import (
    mark_as_published as _sync_mark_as_published,
    get_connection as _sync_get_connection,
    add_rss_feed as _sync_add_rss_feed,
    delete_rss_feed as _sync_delete_rss_feed,
    get_rss_feeds as _sync_get_rss_feeds,
    get_owner_id as _sync_get_owner_id,
    set_owner_id as _sync_set_owner_id,
    is_admin as _sync_is_admin,
    add_admin as _sync_add_admin,
    delete_admin as _sync_delete_admin,
    get_admins as _sync_get_admins,
    get_setting as _sync_get_setting,
    set_setting as _sync_set_setting,
    get_channels as _sync_get_channels,
    add_channel as _sync_add_channel,
    delete_channel as _sync_delete_channel
)

db_lock = asyncio.Lock()
_owner_bootstrap_lock = asyncio.Lock()

_background_tasks: set = set()

def create_tracked_task(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

async def run_db(func, *args, **kwargs):
    async with db_lock:
        return await asyncio.to_thread(func, *args, **kwargs)

async def get_owner_id(*args, **kwargs):
    return await run_db(_sync_get_owner_id, *args, **kwargs)

async def set_owner_id(*args, **kwargs):
    return await run_db(_sync_set_owner_id, *args, **kwargs)

async def is_admin(*args, **kwargs):
    return await run_db(_sync_is_admin, *args, **kwargs)

async def add_admin(*args, **kwargs):
    return await run_db(_sync_add_admin, *args, **kwargs)

async def delete_admin(*args, **kwargs):
    return await run_db(_sync_delete_admin, *args, **kwargs)

async def get_admins(*args, **kwargs):
    return await run_db(_sync_get_admins, *args, **kwargs)

async def get_setting(*args, **kwargs):
    return await run_db(_sync_get_setting, *args, **kwargs)

async def set_setting(*args, **kwargs):
    return await run_db(_sync_set_setting, *args, **kwargs)

async def get_channels(*args, **kwargs):
    return await run_db(_sync_get_channels, *args, **kwargs)

async def add_channel(*args, **kwargs):
    return await run_db(_sync_add_channel, *args, **kwargs)

async def delete_channel(*args, **kwargs):
    return await run_db(_sync_delete_channel, *args, **kwargs)

async def add_rss_feed(*args, **kwargs):
    return await run_db(_sync_add_rss_feed, *args, **kwargs)

async def delete_rss_feed(*args, **kwargs):
    return await run_db(_sync_delete_rss_feed, *args, **kwargs)

async def get_rss_feeds(*args, **kwargs):
    return await run_db(_sync_get_rss_feeds, *args, **kwargs)

async def mark_as_published(*args, **kwargs):
    return await run_db(_sync_mark_as_published, *args, **kwargs)

def get_connection(*args, **kwargs):
    return _sync_get_connection(*args, **kwargs)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Initialize Telegram Bot
bot = AsyncTeleBot(BOT_TOKEN)

# Thread-safe locks and global state
schedule_lock = asyncio.Lock()
scheduled_news = []          # Datetimes for News posts
scheduled_activities = []    # Datetimes for Activity/Earning posts
scheduled_analysis = []      # Datetime for Market Analysis column
scheduled_date = None        # date object tracking the current day of the schedule

# --- SECURITY DECORATORS ---

def admin_only(func):
    """Decorator to restrict commands to authorized admins and the owner."""
    @wraps(func)
    async def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        
        # Bootstrap: if no owner exists, register the first user
        owner_id = await get_owner_id()
        if owner_id is None:
            await set_owner_id(user_id)
            await bot.reply_to(
                message, 
                f"👑 <b>Вітаємо!</b>\nВи автоматично зареєстровані як <b>Власник</b> цього бота (Ваш ID: <code>{user_id}</code>).", 
                parse_mode="HTML"
            )
            return await func(message, *args, **kwargs)
            
        if await is_admin(user_id):
            return await func(message, *args, **kwargs)
        else:
            await bot.reply_to(
                message, 
                f"🔒 <b>Доступ обмежено.</b>\nВаш Telegram ID: <code>{user_id}</code>.\nПопросіть власника надати вам доступ.", 
                parse_mode="HTML"
            )
    return wrapper

def owner_only(func):
    """Decorator to restrict commands strictly to the bot owner."""
    @wraps(func)
    async def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        
        is_new_owner = False
        async with _owner_bootstrap_lock:
            owner_id = await get_owner_id()
            if owner_id is None:
                await set_owner_id(user_id)
                owner_id = user_id
                is_new_owner = True
        
        if is_new_owner:
            await bot.reply_to(
                message, 
                f"👑 <b>Вітаємо!</b>\nВи автоматично зареєстровані як <b>Власник</b> цього бота (Ваш ID: <code>{user_id}</code>).", 
                parse_mode="HTML"
            )
            return await func(message, *args, **kwargs)
            
        if user_id == owner_id:
            return await func(message, *args, **kwargs)
        else:
            await bot.reply_to(
                message, 
                "👑 Ця команда доступна тільки <b>Власнику</b> бота.", 
                parse_mode="HTML"
            )
    return wrapper

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
        pass

def run_web_server():
    """Runs a web server to bind to Render's port for health check verification."""
    try:
        port = int(os.getenv("PORT", 10000))
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        logging.info(f"Web server started on port {port} for Render health checks.")
        server.serve_forever()
    except Exception as e:
        logging.error(f"Error starting health check web server: {e}")

async def keep_alive_task():
    """Periodically pings the Render app's public URL to prevent it from sleeping."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        logging.info("RENDER_EXTERNAL_URL is not set. Self-pinging keep-alive is disabled.")
        return
        
    logging.info(f"Self-pinging keep-alive loop started for: {url}")
    import aiohttp
    while True:
        try:
            await asyncio.sleep(300)  # Ping every 5 minutes
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    logging.info(f"Self-ping sent to {url}, response status: {response.status}")
        except Exception as e:
            logging.error(f"Error in self-ping loop: {e}")

# --- COINGECKO PRICES FETCHER ---

async def fetch_coingecko_prices() -> dict:
    """Fetches real-time prices for BTC, ETH, and SOL using CoinGecko's simple price API."""
    import aiohttp
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    return await response.json()
    except Exception as e:
        logging.error(f"Error fetching CoinGecko prices: {e}")
    return {
        "bitcoin": {"usd": 0.0, "usd_24h_change": 0.0},
        "ethereum": {"usd": 0.0, "usd_24h_change": 0.0},
        "solana": {"usd": 0.0, "usd_24h_change": 0.0}
    }

# --- SCHEDULER & PUBLISHING LOGIC ---

def _generate_daily_schedule_db_ops(force, now, today):
    # Check count
    count = 0
    try:
        with _sync_get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM daily_schedule WHERE date(post_time) = ?", (today.isoformat(),))
            count = cursor.fetchone()[0]
    except Exception as e:
        logging.error(f"Error checking schedule in DB: {e}")
        
    if count == 0 or force:
        try:
            if force:
                with _sync_get_connection() as conn:
                    conn.execute("DELETE FROM daily_schedule WHERE date(post_time) = ?", (today.isoformat(),))
                    conn.commit()
                    logging.info(f"Forced regeneration: deleted today's schedule for {today}")
            
            # Load dynamic configurations
            news_count = int(_sync_get_setting("news_count", "6"))
            activity_count = int(_sync_get_setting("activity_count", "4"))
            start_hour = int(_sync_get_setting("start_hour", "10"))
            end_hour = int(_sync_get_setting("end_hour", "22"))
            
            window_minutes = (end_hour - start_hour) * 60
            start_offset = start_hour * 60
            
            news_times = []
            news_segment = float(window_minutes) / news_count
            for i in range(news_count):
                offset = random.randint(int(i * news_segment), int((i + 1) * news_segment) - 1)
                dt = datetime.combine(today, datetime.min.time()) + timedelta(minutes=start_offset + offset)
                news_times.append(dt)
                
            activity_times = []
            activity_segment = float(window_minutes) / activity_count
            for i in range(activity_count):
                offset = random.randint(int(i * activity_segment), int((i + 1) * activity_segment) - 1)
                dt = datetime.combine(today, datetime.min.time()) + timedelta(minutes=start_offset + offset)
                activity_times.append(dt)
                
            analysis_offset = random.randint(start_offset + 60, start_offset + 180)
            analysis_dt = datetime.combine(today, datetime.min.time()) + timedelta(minutes=analysis_offset)
            
            with _sync_get_connection() as conn:
                for dt in news_times:
                    is_exec = 1 if dt < now else 0
                    conn.execute(
                        "INSERT INTO daily_schedule (post_time, post_type, is_executed) VALUES (?, 'news', ?)",
                        (dt.strftime('%Y-%m-%d %H:%M:%S'), is_exec)
                    )
                for dt in activity_times:
                    is_exec = 1 if dt < now else 0
                    conn.execute(
                        "INSERT INTO daily_schedule (post_time, post_type, is_executed) VALUES (?, 'activity', ?)",
                        (dt.strftime('%Y-%m-%d %H:%M:%S'), is_exec)
                    )
                is_exec = 1 if analysis_dt < now else 0
                conn.execute(
                    "INSERT INTO daily_schedule (post_time, post_type, is_executed) VALUES (?, 'analysis', ?)",
                    (analysis_dt.strftime('%Y-%m-%d %H:%M:%S'), is_exec)
                )
                conn.commit()
            logging.info(f"Daily schedules generated dynamically and saved to DB for {today}.")
        except Exception as e:
            logging.error(f"Error generating daily schedule: {e}")
            
    # Read rows
    try:
        with _sync_get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT post_time, post_type FROM daily_schedule WHERE date(post_time) = ? ORDER BY post_time ASC",
                (today.isoformat(),)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error syncing schedule globals: {e}")
        return []

async def generate_daily_schedule(force=False):
    global scheduled_news, scheduled_activities, scheduled_analysis, scheduled_date
    now = get_berlin_now()
    today = now.date()
    
    async with schedule_lock:
        rows = await run_db(_generate_daily_schedule_db_ops, force, now, today)
        
        scheduled_news = []
        scheduled_activities = []
        scheduled_analysis = []
        
        for row in rows:
            dt = datetime.strptime(row["post_time"], '%Y-%m-%d %H:%M:%S')
            ptype = row["post_type"]
            if ptype == "news":
                scheduled_news.append(dt)
            elif ptype == "activity":
                scheduled_activities.append(dt)
            elif ptype == "analysis":
                scheduled_analysis.append(dt)
        scheduled_date = today

async def run_publish_cycle_by_type(post_type: str, test_chat_id=None) -> tuple:
    """Executes a news or activity publishing cycle, posting to all registered channels."""
    logging.info(f"Running publish cycle for type: {post_type}")
    try:
        items = await asyncio.to_thread(fetch_all_new_items)
        if not items:
            logging.info("No new items found in feeds.")
            if test_chat_id:
                await bot.send_message(test_chat_id, "❌ Не знайдено нових матеріалів для сбору.")
            return False, "Не знайдено нових матеріалів для збору (RSS/Bybit)"
            
        selected_link, post_text, poll = await generate_single_post_by_type(items, post_type, skip_dedup=(test_chat_id is not None))
        
        # Get target channels list from DB
        channels = await get_channels()
        if not channels and not test_chat_id:
            logging.warning("No channels configured in SQLite db. Skipping publication.")
            return False, "Канали для публікації не налаштовані в базі даних"
            
        targets = [test_chat_id] if test_chat_id else [ch["channel_id"] for ch in channels]
        
        if post_text and selected_link:
            img_url = await asyncio.to_thread(extract_image_url, selected_link)
            
            for target in targets:
                if img_url:
                    try:
                        await bot.send_photo(chat_id=target, photo=img_url, caption=post_text, parse_mode="HTML")
                        logging.info(f"Photo post for {post_type} published to {target}.")
                    except Exception as pe:
                        logging.error(f"Failed to post photo to {target}: {pe}. Falling back to text.")
                        await bot.send_message(chat_id=target, text=post_text, parse_mode="HTML", disable_web_page_preview=False)
                else:
                    await bot.send_message(chat_id=target, text=post_text, parse_mode="HTML", disable_web_page_preview=False)
                    logging.info(f"Text post for {post_type} published to {target}.")
                
                # Send poll if present
                if poll and isinstance(poll, dict):
                    try:
                        await bot.send_poll(
                            chat_id=target,
                            question=poll.get("question")[:80],
                            options=[opt[:30] for opt in poll.get("options", [])],
                            is_anonymous=True
                        )
                        logging.info(f"News/Activity poll sent to {target}.")
                    except Exception as pole:
                        logging.error(f"Failed to send poll to {target}: {pole}")
                
            if not test_chat_id:
                # Mark ONLY the chosen item as actually posted
                selected_item = next((i for i in items if i["link"] == selected_link), None)
                if selected_item:
                    await mark_as_published(selected_item["link"], selected_item["title"], selected_item["source"], was_posted=1)
            return True, None
        else:
            logging.info(f"No suitable post of type {post_type} selected.")
            if test_chat_id:
                await bot.send_message(test_chat_id, f"⚠️ Gemini не знайшов підходящих матеріалів для типу '{post_type}'.")
            return False, "Немає підходящих нових матеріалів (усі дублікати або ШІ відхилив)"
            
    except Exception as e:
        logging.error(f"Error in run_publish_cycle_by_type ({post_type}): {e}")
        if test_chat_id:
            await bot.send_message(test_chat_id, f"❌ Помилка: {e}")
        return False, f"Виникла помилка: {str(e)}"

async def run_market_analysis_cycle(test_chat_id=None) -> tuple:
    """Executes a market analysis review, posting to all registered channels."""
    logging.info("Running market analysis cycle...")
    try:
        prices = await fetch_coingecko_prices()
        items = await asyncio.to_thread(fetch_all_new_items)
        headlines = [item["title"] for item in items[:8]] if items else ["No major breaking news headlines reported today."]
        
        analysis_text = await generate_market_analysis(prices, headlines)
        
        channels = await get_channels()
        if not channels and not test_chat_id:
            logging.warning("No channels configured in SQLite. Skipping analysis.")
            return False, "Канали для публікації не налаштовані в базі даних"
            
        targets = [test_chat_id] if test_chat_id else [ch["channel_id"] for ch in channels]
        
        if analysis_text:
            for target in targets:
                await bot.send_message(
                    chat_id=target,
                    text=analysis_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                # Send standard market sentiment poll
                try:
                    await bot.send_poll(
                        chat_id=target,
                        question="Які ваші очікування від ринку на найближчу добу?",
                        options=["🚀 Бичачі (Ріст)", "📉 Ведмежі (Падіння)", "🤷‍♂️ Флет / Невизначеність"],
                        is_anonymous=True
                    )
                    logging.info(f"Market sentiment poll sent to {target}.")
                except Exception as pole:
                    logging.error(f"Failed to send sentiment poll to {target}: {pole}")
            logging.info("Market analysis published successfully.")
            return True, None
        else:
            logging.warning("Market analysis generation returned empty string.")
            if test_chat_id:
                await bot.send_message(test_chat_id, "❌ Не вдалося згенерувати аналіз ринку.")
            return False, "Gemini повернув порожній текст аналітики"
            
    except Exception as e:
        logging.error(f"Error in run_market_analysis_cycle: {e}")
        if test_chat_id:
            await bot.send_message(test_chat_id, f"❌ Помилка аналітики: {e}")
        return False, f"Помилка генерації аналітики: {str(e)}"

def _get_pending_scheduled_posts_sync(max_time: datetime) -> list:
    try:
        with _sync_get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, post_time, post_type, is_executed FROM daily_schedule "
                "WHERE is_executed = 0 AND post_time <= ? ORDER BY post_time ASC",
                (max_time.strftime('%Y-%m-%d %H:%M:%S'),)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error fetching pending scheduled posts: {e}")
        return []

async def get_pending_scheduled_posts(max_time: datetime) -> list:
    return await run_db(_get_pending_scheduled_posts_sync, max_time)

def _mark_scheduled_post_executed_sync(post_id: int):
    try:
        with _sync_get_connection() as conn:
            conn.execute(
                "UPDATE daily_schedule SET is_executed = 1 WHERE id = ?",
                (post_id,)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Error marking scheduled post as executed: {e}")

async def mark_scheduled_post_executed(post_id: int):
    await run_db(_mark_scheduled_post_executed_sync, post_id)

def _reschedule_scheduled_post_sync(post_id: int, new_time: datetime):
    try:
        with _sync_get_connection() as conn:
            conn.execute(
                "UPDATE daily_schedule SET post_time = ? WHERE id = ?",
                (new_time.strftime('%Y-%m-%d %H:%M:%S'), post_id)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Error rescheduling post: {e}")

async def reschedule_scheduled_post(post_id: int, new_time: datetime):
    await run_db(_reschedule_scheduled_post_sync, post_id, new_time)

async def notify_admins_of_failure(post_type: str, reason: str = "не знайдено нового контенту або помилка ШІ"):
    """Sends a failure notification to the owner and all administrators."""
    owner_id = await get_owner_id()
    admins = await get_admins()
    
    ptype_ua = {
        "news": "Новини",
        "activity": "Активності",
        "analysis": "Аналітика ринку"
    }.get(post_type, post_type)
    
    text = (
        f"⚠️ <b>Попередження автопостингу</b>\n\n"
        f"Бот не зміг опублікувати запланований пост типу: <b>{ptype_ua}</b>.\n"
        f"Причина: {reason}.\n"
        f"🕒 Пост перенесено на 30 хвилин пізніше."
    )
    
    targets = []
    if owner_id:
        targets.append(owner_id)
    for adm in admins:
        targets.append(adm["user_id"])
        
    targets = list(set(targets))
    
    for target in targets:
        try:
            await bot.send_message(target, text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Failed to send failure notification to admin {target}: {e}")

async def scheduler_task():
    """Background task that manages and executes the schedule using SQLite persistence."""
    logging.info("Scheduler task started.")
    
    await generate_daily_schedule()
    
    while True:
        try:
            now = get_berlin_now()
            today = now.date()
            
            # Ensure daily schedule exists for today (runs daily date check)
            await generate_daily_schedule()
            
            # Query for pending posts that are due
            pending_posts = await get_pending_scheduled_posts(now)
            
            if pending_posts:
                # Trigger only the first pending post
                first_post = pending_posts[0]
                post_id = first_post["id"]
                post_type = first_post["post_type"]
                post_time_str = first_post["post_time"]
                
                logging.info(f"Triggering scheduled {post_type} post (ID: {post_id}, scheduled for {post_time_str})")
                
                success = False
                reason = "Невідома помилка"
                if post_type == "news":
                    success, reason = await run_publish_cycle_by_type("news")
                elif post_type == "activity":
                    success, reason = await run_publish_cycle_by_type("activity")
                elif post_type == "analysis":
                    success, reason = await run_market_analysis_cycle()
                    
                if success:
                    await mark_scheduled_post_executed(post_id)
                    logging.info(f"Scheduled {post_type} post (ID: {post_id}) executed successfully.")
                else:
                    # Failed: reschedule post to 30 minutes in the future
                    new_time = now + timedelta(minutes=30)
                    await reschedule_scheduled_post(post_id, new_time)
                    logging.info(f"Scheduled {post_type} post (ID: {post_id}) failed. Rescheduled to {new_time.strftime('%Y-%m-%d %H:%M:%S')}. Reason: {reason}")
                    await notify_admins_of_failure(post_type, reason)
                
                # Space out any remaining pending posts
                # to prevent back-to-back spam in the channel.
                for idx, post in enumerate(pending_posts[1:], 1):
                    spaced_time = now + timedelta(minutes=idx * 10)
                    await reschedule_scheduled_post(post["id"], spaced_time)
                    logging.info(f"Spaced out pending post ID {post['id']} ({post['post_type']}) to {spaced_time.strftime('%Y-%m-%d %H:%M:%S')}")
                
                # Regenerate daily schedule list globals to keep /status command and globals in sync
                # This guarantees that the rescheduled/updated times appear correctly in /status.
                await generate_daily_schedule()
                
            await asyncio.sleep(30)
        except Exception as e:
            logging.error(f"Error in scheduler_task: {e}")
            await asyncio.sleep(30)


async def breaking_news_monitor_task():
    """Background task that monitors RSS feeds for breaking keywords and publishes instantly."""
    logging.info("Breaking news monitor task started.")
    while True:
        try:
            # 1. Fetch breaking keywords
            keywords_str = await get_setting("breaking_keywords", "")
            keywords = [w.strip().lower() for w in keywords_str.split(",") if w.strip()]
            
            if keywords:
                # Fetch new RSS items
                items = await asyncio.to_thread(fetch_all_new_items)
                
                # Filter items whose titles contain breaking keywords
                breaking_items = []
                for item in items:
                    title_lower = item["title"].lower()
                    if any(kw in title_lower for kw in keywords):
                        breaking_items.append(item)
                
                if breaking_items:
                    logging.info(f"Found {len(breaking_items)} potential breaking news items based on keywords.")
                    
                    actual_breaking_items = []
                    from db import is_already_published as _sync_is_already_published
                    
                    for item in breaking_items:
                        is_pub = await run_db(_sync_is_already_published, item["link"])
                        if is_pub:
                            continue
                        # Verify using Gemini urgency filter
                        if await is_news_highly_urgent(item["title"], item["summary"]):
                            logging.info(f"🚨 Verified highly urgent breaking news: '{item['title']}'")
                            actual_breaking_items.append(item)
                        else:
                            logging.info(f"ℹ️ Item filtered out as not urgent enough for breaking news: '{item['title']}'")
                    
                    if actual_breaking_items:
                        for item in actual_breaking_items:
                            is_pub = await run_db(_sync_is_already_published, item["link"])
                            if is_pub:
                                continue
                            
                            selected_link, post_text, poll = await generate_single_post_by_type(actual_breaking_items, "breaking", skip_dedup=False)
                            
                            if selected_link and post_text:
                                channels = await get_channels()
                                targets = [ch["channel_id"] for ch in channels]
                                
                                if targets:
                                    img_url = await asyncio.to_thread(extract_image_url, selected_link)
                                    for target in targets:
                                        if img_url:
                                            try:
                                                await bot.send_photo(chat_id=target, photo=img_url, caption=post_text, parse_mode="HTML")
                                                logging.info(f"Breaking news photo post published to {target}")
                                            except Exception as pe:
                                                logging.error(f"Failed to post breaking photo to {target}: {pe}. Falling back to text.")
                                                await bot.send_message(chat_id=target, text=post_text, parse_mode="HTML", disable_web_page_preview=False)
                                        else:
                                            await bot.send_message(chat_id=target, text=post_text, parse_mode="HTML", disable_web_page_preview=False)
                                            logging.info(f"Breaking news text post published to {target}")
                                        
                                        # Send poll if present
                                        if poll and isinstance(poll, dict):
                                            try:
                                                await bot.send_poll(
                                                    chat_id=target,
                                                    question=poll.get("question")[:80],
                                                    options=[opt[:30] for opt in poll.get("options", [])],
                                                    is_anonymous=True
                                                )
                                                logging.info(f"Breaking news poll sent to {target}")
                                            except Exception as pole:
                                                logging.error(f"Failed to send poll for breaking news to {target}: {pole}")
                                                
                                    # Mark the chosen item as actually posted
                                    selected_item = next((i for i in actual_breaking_items if i["link"] == selected_link), None)
                                    if selected_item:
                                        await mark_as_published(selected_item["link"], selected_item["title"], selected_item["source"], was_posted=1)
                                    
                                    # Mark other actual breaking items we fetched as processed (not posted) so they don't spam
                                    for b_item in actual_breaking_items:
                                        if b_item["link"] != selected_link:
                                            await mark_as_published(b_item["link"], b_item["title"], b_item["source"], was_posted=0)
                                    break
            await asyncio.sleep(300)
        except Exception as e:
            logging.error(f"Error in breaking news monitor task: {e}")
            await asyncio.sleep(300)

# --- KEYBOARD BUILDERS ---

async def main_menu_keyboard(user_id=None) -> telebot.types.ReplyKeyboardMarkup:
    """Builds the persistent bottom reply menu for admins."""
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📊 Статус", "📈 Аналітика")
    markup.row("⚙️ Налаштування", "🔄 Оновити розклад")
    markup.row("📢 Канали", "🔗 RSS-Джерела")
    markup.row("📝 Тест-Пости", "⏳ Опублікувати зараз")
    if user_id and user_id == await get_owner_id():
        markup.row("👥 Адміністратори", "💾 Резервна копія БД")
        markup.row("ℹ️ Довідка")
    else:
        markup.row("ℹ️ Довідка")
    return markup

async def get_settings_menu() -> tuple[str, telebot.types.InlineKeyboardMarkup]:
    """Generates the settings panel and inline keyboard."""
    news_count = await get_setting("news_count", "6")
    activity_count = await get_setting("activity_count", "4")
    start_hour = await get_setting("start_hour", "10")
    end_hour = await get_setting("end_hour", "22")
    
    text = (
        f"⚙️ <b>Налаштування розкладу публікацій:</b>\n\n"
        f"📰 Кількість новин на день: <b>{news_count}</b>\n"
        f"🎁 Кількість активностей на день: <b>{activity_count}</b>\n"
        f"⏰ Активне вікно: з <b>{start_hour}:00</b> до <b>{end_hour}:00</b>\n\n"
        f"<i>Зміни будуть застосовані при генерації розкладу на наступний день, або ви можете примусово перегенерувати розклад командою /regenerate.</i>"
    )
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("📰 Змінити к-ть новин", callback_data="set_news_count"),
        telebot.types.InlineKeyboardButton("🎁 Змінити к-ть активностей", callback_data="set_act_count")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("⏰ Початок вікна (год)", callback_data="set_start_hour"),
        telebot.types.InlineKeyboardButton("⏰ Кінець вікна (год)", callback_data="set_end_hour")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("🔧 Додаткові налаштування", callback_data="advanced_settings")
    )
    return text, markup

async def get_advanced_settings_menu() -> tuple[str, telebot.types.InlineKeyboardMarkup]:
    """Generates the advanced settings panel and inline keyboard."""
    blacklist_words = await get_setting("blacklist_words", "presale, pre-sale, 10000%, 1000x, scam, скандал")
    breaking_keywords = await get_setting("breaking_keywords", "massive, hack, halving, sec, approved, exploit, bankrupt, liquidation")
    proxies = await get_setting("proxies", "")
    
    blacklist_short = blacklist_words[:100] + ("..." if len(blacklist_words) > 100 else "")
    breaking_short = breaking_keywords[:100] + ("..." if len(breaking_keywords) > 100 else "")
    proxies_short = proxies[:100] + ("..." if len(proxies) > 100 else "") if proxies else "Не налаштовано (пряме підключення)"
    
    text = (
        f"🔧 <b>Додаткові налаштування бота:</b>\n\n"
        f"🚫 <b>Чорний список слів:</b>\n"
        f"<code>{blacklist_short}</code>\n\n"
        f"🚨 <b>Ключові слова для Breaking News:</b>\n"
        f"<code>{breaking_short}</code>\n\n"
        f"🌐 <b>Список проксі:</b>\n"
        f"<code>{proxies_short}</code>\n\n"
        f"<i>Виберіть параметр для зміни:</i>"
    )
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("🚫 Чорний Список", callback_data="edit_blacklist"),
        telebot.types.InlineKeyboardButton("🚨 Breaking слова", callback_data="edit_breaking")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("🌐 Налаштувати Проксі", callback_data="edit_proxies"),
        telebot.types.InlineKeyboardButton("🔌 Перевірити Проксі", callback_data="check_proxies")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")
    )
    return text, markup

async def get_channels_menu() -> tuple[str, telebot.types.InlineKeyboardMarkup]:
    """Generates the target channels panel and inline keyboard."""
    channels = await get_channels()
    if not channels:
        text = "📢 <b>Канали для публікації:</b>\n\n📭 Немає підключених каналів! Додайте хоча б один канал, щоб бот міг туди писати."
    else:
        lines = []
        for idx, ch in enumerate(channels, 1):
            lines.append(f"{idx}. <b>{ch['name']}</b>\n   ID: <code>{ch['channel_id']}</code>")
        text = "📢 <b>Підключені канали для публікації:</b>\n\n" + "\n\n".join(lines)
        
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("➕ Додати канал", callback_data="add_channel"),
        telebot.types.InlineKeyboardButton("❌ Видалити канал", callback_data="delete_channel")
    )
    return text, markup

async def get_feeds_menu() -> tuple[str, telebot.types.InlineKeyboardMarkup]:
    """Generates the RSS sources panel and inline keyboard."""
    feeds = await get_rss_feeds()
    if not feeds:
        text = "🔗 <b>Джерела RSS-стрічок:</b>\n\n📭 Немає підключених джерел."
    else:
        lines = []
        for idx, f in enumerate(feeds, 1):
            lines.append(f"{idx}. <b>{f['name']}</b>\n   <code>{f['url']}</code>")
        text = "🔗 <b>Активні RSS-джерела:</b>\n\n" + "\n\n".join(lines)
        
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("➕ Додати джерело", callback_data="add_feed"),
        telebot.types.InlineKeyboardButton("❌ Видалити джерело", callback_data="delete_feed")
    )
    return text, markup

def get_test_menu() -> telebot.types.InlineKeyboardMarkup:
    """Inline menu to trigger dry-run test posts."""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("📰 Тест Новини", callback_data="t_news"),
        telebot.types.InlineKeyboardButton("🎁 Тест Активності", callback_data="t_activity")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("📊 Тест Аналітики", callback_data="t_analysis")
    )
    return markup

def get_publish_menu() -> telebot.types.InlineKeyboardMarkup:
    """Inline menu to trigger live channel publications."""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("📰 Опубл. Новину", callback_data="p_news"),
        telebot.types.InlineKeyboardButton("🎁 Опубл. Активність", callback_data="p_activity")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("📊 Опубл. Аналітику", callback_data="p_analysis")
    )
    return markup

# --- TELEGRAM BOT DYNAMIC DIALOG FLOWS (register_next_step_handler) ---

async def check_cancel_command(message) -> bool:
    """If message is a command or menu button, clears step handler, re-processes it, and returns True."""
    menu_buttons = [
        "📊 Статус", "📈 Аналітика", "⚙️ Налаштування", "🔄 Оновити розклад", 
        "📢 Канали", "🔗 RSS-Джерела", "📝 Тест-Пости", "⏳ Опублікувати зараз", 
        "👥 Адміністратори", "💾 Резервна копія БД", "ℹ️ Довідка"
    ]
    if message.text and (message.text.startswith("/") or message.text in menu_buttons):
        await bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
        bot.process_new_messages([message])
        return True
    return False

async def process_set_news_count(message):
    if await check_cancel_command(message):
        return
    try:
        val = int(message.text.strip())
        if val <= 0 or val > 30:
            raise ValueError
        await set_setting("news_count", str(val))
        await bot.send_message(message.chat.id, f"✅ Кількість новин успішно змінена на <b>{val}</b> на день!", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
    except ValueError:
        await bot.send_message(message.chat.id, "❌ Помилка. Введіть позитивне число (від 1 до 30):")
        bot.register_next_step_handler(message, process_set_news_count)

async def process_set_act_count(message):
    if await check_cancel_command(message):
        return
    try:
        val = int(message.text.strip())
        if val <= 0 or val > 20:
            raise ValueError
        await set_setting("activity_count", str(val))
        await bot.send_message(message.chat.id, f"✅ Кількість активнотей успішно змінена на <b>{val}</b> на день!", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
    except ValueError:
        await bot.send_message(message.chat.id, "❌ Помилка. Введіть позитивне число (від 1 до 20):")
        bot.register_next_step_handler(message, process_set_act_count)

async def process_set_start_hour(message):
    if await check_cancel_command(message):
        return
    try:
        val = int(message.text.strip())
        end_h = int(await get_setting("end_hour", "22"))
        if val < 0 or val >= end_h or val > 23:
            raise ValueError
        await set_setting("start_hour", str(val))
        await bot.send_message(message.chat.id, f"✅ Початок активного вікна змінено на <b>{val}:00</b>!", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
    except ValueError:
        await bot.send_message(message.chat.id, f"❌ Помилка. Введіть годину від 0 до {int(await get_setting('end_hour', '22'))-1}:")
        bot.register_next_step_handler(message, process_set_start_hour)

async def process_set_end_hour(message):
    if await check_cancel_command(message):
        return
    try:
        val = int(message.text.strip())
        start_h = int(await get_setting("start_hour", "10"))
        if val <= start_h or val > 24:
            raise ValueError
        await set_setting("end_hour", str(val))
        await bot.send_message(message.chat.id, f"✅ Кінець активного вікна змінено на <b>{val}:00</b>!", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
    except ValueError:
        await bot.send_message(message.chat.id, f"❌ Помилка. Введіть годину від {int(await get_setting('start_hour', '10'))+1} до 24:")
        bot.register_next_step_handler(message, process_set_end_hour)

async def process_add_channel(message):
    if await check_cancel_command(message):
        return
    try:
        text = message.text.strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await bot.send_message(message.chat.id, "❌ Неправильний формат. Введіть ID та Назву через пробіл (наприклад: <code>-100123456789 КриптоКанал</code>):", parse_mode="HTML")
            bot.register_next_step_handler(message, process_add_channel)
            return
            
        ch_id = parts[0].strip()
        name = parts[1].strip()
        
        if await add_channel(ch_id, name):
            await bot.send_message(message.chat.id, f"✅ Канал <code>{ch_id}</code> успішно додано.", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
        else:
            await bot.send_message(message.chat.id, "❌ Не вдалося додати канал. Перевірте формат.")
    except Exception as e:
        await bot.send_message(message.chat.id, f"❌ Помилка: {e}")

async def process_delete_channel(message):
    if await check_cancel_command(message):
        return
    ch_id = message.text.strip()
    if await delete_channel(ch_id):
        await bot.send_message(message.chat.id, f"✅ Канал з ID <code>{ch_id}</code> успішно видалено.", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
    else:
        await bot.send_message(message.chat.id, f"❌ Канал з ID <code>{ch_id}</code> не знайдено в базі.", parse_mode="HTML")

async def process_add_feed(message):
    if await check_cancel_command(message):
        return
    try:
        text = message.text.strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await bot.send_message(message.chat.id, "❌ Неправильний формат. Введіть Назву та URL через пробіл (наприклад: <code>CoinDesk https://coindesk.com/arc/outboundfeed/rss/</code>):", parse_mode="HTML")
            bot.register_next_step_handler(message, process_add_feed)
            return
            
        name = parts[0].strip()
        url = parts[1].strip()
        
        if await add_rss_feed(name, url):
            await bot.send_message(message.chat.id, f"✅ Джерело <b>{name}</b> (<code>{url}</code>) успішно додано.", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
        else:
            await bot.send_message(message.chat.id, "❌ Не вдалося додати джерело. Можливо, воно вже є.")
    except Exception as e:
        await bot.send_message(message.chat.id, f"❌ Помилка: {e}")

async def process_delete_feed(message):
    if await check_cancel_command(message):
        return
    url = message.text.strip()
    if await delete_rss_feed(url):
        await bot.send_message(message.chat.id, f"✅ Джерело <code>{url}</code> видалено.", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
    else:
        await bot.send_message(message.chat.id, f"❌ Джерело з адресою <code>{url}</code> не знайдено.", parse_mode="HTML")

async def process_edit_blacklist(message):
    if await check_cancel_command(message):
        return
    val = message.text.strip()
    await set_setting("blacklist_words", val)
    await bot.send_message(message.chat.id, "✅ Чорний список слів успішно оновлено!", reply_markup=await main_menu_keyboard(message.from_user.id))

async def process_edit_breaking(message):
    if await check_cancel_command(message):
        return
    val = message.text.strip()
    await set_setting("breaking_keywords", val)
    await bot.send_message(message.chat.id, "✅ Ключові слова для Breaking News успішно оновлено!", reply_markup=await main_menu_keyboard(message.from_user.id))

async def process_edit_proxies(message):
    if await check_cancel_command(message):
        return
    val = message.text.strip()
    if val.lower() in ["none", "empty", "очистити", "-", "видалити"]:
        val = ""
    await set_setting("proxies", val)
    await bot.send_message(message.chat.id, "✅ Список проксі успешно оновлено!", reply_markup=await main_menu_keyboard(message.from_user.id))

def format_proxy(proxy_str: str) -> str:
    """Ensures the proxy string has a scheme, defaulting to http://."""
    p = proxy_str.strip()
    if not (p.startswith("http://") or p.startswith("https://") or p.startswith("socks5://") or p.startswith("socks4://")):
        return "http://" + p
    return p

async def check_proxies_job(chat_id, message_id, proxies_list):
    """Worker job running in a background thread to check configured proxies."""
    results = []
    total = len(proxies_list)
    
    for idx, proxy in enumerate(proxies_list, 1):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"⏳ <b>Перевірка проксі у процесі...</b>\n\nПеревірено: <b>{idx - 1} / {total}</b>\nПоточний проксі: <code>{proxy}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
            
        formatted_proxy = format_proxy(proxy)
        try:
            start_time = time.time()
            response = await asyncio.to_thread(requests.get,
                "https://api.coingecko.com/api/v3/ping",
                proxies={"http": formatted_proxy, "https": formatted_proxy},
                timeout=5
            )
            latency = int((time.time() - start_time) * 1000)
            if response.status_code == 200:
                results.append((proxy, True, f"✅ Працює ({latency}ms)"))
            else:
                results.append((proxy, False, f"⚠️ Помилка (код: {response.status_code})"))
        except Exception as e:
            results.append((proxy, False, f"❌ Не працює ({type(e).__name__})"))
            
    working_count = sum(1 for r in results if r[1])
    lines = []
    for proxy, is_ok, status in results:
        lines.append(f"• <code>{proxy}</code>: {status}")
        
    result_text = (
        f"🌐 <b>Результати перевірки проксі:</b>\n\n"
        + "\n".join(lines) + "\n\n"
        f"📊 Усього: <b>{total}</b> | Працює: <b>{working_count}</b>\n\n"
        f"<i>Бот автоматично ротує проксі зі списку. Якщо всі проксі не працюють, використовується пряме підключення.</i>"
    )
    
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("🔄 Повторити перевірку", callback_data="check_proxies"),
        telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="advanced_settings")
    )
    
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=result_text,
            parse_mode="HTML",
            reply_markup=markup
        )
    except Exception:
        try:
            await bot.send_message(chat_id, result_text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass

async def process_add_admin_btn(message):
    if await check_cancel_command(message):
        return
    try:
        parts = message.text.strip().split()
        if len(parts) < 1:
            raise ValueError
        user_id = int(parts[0])
        username = parts[1] if len(parts) > 1 else ""
        if username.startswith("@"):
            username = username[1:]
        if await add_admin(user_id, username):
            await bot.send_message(message.chat.id, f"✅ Адміністратора з ID <code>{user_id}</code> (@{username or 'немає'}) успішно додано!", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
        else:
            await bot.send_message(message.chat.id, "❌ Не вдалося додати адміністратора.")
    except ValueError:
        await bot.send_message(message.chat.id, "❌ Помилка. Будь ласка, введіть числовий ID та Username (опціонально) через пробіл:")
        bot.register_next_step_handler(message, process_add_admin_btn)

async def process_delete_admin_btn(message):
    if await check_cancel_command(message):
        return
    try:
        user_id = int(message.text.strip())
        if await delete_admin(user_id):
            await bot.send_message(message.chat.id, f"✅ Адміністратора з ID <code>{user_id}</code> успішно видалено.", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
        else:
            await bot.send_message(message.chat.id, f"❌ Адміністратора з ID <code>{user_id}</code> не знайдено в базі.", parse_mode="HTML")
    except ValueError:
        await bot.send_message(message.chat.id, "❌ Помилка. Будь ласка, введіть числовий ID адміністратора:")
        bot.register_next_step_handler(message, process_delete_admin_btn)

async def handle_list_admins(message):
    admins = await get_admins()
    owner_id = await get_owner_id()
    text = f"👑 <b>Власник:</b> <code>{owner_id}</code>\n\n👮‍♂️ <b>Адміністратори:</b>\n"
    if not admins:
        text += "Немає додаткових адміністраторів."
    else:
        for idx, adm in enumerate(admins, 1):
            text += f"{idx}. ID: <code>{adm['user_id']}</code> | @{adm['username'] or 'немає'} (доданий: {adm['added_at']})\n"
            
    markup = telebot.types.InlineKeyboardMarkup()
    if message.from_user.id == owner_id:
        markup.add(
            telebot.types.InlineKeyboardButton("➕ Додати адміна", callback_data="add_admin_btn"),
            telebot.types.InlineKeyboardButton("❌ Видалити адміна", callback_data="delete_admin_btn")
        )
        await bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
    else:
        await bot.send_message(message.chat.id, text, parse_mode="HTML")

async def handle_add_admin(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await bot.reply_to(message, "⚠️ Використовуйте: <code>/add_admin [ID] [Username]</code>", parse_mode="HTML")
            return
        user_id = int(parts[1])
        username = parts[2] if len(parts) > 2 else ""
        if await add_admin(user_id, username):
            await bot.reply_to(message, f"✅ Користувача <code>{user_id}</code> (@{username or 'немає'}) додано до списку адміністраторів.", parse_mode="HTML")
        else:
            await bot.reply_to(message, "❌ Не вдалося додати адміністратора.")
    except Exception as e:
        await bot.reply_to(message, f"❌ Помилка: {e}")

async def handle_delete_admin(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await bot.reply_to(message, "⚠️ Використовуйте: <code>/delete_admin [ID]</code>", parse_mode="HTML")
            return
        user_id = int(parts[1])
        if await delete_admin(user_id):
            await bot.reply_to(message, f"✅ Адміністратора з ID <code>{user_id}</code> видалено.", parse_mode="HTML", reply_markup=await main_menu_keyboard(message.from_user.id))
        else:
            await bot.reply_to(message, f"❌ Адміністратора з ID <code>{user_id}</code> не знайдено.", parse_mode="HTML")
    except Exception as e:
        await bot.reply_to(message, f"❌ Помилка: {e}")

async def handle_list_feeds(message):
    feeds = await get_rss_feeds()
    if not feeds:
        text = "🔗 <b>Джерела RSS-стрічок:</b>\n\n📭 Немає підключених джерел."
    else:
        lines = []
        for idx, f in enumerate(feeds, 1):
            lines.append(f"{idx}. <b>{f['name']}</b>\n   <code>{f['url']}</code>")
        text = "🔗 <b>Активні RSS-джерела:</b>\n\n" + "\n\n".join(lines)
    await bot.send_message(message.chat.id, text, parse_mode="HTML")

from typing import Tuple

def _sync_get_analytics_db_data(ch_id: str, member_count: int) -> Tuple[Dict[str, Any], int]:
    """Saves member count stats and retrieves analytics history and total post counts from database."""
    from db import record_channel_stats, get_channel_analytics
    record_channel_stats(ch_id, member_count)
    analytics = get_channel_analytics(ch_id)
    with _sync_get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM published_posts WHERE was_posted = 1")
        total_posts = cursor.fetchone()[0]
    return analytics, total_posts

async def handle_analytics(message):
    channels = await get_channels()
    if not channels:
        await bot.reply_to(message, "📢 <b>Канали не підключені.</b>\nДодайте хоча б один канал для відстеження аналітики.", parse_mode="HTML")
        return
        
    response_lines = ["📊 <b>Аналітика підключених каналів:</b>\n"]
    
    for ch in channels:
        ch_id = ch["channel_id"]
        ch_name = ch["name"]
        
        try:
            member_count = await bot.get_chat_member_count(ch_id)
            
            # Asynchronously offload blocking db execution
            analytics, total_posts = await run_db(_sync_get_analytics_db_data, ch_id, member_count)
            
            current = analytics.get("current", member_count)
            growth = analytics.get("growth_7d", 0)
            
            growth_str = f"+{growth}" if growth > 0 else f"{growth}"
            
            response_lines.append(
                f"📢 <b>{ch_name}</b> (<code>{ch_id}</code>):\n"
                f"👥 Підписників: <b>{current}</b> ({growth_str} за 7 днів)\n"
                f"✍️ Опубліковано постів (усього): <b>{total_posts}</b>\n"
            )
        except Exception as e:
            logging.error(f"Error fetching analytics for channel {ch_id}: {e}")
            response_lines.append(
                f"📢 <b>{ch_name}</b> (<code>{ch_id}</code>):\n"
                f"⚠️ Помилка зчитування даних Telegram API: {e}\n"
            )
            
    response_lines.append(
        "<i>Примітка: Через обмеження Telegram Bot API, статистика переглядів постів (ER) недоступна ботам. Відображається лише динаміка підписників та загальна кількість постів.</i>"
    )
    
    await bot.send_message(message.chat.id, "\n".join(response_lines), parse_mode="HTML")

# --- CALLBACK QUERY HANDLER FOR INLINE BUTTONS ---

@bot.callback_query_handler(func=lambda call: True)
async def handle_inline_callbacks(call):
    user_id = call.from_user.id
    if not await is_admin(user_id):
        await bot.answer_callback_query(call.id, "🔒 Доступ обмежено!", show_alert=True)
        return
        
    action = call.data
    chat_id = call.message.chat.id
    
    # Settings Modifications
    if action == "set_news_count":
        msg = await bot.send_message(chat_id, "🔢 Введіть нову кількість новин на день (від 1 до 30):")
        bot.register_next_step_handler(msg, process_set_news_count)
        await bot.answer_callback_query(call.id)
    elif action == "set_act_count":
        msg = await bot.send_message(chat_id, "🔢 Введіть нову кількість активностей на день (від 1 to 20):")
        bot.register_next_step_handler(msg, process_set_act_count)
        await bot.answer_callback_query(call.id)
    elif action == "set_start_hour":
        msg = await bot.send_message(chat_id, "⏰ Введіть годину початку активного вікна (наприклад, 9):")
        bot.register_next_step_handler(msg, process_set_start_hour)
        await bot.answer_callback_query(call.id)
    elif action == "set_end_hour":
        msg = await bot.send_message(chat_id, "⏰ Введіть годину кінця активного вікна (наприклад, 23):")
        bot.register_next_step_handler(msg, process_set_end_hour)
        await bot.answer_callback_query(call.id)
        
    # Channels Management
    elif action == "add_channel":
        msg = await bot.send_message(chat_id, "📢 Введіть ID та Назву каналу через пробіл (наприклад: <code>-1001668264285 МійКанал</code>):", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_add_channel)
        await bot.answer_callback_query(call.id)
    elif action == "delete_channel":
        msg = await bot.send_message(chat_id, "❌ Введіть точний ID каналу, який потрібно видалити:")
        bot.register_next_step_handler(msg, process_delete_channel)
        await bot.answer_callback_query(call.id)
        
    # Feeds Management
    elif action == "add_feed":
        msg = await bot.send_message(chat_id, "🔗 Введіть Назву та URL RSS-ленти через пробіл (наприклад: <code>CoinDesk https://coindesk.com/arc/outboundfeed/rss/</code>):", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_add_feed)
        await bot.answer_callback_query(call.id)
    elif action == "delete_feed":
        msg = await bot.send_message(chat_id, "❌ Введіть точний URL джерела, яке потрібно видалити:")
        bot.register_next_step_handler(msg, process_delete_feed)
        await bot.answer_callback_query(call.id)
    elif action == "advanced_settings":
        text, markup = await get_advanced_settings_menu()
        await bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=text, parse_mode="HTML", reply_markup=markup)
        await bot.answer_callback_query(call.id)
    elif action == "back_to_settings":
        text, markup = await get_settings_menu()
        await bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=text, parse_mode="HTML", reply_markup=markup)
        await bot.answer_callback_query(call.id)
    elif action == "edit_blacklist":
        msg = await bot.send_message(chat_id, "🚫 Введіть слова для чорного списку через кому (наприклад: <code>presale, scam, airdrop</code>):", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_edit_blacklist)
        await bot.answer_callback_query(call.id)
    elif action == "edit_breaking":
        msg = await bot.send_message(chat_id, "🚨 Введіть ключові слова для Breaking News через кому (наприклад: <code>sec, hack, exploit</code>):", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_edit_breaking)
        await bot.answer_callback_query(call.id)
    elif action == "edit_proxies":
        msg = await bot.send_message(chat_id, "🌐 Введіть список проксі через кому або новий рядок у форматі <code>http://user:pass@ip:port</code> (або напишіть 'видалити' для очищення):", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_edit_proxies)
        await bot.answer_callback_query(call.id)
    elif action == "check_proxies":
        proxies_str = await get_setting("proxies", "").strip()
        if not proxies_str:
            await bot.answer_callback_query(call.id, "🔌 Список проксі порожній. Бот використовує пряме підключення.", show_alert=True)
        else:
            proxies_list = [p.strip() for p in proxies_str.replace("\n", ",").split(",") if p.strip()]
            await bot.answer_callback_query(call.id, "⏳ Початок перевірки проксі...")
            create_tracked_task(
                check_proxies_job(chat_id, call.message.message_id, proxies_list)
            )
    elif action == "add_admin_btn":
        owner_id = await get_owner_id()
        if user_id != owner_id:
            await bot.answer_callback_query(call.id, "🔒 Тільки власник може додавати адміністраторів!", show_alert=True)
        else:
            await bot.answer_callback_query(call.id)
            msg = await bot.send_message(chat_id, "👥 Введіть ID та Username нового адміністратора через пробіл (наприклад: <code>123456789 username</code>):", parse_mode="HTML")
            bot.register_next_step_handler(msg, process_add_admin_btn)
    elif action == "delete_admin_btn":
        owner_id = await get_owner_id()
        if user_id != owner_id:
            await bot.answer_callback_query(call.id, "🔒 Тільки власник може видаляти адміністраторів!", show_alert=True)
        else:
            await bot.answer_callback_query(call.id)
            msg = await bot.send_message(chat_id, "❌ Введіть точний ID адміністратора, якого потрібно видалити:")
            bot.register_next_step_handler(msg, process_delete_admin_btn)
        
    # Test Actions (Dry Run)
    elif action == "t_news":
        await bot.send_message(chat_id, "⏳ Тест: Збір та підготовка новини...")
        create_tracked_task(run_publish_cycle_by_type("news", chat_id))
        await bot.answer_callback_query(call.id)
    elif action == "t_activity":
        await bot.send_message(chat_id, "⏳ Тест: Збір та підготовка активності...")
        create_tracked_task(run_publish_cycle_by_type("activity", chat_id))
        await bot.answer_callback_query(call.id)
    elif action == "t_analysis":
        await bot.send_message(chat_id, "⏳ Тест: Збір ринкових даних та аналітики...")
        create_tracked_task(run_market_analysis_cycle(test_chat_id=chat_id))
        await bot.answer_callback_query(call.id)
        
    # Force Publish Actions (Live)
    elif action == "p_news":
        await bot.send_message(chat_id, "⏳ Публікація Новини у канали...")
        create_tracked_task(run_publish_cycle_by_type("news", None))
        await bot.answer_callback_query(call.id)
    elif action == "p_activity":
        await bot.send_message(chat_id, "⏳ Публікація Активності у канали...")
        create_tracked_task(run_publish_cycle_by_type("activity", None))
        await bot.answer_callback_query(call.id)
    elif action == "p_analysis":
        await bot.send_message(chat_id, "⏳ Публікація Analizu Rinku u kanaly...")
        create_tracked_task(run_market_analysis_cycle(None))
        await bot.answer_callback_query(call.id)

# --- REPLY KEYBOARD COMMAND HANDLERS ---

@bot.message_handler(commands=["start", "help"])
async def handle_start(message):
    user_id = message.from_user.id
    
    is_new_owner = False
    async with _owner_bootstrap_lock:
        owner_id = await get_owner_id()
        if owner_id is None:
            await set_owner_id(user_id)
            owner_id = user_id
            is_new_owner = True
            
    # Bootstrap Owner
    if is_new_owner:
        await bot.send_message(
            message.chat.id, 
            f"👑 <b>Вітаємо!</b>\nВи автоматично зареєстровані як <b>Власник</b> цього бота (Ваш ID: <code>{user_id}</code>).", 
            parse_mode="HTML",
            reply_markup=await main_menu_keyboard(user_id)
        )
        return
        
    if not await is_admin(user_id):
        await bot.reply_to(
            message,
            f"👋 <b>Привіт! Я Telegram-бот автопублікації крипто-новин.</b>\n\n"
            f"🔒 Доступ до адмін-панелі обмежено.\n"
            f"Ваш Telegram ID: <code>{user_id}</code>.\n"
            f"Передайте цей ID власнику каналу для отримання доступу.",
            parse_mode="HTML"
        )
        return

    # Admin Help
    help_text = (
        "👋 <b>Панель Адміністратора (v3.5):</b>\n\n"
        "Усіма функціями бота можна керувати за допомогою зручних кнопок меню нижче. "
        "Вам більше не потрібно вводити текстові команди вручну!\n\n"
        "📊 <b>Доступні розділи меню:</b>\n"
        "• <b>📊 Статус</b> — Перегляд розкладу публікацій на сьогодні та статусу виконання\n"
        "• <b>📈 Аналітика</b> — Статистика підписників та загальна кількість опублікованих постів у каналах\n"
        "• <b>⚙️ Налаштування</b> — Кількість постів на день та години активного вікна бота\n"
        "• <b>🔄 Оновити розклад</b> — Примусово перегенерувати розклад постів на сьогодні\n"
        "• <b>📢 Канали</b> — Додавання та видалення каналів для публікації\n"
        "• <b>🔗 RSS-Джерела</b> — Керування RSS-стрічками новин для збору інформації\n"
        "• <b>📝 Тест-Пости</b> — Генерація тестового поста у цей чат (без публікації у канали)\n"
        "• <b>⏳ Опублікувати зараз</b> — Примусова генерація та негайна публікація обраного типу поста у канали\n\n"
        "👑 <b>Панель Власника (показується тільки вам):</b>\n"
        "• <b>👥 Адміністратори</b> — Перегляд списку адміністраторів та надання/відкликання доступу\n"
        "• <b>💾 Резервна копія БД</b> — Скачати файл бази даних (data.db)\n"
        "• <b>ℹ️ Довідка</b> — Показати це повідомлення з описом кнопок"
    )
    await bot.send_message(message.chat.id, help_text, parse_mode="HTML", reply_markup=await main_menu_keyboard(user_id))

@bot.message_handler(commands=["analytics"])
@admin_only
async def handle_analytics_command(message):
    await handle_analytics(message)

@bot.message_handler(commands=["regenerate"])
@admin_only
async def handle_regenerate(message):
    await generate_daily_schedule(force=True)
    await bot.reply_to(message, "🔄 Розклад на сьогодні успішно перегенеровано на основі актуальних налаштувань!")

# Map Text Buttons
@bot.message_handler(func=lambda message: message.text in [
    "📊 Статус", "📈 Аналітика", "⚙️ Налаштування", "🔄 Оновити розклад", 
    "📢 Канали", "🔗 RSS-Джерела", "📝 Тест-Пости", "⏳ Опублікувати зараз", 
    "👥 Адміністратори", "💾 Резервна копія БД", "ℹ️ Довідка"
])
@admin_only
async def handle_menu_buttons(message):
    btn_text = message.text
    
    if btn_text == "📊 Статус":
        await handle_status(message)
        
    elif btn_text == "📈 Аналітика":
        await handle_analytics(message)
        
    elif btn_text == "⚙️ Налаштування":
        text, markup = await get_settings_menu()
        await bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        
    elif btn_text == "🔄 Оновити розклад":
        await handle_regenerate(message)
        
    elif btn_text == "📢 Канали":
        text, markup = await get_channels_menu()
        await bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        
    elif btn_text == "🔗 RSS-Джерела":
        text, markup = await get_feeds_menu()
        await bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        
    elif btn_text == "👥 Адміністратори":
        await handle_list_admins(message)
        
    elif btn_text == "💾 Резервна копія БД":
        await handle_backup_db(message)
        
    elif btn_text == "ℹ️ Довідка":
        await handle_start(message)
        
    elif btn_text == "📝 Тест-Пости":
        await bot.send_message(
            message.chat.id, 
            "📝 <b>Оберіть тип тестової публікації:</b>\n(Пост прийде сюди у чат без відправки в канали та запису в БД)",
            parse_mode="HTML",
            reply_markup=get_test_menu()
        )
        
    elif btn_text == "⏳ Опублікувати зараз":
        await bot.send_message(
            message.chat.id, 
            "⏳ <b>Оберіть тип примусової публікації у канали:</b>",
            parse_mode="HTML",
            reply_markup=get_publish_menu()
        )

def _sync_get_status_data(today_iso: str):
    """Retrieves status numbers and daily schedule from SQLite database safely in worker thread."""
    with _sync_get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM published_posts")
        db_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM rss_feeds")
        feed_count = cursor.fetchone()[0]
        
        cursor.execute(
            "SELECT post_time, post_type, is_executed FROM daily_schedule "
            "WHERE date(post_time) = ? ORDER BY post_time ASC",
            (today_iso,)
        )
        rows = [dict(row) for row in cursor.fetchall()]
    return db_count, feed_count, rows

# Fallback direct commands (keep them active for compatibility)
@bot.message_handler(commands=["status"])
@admin_only
async def handle_status(message):
    try:
        now = get_berlin_now()
        today = now.date()
        
        # Ensure daily schedule exists for today in DB
        await generate_daily_schedule()
        
        db_count, feed_count, rows = await run_db(_sync_get_status_data, today.isoformat())
        
        news_list = []
        activity_list = []
        analysis_list = []
        
        for row in rows:
            dt = datetime.strptime(row["post_time"], '%Y-%m-%d %H:%M:%S')
            time_str = dt.strftime('%H:%M')
            executed_indicator = '✓' if row["is_executed"] == 1 else ''
            formatted = f"{time_str}{executed_indicator}"
            
            ptype = row["post_type"]
            if ptype == "news":
                news_list.append(formatted)
            elif ptype == "activity":
                activity_list.append(formatted)
            elif ptype == "analysis":
                analysis_list.append(formatted)
                
        news_str = ", ".join(news_list) if news_list else "немає"
        act_str = ", ".join(activity_list) if activity_list else "немає"
        an_str = ", ".join(analysis_list) if analysis_list else "немає"
        
        status_msg = (
            f"🤖 <b>Статус Crypto Publisher Bot:</b>\n\n"
            f"📊 Посилань в БД: <code>{db_count}</code> | Джерел: <code>{feed_count}</code>\n"
            f"📅 Розклад на сьогодні ({today.strftime('%Y-%m-%d')}):\n"
            f"📰 <b>Новини:</b> {news_str}\n"
            f"🎁 <b>Активності:</b> {act_str}\n"
            f"📊 <b>Аналітика:</b> {an_str}\n\n"
            f"Системний час: <code>{now.strftime('%H:%M:%S')}</code>"
        )
        await bot.reply_to(message, status_msg, parse_mode="HTML")
    except Exception as e:
        await bot.reply_to(message, f"❌ Помилка: {e}")

@bot.message_handler(commands=["list_feeds"])
@admin_only
async def handle_list_feeds_command(message):
    await handle_list_feeds(message)

# --- OWNER ONLY: ACCESS MANAGEMENT COMMANDS ---

@bot.message_handler(commands=["list_admins"])
@owner_only
async def handle_list_admins_cmd(message):
    await handle_list_admins(message)

@bot.message_handler(commands=["add_admin"])
@owner_only
async def handle_add_admin_cmd(message):
    await handle_add_admin(message)

@bot.message_handler(commands=["delete_admin"])
@owner_only
async def handle_delete_admin_cmd(message):
    await handle_delete_admin(message)

@bot.message_handler(commands=["backup_db"])
@owner_only
async def handle_backup_db(message):
    try:
        from config import DB_PATH
        if os.path.exists(DB_PATH):
            with open(DB_PATH, "rb") as db_file:
                await bot.send_document(
                    chat_id=message.chat.id,
                    document=db_file,
                    visible_file_name="data.db",
                    caption="📦 <b>Резервна копія бази даних SQLite (data.db)</b>",
                    parse_mode="HTML"
                )
        else:
            await bot.reply_to(message, "❌ Файл бази даних не знайдено.")
    except Exception as e:
        await bot.reply_to(message, f"❌ Помилка при створенні бекапу: {e}")

async def main():
    logging.info("Starting bot services (v3.5 with async architecture, security, keyboards, and Render support)...")
    
    # Ensure orchestrator is initialized
    from processor import orchestrator
    await orchestrator._ensure_initialized()
    
    # 1. Start Web Server for Render Health Checks (synchronous daemon thread)
    web_t = threading.Thread(target=run_web_server, daemon=True)
    web_t.start()
    
    # 2. Start Self-pinging keep-alive task
    create_tracked_task(keep_alive_task())
    
    # 3. Start background scheduler task
    create_tracked_task(scheduler_task())
    
    # 3b. Start breaking news monitor task
    create_tracked_task(breaking_news_monitor_task())
    
    # 4. Start telegram bot polling
    logging.info("Telegram Bot starts polling...")
    try:
        await bot.infinity_polling()
    except Exception as e:
        logging.error(f"Bot polling crashed: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Stopping bot...")
