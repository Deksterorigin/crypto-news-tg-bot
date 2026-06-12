import telebot
import time
import random
import logging
import threading
from datetime import datetime, timedelta
from config import BOT_TOKEN, CHANNEL_ID
from fetcher import fetch_all_new_items, extract_image_url
from processor import generate_single_post
from db import mark_as_published, get_connection

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Initialize Telegram Bot
bot = telebot.TeleBot(BOT_TOKEN)

# Thread-safe locks and global state
schedule_lock = threading.Lock()
scheduled_times = []      # List of datetime objects for today's postings
scheduled_date = None      # date object tracking the current day of the schedule

def generate_daily_schedule():
    """
    Generates a random number of post times (between 6 and 12) for today
    between 10:00 and 22:00. Spaced out using equal time segments.
    """
    global scheduled_times, scheduled_date
    with schedule_lock:
        now = datetime.now()
        today = now.date()
        
        # Random number of posts today (6 to 12)
        n_posts = random.randint(6, 12)
        
        # 10:00 AM to 10:00 PM is 12 hours = 720 minutes.
        # We divide 720 minutes into n_posts segments, and pick a random time in each.
        segment_len = 720.0 / n_posts
        
        times = []
        for i in range(n_posts):
            start_m = int(i * segment_len)
            end_m = int((i + 1) * segment_len) - 1
            
            # Select a random minute offset within this segment
            offset_m = random.randint(start_m, end_m)
            
            # 10:00 AM is 600 minutes from midnight
            time_offset = timedelta(minutes=600 + offset_m)
            t = datetime.combine(today, datetime.min.time()) + time_offset
            times.append(t)
            
        # Sort chronologically
        times.sort()
        
        scheduled_times = times
        scheduled_date = today
        
        logging.info(f"Generated daily schedule with {n_posts} posts for {today}: {[t.strftime('%H:%M:%S') for t in scheduled_times]}")

def run_publish_cycle() -> bool:
    """
    Core publisher routine. Fetches new items, selects the single best post via Gemini,
    extracts the article's preview image, and posts it to Telegram.
    """
    logging.info("Starting automated publish cycle...")
    try:
        # 1. Fetch new items from RSS feeds
        items = fetch_all_new_items()
        if not items:
            logging.info("No new news or projects found during this cycle.")
            return False
            
        # 2. Select and format the top post using Gemini API
        selected_link, post_text = generate_single_post(items)
        
        # 3. Publish to Telegram Channel
        if post_text and selected_link:
            logging.info(f"Selected article: {selected_link}")
            
            # Scrape og:image from the article
            img_url = extract_image_url(selected_link)
            
            if img_url:
                logging.info(f"Publishing photo post to channel {CHANNEL_ID} with image: {img_url}")
                try:
                    bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=img_url,
                        caption=post_text,
                        parse_mode="HTML"
                    )
                    logging.info("Photo post successfully published.")
                except Exception as photo_err:
                    logging.error(f"Failed to send photo post: {photo_err}. Falling back to text-only.")
                    bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=post_text,
                        parse_mode="HTML",
                        disable_web_page_preview=False
                    )
            else:
                logging.info(f"No image found. Publishing text-only post to channel {CHANNEL_ID}")
                bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post_text,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )
                logging.info("Text post successfully published.")
                
        # 4. Mark all fetched items in this cycle as processed in SQLite.
        # This keeps the channel's slate clean for the next hourly check.
        for item in items:
            mark_as_published(item["link"], item["title"], item["source"])
            
        logging.info(f"Marked {len(items)} items as processed in the database.")
        return True
        
    except Exception as e:
        logging.error(f"Error in run_publish_cycle: {e}")
        return False

def scheduler_thread():
    """Background thread that manages and executes the schedule."""
    global scheduled_times, scheduled_date
    logging.info("Scheduler thread started.")
    
    # Initialize schedule at start
    generate_daily_schedule()
    
    # Track executed times for today
    executed_times = set()
    
    # If bot starts mid-day, mark past times as executed to prevent backlog
    now = datetime.now()
    with schedule_lock:
        for t in scheduled_times:
            if t < now:
                executed_times.add(t)
                logging.info(f"Scheduled time {t.strftime('%H:%M:%S')} is in the past. Skipping.")
                
    while True:
        try:
            now = datetime.now()
            today = now.date()
            
            # Check if day changed
            if today != scheduled_date:
                generate_daily_schedule()
                executed_times.clear()
                
            # Check if any scheduled time is reached
            to_trigger = []
            with schedule_lock:
                for t in scheduled_times:
                    if t <= now and t not in executed_times:
                        to_trigger.append(t)
                        
            for t in to_trigger:
                logging.info(f"Time {t.strftime('%H:%M:%S')} reached. Running publish cycle.")
                run_publish_cycle()
                executed_times.add(t)
                
            time.sleep(30) # check every 30 seconds
        except Exception as e:
            logging.error(f"Error in scheduler_thread: {e}")
            time.sleep(60)

# --- TELEGRAM BOT COMMAND HANDLERS ---

@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    help_text = (
        "👋 <b>Привіт! Я оновлений Telegram-бот для автопублікації крипто-новин.</b>\n\n"
        "Я збираю новини з Cointelegraph, Decrypt, a16z, Coinbase та інших блогів, "
        "вибираю найкращу новину, знаходжу її оригінальне прев'ю-зображення, "
        "перекладаю на українську з хэштегами та публікую її.\n\n"
        "⏰ Бот публікує випадкову кількість постів (<b>від 6 до 12 разів на день</b>) у проміжку з 10:00 до 22:00.\n\n"
        "<b>Доступні команди:</b>\n"
        "📊 /status - Перевірити розклад публікацій на сьогодні та статистику бази даних.\n"
        "⏳ /publish - Примусово запустити пошук новин та опублікувати найкращу новину в канал зараз.\n"
        "📝 /test - Тестовий запуск: отримати новину з картинкою прямо в цей чат (без публікації у канал та без запису в БД).\n"
        "ℹ️ /help - Показати цю довідку."
    )
    bot.reply_to(message, help_text, parse_mode="HTML")

@bot.message_handler(commands=["status"])
def handle_status(message):
    global scheduled_times, scheduled_date
    try:
        now = datetime.now()
        
        # Fetch stats from DB
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM published_posts")
            db_count = cursor.fetchone()[0]
            
        with schedule_lock:
            times_str = "\n".join([
                f"⏰ {t.strftime('%H:%M')} {'(вже виконано)' if t < now else '(очікується)'}"
                for t in scheduled_times
            ])
            n_scheduled = len(scheduled_times)
            curr_date = scheduled_date
            
        status_msg = (
            f"🤖 <b>Статус Crypto Publisher Bot (v2):</b>\n\n"
            f"📊 Всього оброблених посилань в БД: <code>{db_count}</code>\n"
            f"📢 ID каналу: <code>{CHANNEL_ID}</code>\n"
            f"📅 Розклад на сьогодні ({curr_date}): {n_scheduled} постів\n{times_str}\n\n"
            f"Поточний системний час: <code>{now.strftime('%H:%M:%S')}</code>"
        )
        bot.reply_to(message, status_msg, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка при отриманні статусу: {e}")

@bot.message_handler(commands=["publish"])
def handle_publish(message):
    bot.reply_to(message, "⏳ Починаю збір новин, вибір найкращої та публікацію в канал...")
    
    def worker():
        try:
            items = fetch_all_new_items()
            if not items:
                bot.send_message(message.chat.id, "❌ Нових матеріалів для публікації не знайдено.")
                return
                
            selected_link, post_text = generate_single_post(items)
            if post_text and selected_link:
                img_url = extract_image_url(selected_link)
                
                if img_url:
                    bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=img_url,
                        caption=post_text,
                        parse_mode="HTML"
                    )
                else:
                    bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=post_text,
                        parse_mode="HTML",
                        disable_web_page_preview=False
                    )
                    
                # Mark all as published
                for item in items:
                    mark_as_published(item["link"], item["title"], item["source"])
                    
                bot.send_message(message.chat.id, f"✅ Публікацію успішно зроблено! Оброблено {len(items)} новин, обрано: {selected_link}")
            else:
                for item in items:
                    mark_as_published(item["link"], item["title"], item["source"])
                bot.send_message(message.chat.id, "⚠️ Gemini відфільтрував усі новини як нецікаві. Матеріали відмічено як прочитані.")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Помилка під час примусової публікації: {e}")
            
    threading.Thread(target=worker).start()

@bot.message_handler(commands=["test"])
def handle_test(message):
    bot.reply_to(message, "⏳ Запускаю тестовий збір (результат з картинкою буде надіслано сюди, БД та канал не зміняться)...")
    
    def worker():
        try:
            items = fetch_all_new_items()
            if not items:
                bot.send_message(message.chat.id, "❌ Нових новин для тестового збору не знайдено.")
                return
                
            selected_link, post_text = generate_single_post(items)
            if post_text and selected_link:
                img_url = extract_image_url(selected_link)
                
                # Report preview details
                bot.send_message(
                    message.chat.id, 
                    f"📝 <b>Результат тестового збору:</b>\n\nSelected link: {selected_link}\nExtracted Image: {img_url}",
                    parse_mode="HTML"
                )
                
                if img_url:
                    bot.send_photo(
                        chat_id=message.chat.id,
                        photo=img_url,
                        caption=post_text,
                        parse_mode="HTML"
                    )
                else:
                    bot.send_message(
                        chat_id=message.chat.id,
                        text=post_text,
                        parse_mode="HTML"
                    )
            else:
                bot.send_message(message.chat.id, "⚠️ Gemini відфільтрував новини як нецікаві. Тестовий пост пустий.")
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Помилка під час тесту: {e}")
            
    threading.Thread(target=worker).start()

if __name__ == "__main__":
    logging.info("Starting bot services (v2)...")
    
    # 1. Start background scheduler thread
    sched_t = threading.Thread(target=scheduler_thread, daemon=True)
    sched_t.start()
    
    # 2. Start telegram bot polling
    logging.info("Telegram Bot starts polling...")
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        logging.info("Stopping bot...")
