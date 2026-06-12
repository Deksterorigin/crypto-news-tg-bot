import telebot
import time
import random
import logging
import threading
import os
import requests
from datetime import datetime, timedelta
from functools import wraps
from http.server import SimpleHTTPRequestHandler, HTTPServer
from config import BOT_TOKEN, CHANNEL_ID
from fetcher import fetch_all_new_items, extract_image_url
from processor import generate_single_post_by_type, generate_market_analysis
from db import (
    mark_as_published, get_connection, add_rss_feed, delete_rss_feed, get_rss_feeds,
    get_owner_id, set_owner_id, is_admin, add_admin, delete_admin, get_admins,
    get_setting, set_setting, get_channels, add_channel, delete_channel
)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Initialize Telegram Bot
bot = telebot.TeleBot(BOT_TOKEN)

# Thread-safe locks and global state
schedule_lock = threading.Lock()
scheduled_news = []          # Datetimes for News posts
scheduled_activities = []    # Datetimes for Activity/Earning posts
scheduled_analysis = []      # Datetime for Market Analysis column
scheduled_date = None        # date object tracking the current day of the schedule

# --- SECURITY DECORATORS ---

def admin_only(func):
    """Decorator to restrict commands to authorized admins and the owner."""
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        
        # Bootstrap: if no owner exists, register the first user
        if get_owner_id() is None:
            set_owner_id(user_id)
            bot.reply_to(
                message, 
                f"👑 <b>Вітаємо!</b>\nВи автоматично зареєстровані як <b>Власник</b> цього бота (Ваш ID: <code>{user_id}</code>).", 
                parse_mode="HTML"
            )
            return func(message, *args, **kwargs)
            
        if is_admin(user_id):
            return func(message, *args, **kwargs)
        else:
            bot.reply_to(
                message, 
                f"🔒 <b>Доступ обмежено.</b>\nВаш Telegram ID: <code>{user_id}</code>.\nПопросіть власника надати вам доступ.", 
                parse_mode="HTML"
            )
    return wrapper

def owner_only(func):
    """Decorator to restrict commands strictly to the bot owner."""
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        
        if get_owner_id() is None:
            set_owner_id(user_id)
            bot.reply_to(
                message, 
                f"👑 <b>Вітаємо!</b>\nВи автоматично зареєстровані як <b>Власник</b> цього бота (Ваш ID: <code>{user_id}</code>).", 
                parse_mode="HTML"
            )
            return func(message, *args, **kwargs)
            
        if user_id == get_owner_id():
            return func(message, *args, **kwargs)
        else:
            bot.reply_to(
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
    return {
        "bitcoin": {"usd": 0.0, "usd_24h_change": 0.0},
        "ethereum": {"usd": 0.0, "usd_24h_change": 0.0},
        "solana": {"usd": 0.0, "usd_24h_change": 0.0}
    }

# --- SCHEDULER & PUBLISHING LOGIC ---

def generate_daily_schedule():
    """
    Generates schedules dynamically based on settings in SQLite database.
    Window boundaries and counts are fully customized.
    """
    global scheduled_news, scheduled_activities, scheduled_analysis, scheduled_date
    with schedule_lock:
        now = datetime.now()
        today = now.date()
        
        # Load dynamic configurations (with default Fallbacks)
        news_count = int(get_setting("news_count", "6"))
        activity_count = int(get_setting("activity_count", "4"))
        start_hour = int(get_setting("start_hour", "10"))
        end_hour = int(get_setting("end_hour", "22"))
        
        # Calculate window boundary minutes
        window_minutes = (end_hour - start_hour) * 60
        start_offset = start_hour * 60
        
        # 1. News Queue
        news_times = []
        news_segment = float(window_minutes) / news_count
        for i in range(news_count):
            offset = random.randint(int(i * news_segment), int((i + 1) * news_segment) - 1)
            news_times.append(datetime.combine(today, datetime.min.time()) + timedelta(minutes=start_offset + offset))
        news_times.sort()
        scheduled_news = news_times
        
        # 2. Activity Queue
        activity_times = []
        activity_segment = float(window_minutes) / activity_count
        for i in range(activity_count):
            offset = random.randint(int(i * activity_segment), int((i + 1) * activity_segment) - 1)
            activity_times.append(datetime.combine(today, datetime.min.time()) + timedelta(minutes=start_offset + offset))
        activity_times.sort()
        scheduled_activities = activity_times
        
        # 3. Market Analysis: 1 post (randomly in the first 2 hours of the starting hour)
        analysis_offset = random.randint(start_offset + 60, start_offset + 180)
        scheduled_analysis = [datetime.combine(today, datetime.min.time()) + timedelta(minutes=analysis_offset)]
        
        scheduled_date = today
        
        logging.info(f"Daily schedules generated dynamically for {today}:")
        logging.info(f"  News Queue ({news_count}): {[t.strftime('%H:%M') for t in scheduled_news]}")
        logging.info(f"  Activity Queue ({activity_count}): {[t.strftime('%H:%M') for t in scheduled_activities]}")
        logging.info(f"  Analysis Column (1): {[t.strftime('%H:%M') for t in scheduled_analysis]}")

def run_publish_cycle_by_type(post_type: str, test_chat_id=None) -> bool:
    """Executes a news or activity publishing cycle, posting to all registered channels."""
    logging.info(f"Running publish cycle for type: {post_type}")
    try:
        items = fetch_all_new_items()
        if not items:
            logging.info("No new items found in feeds.")
            if test_chat_id:
                bot.send_message(test_chat_id, "❌ Не знайдено нових матеріалів для сбору.")
            return False
            
        selected_link, post_text = generate_single_post_by_type(items, post_type)
        
        # Get target channels list from DB
        channels = get_channels()
        if not channels and not test_chat_id:
            logging.warning("No channels configured in SQLite db. Skipping publication.")
            return False
            
        targets = [test_chat_id] if test_chat_id else [ch["channel_id"] for ch in channels]
        
        if post_text and selected_link:
            img_url = extract_image_url(selected_link)
            
            for target in targets:
                if img_url:
                    try:
                        bot.send_photo(chat_id=target, photo=img_url, caption=post_text, parse_mode="HTML")
                        logging.info(f"Photo post for {post_type} published to {target}.")
                    except Exception as pe:
                        logging.error(f"Failed to post photo to {target}: {pe}. Falling back to text.")
                        bot.send_message(chat_id=target, text=post_text, parse_mode="HTML", disable_web_page_preview=False)
                else:
                    bot.send_message(chat_id=target, text=post_text, parse_mode="HTML", disable_web_page_preview=False)
                    logging.info(f"Text post for {post_type} published to {target}.")
                
            if not test_chat_id:
                for item in items:
                    mark_as_published(item["link"], item["title"], item["source"])
            return True
        else:
            logging.info(f"No suitable post of type {post_type} selected.")
            if test_chat_id:
                bot.send_message(test_chat_id, f"⚠️ Gemini не знайшов підходящих матеріалів для типу '{post_type}'.")
            
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
    """Executes a market analysis review, posting to all registered channels."""
    logging.info("Running market analysis cycle...")
    try:
        prices = fetch_coingecko_prices()
        items = fetch_all_new_items()
        headlines = [item["title"] for item in items[:8]] if items else ["No major breaking news headlines reported today."]
        
        analysis_text = generate_market_analysis(prices, headlines)
        
        channels = get_channels()
        if not channels and not test_chat_id:
            logging.warning("No channels configured in SQLite. Skipping analysis.")
            return False
            
        targets = [test_chat_id] if test_chat_id else [ch["channel_id"] for ch in channels]
        
        if analysis_text:
            for target in targets:
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
    
    generate_daily_schedule()
    
    executed_news = set()
    executed_activities = set()
    executed_analysis = set()
    
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

# --- KEYBOARD BUILDERS ---

def main_menu_keyboard() -> telebot.types.ReplyKeyboardMarkup:
    """Builds the persistent bottom reply menu for admins."""
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📊 Статус", "⚙️ Налаштування")
    markup.row("📢 Канали", "🔗 RSS-Джерела")
    markup.row("📝 Тест-Пости", "⏳ Опублікувати зараз")
    return markup

def get_settings_menu() -> tuple[str, telebot.types.InlineKeyboardMarkup]:
    """Generates the settings panel and inline keyboard."""
    news_count = get_setting("news_count", "6")
    activity_count = get_setting("activity_count", "4")
    start_hour = get_setting("start_hour", "10")
    end_hour = get_setting("end_hour", "22")
    
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
    return text, markup

def get_channels_menu() -> tuple[str, telebot.types.InlineKeyboardMarkup]:
    """Generates the target channels panel and inline keyboard."""
    channels = get_channels()
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

def get_feeds_menu() -> tuple[str, telebot.types.InlineKeyboardMarkup]:
    """Generates the RSS sources panel and inline keyboard."""
    feeds = get_rss_feeds()
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

def process_set_news_count(message):
    try:
        val = int(message.text.strip())
        if val <= 0 or val > 30:
            raise ValueError
        set_setting("news_count", str(val))
        bot.send_message(message.chat.id, f"✅ Кількість новин успішно змінена на <b>{val}</b> на день!", parse_mode="HTML", reply_markup=main_menu_keyboard())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Помилка. Введіть позитивне число (від 1 до 30):")
        bot.register_next_step_handler(message, process_set_news_count)

def process_set_act_count(message):
    try:
        val = int(message.text.strip())
        if val <= 0 or val > 20:
            raise ValueError
        set_setting("activity_count", str(val))
        bot.send_message(message.chat.id, f"✅ Кількість активнотей успішно змінена на <b>{val}</b> на день!", parse_mode="HTML", reply_markup=main_menu_keyboard())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Помилка. Введіть позитивне число (від 1 до 20):")
        bot.register_next_step_handler(message, process_set_act_count)

def process_set_start_hour(message):
    try:
        val = int(message.text.strip())
        end_h = int(get_setting("end_hour", "22"))
        if val < 0 or val >= end_h or val > 23:
            raise ValueError
        set_setting("start_hour", str(val))
        bot.send_message(message.chat.id, f"✅ Початок активного вікна змінено на <b>{val}:00</b>!", parse_mode="HTML", reply_markup=main_menu_keyboard())
    except ValueError:
        bot.send_message(message.chat.id, f"❌ Помилка. Введіть годину від 0 до {int(get_setting('end_hour', '22'))-1}:")
        bot.register_next_step_handler(message, process_set_start_hour)

def process_set_end_hour(message):
    try:
        val = int(message.text.strip())
        start_h = int(get_setting("start_hour", "10"))
        if val <= start_h or val > 24:
            raise ValueError
        set_setting("end_hour", str(val))
        bot.send_message(message.chat.id, f"✅ Кінець активного вікна змінено на <b>{val}:00</b>!", parse_mode="HTML", reply_markup=main_menu_keyboard())
    except ValueError:
        bot.send_message(message.chat.id, f"❌ Помилка. Введіть годину від {int(get_setting('start_hour', '10'))+1} до 24:")
        bot.register_next_step_handler(message, process_set_end_hour)

def process_add_channel(message):
    try:
        text = message.text.strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неправильний формат. Введіть ID та Назву через пробіл (наприклад: <code>-100123456789 КриптоКанал</code>):", parse_mode="HTML")
            bot.register_next_step_handler(message, process_add_channel)
            return
            
        ch_id = parts[0].strip()
        name = parts[1].strip()
        
        if add_channel(ch_id, name):
            bot.send_message(message.chat.id, f"✅ Канал <b>{name}</b> успішно додано!", parse_mode="HTML", reply_markup=main_menu_keyboard())
        else:
            bot.send_message(message.chat.id, "❌ Не вдалося додати канал. Перевірте формат.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

def process_delete_channel(message):
    ch_id = message.text.strip()
    if delete_channel(ch_id):
        bot.send_message(message.chat.id, f"✅ Канал з ID <code>{ch_id}</code> успішно видалено.", parse_mode="HTML", reply_markup=main_menu_keyboard())
    else:
        bot.send_message(message.chat.id, f"❌ Канал з ID <code>{ch_id}</code> не знайдено в базі.", parse_mode="HTML")

def process_add_feed(message):
    try:
        text = message.text.strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            bot.send_message(message.chat.id, "❌ Неправильний формат. Введіть Назву та URL через пробіл (наприклад: <code>CoinDesk https://coindesk.com/arc/outboundfeed/rss/</code>):", parse_mode="HTML")
            bot.register_next_step_handler(message, process_add_feed)
            return
            
        name = parts[0].strip()
        url = parts[1].strip()
        
        if add_rss_feed(name, url):
            bot.send_message(message.chat.id, f"✅ Джерело <b>{name}</b> додано!", parse_mode="HTML", reply_markup=main_menu_keyboard())
        else:
            bot.send_message(message.chat.id, "❌ Не вдалося додати джерело. Можливо, воно вже є.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Помилка: {e}")

def process_delete_feed(message):
    url = message.text.strip()
    if delete_rss_feed(url):
        bot.send_message(message.chat.id, f"✅ Джерело <code>{url}</code> видалено.", parse_mode="HTML", reply_markup=main_menu_keyboard())
    else:
        bot.send_message(message.chat.id, f"❌ Джерело з адресою <code>{url}</code> не знайдено.", parse_mode="HTML")

# --- CALLBACK QUERY HANDLER FOR INLINE BUTTONS ---

@bot.callback_query_handler(func=lambda call: True)
def handle_inline_callbacks(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "🔒 Доступ обмежено!", show_alert=True)
        return
        
    action = call.data
    chat_id = call.message.chat.id
    
    # Settings Modifications
    if action == "set_news_count":
        msg = bot.send_message(chat_id, "🔢 Введіть нову кількість новин на день (від 1 до 30):")
        bot.register_next_step_handler(msg, process_set_news_count)
        bot.answer_callback_query(call.id)
    elif action == "set_act_count":
        msg = bot.send_message(chat_id, "🔢 Введіть нову кількість активностей на день (від 1 to 20):")
        bot.register_next_step_handler(msg, process_set_act_count)
        bot.answer_callback_query(call.id)
    elif action == "set_start_hour":
        msg = bot.send_message(chat_id, "⏰ Введіть годину початку активного вікна (наприклад, 9):")
        bot.register_next_step_handler(msg, process_set_start_hour)
        bot.answer_callback_query(call.id)
    elif action == "set_end_hour":
        msg = bot.send_message(chat_id, "⏰ Введіть годину кінця активного вікна (наприклад, 23):")
        bot.register_next_step_handler(msg, process_set_end_hour)
        bot.answer_callback_query(call.id)
        
    # Channels Management
    elif action == "add_channel":
        msg = bot.send_message(chat_id, "📢 Введіть ID та Назву каналу через пробіл (наприклад: <code>-1001668264285 МійКанал</code>):", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_add_channel)
        bot.answer_callback_query(call.id)
    elif action == "delete_channel":
        msg = bot.send_message(chat_id, "❌ Введіть точний ID каналу, який потрібно видалити:")
        bot.register_next_step_handler(msg, process_delete_channel)
        bot.answer_callback_query(call.id)
        
    # Feeds Management
    elif action == "add_feed":
        msg = bot.send_message(chat_id, "🔗 Введіть Назву та URL RSS-ленти через пробіл (наприклад: <code>CoinDesk https://coindesk.com/arc/outboundfeed/rss/</code>):", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_add_feed)
        bot.answer_callback_query(call.id)
    elif action == "delete_feed":
        msg = bot.send_message(chat_id, "❌ Введіть точний URL джерела, яке потрібно видалити:")
        bot.register_next_step_handler(msg, process_delete_feed)
        bot.answer_callback_query(call.id)
        
    # Test Actions (Dry Run)
    elif action == "t_news":
        bot.send_message(chat_id, "⏳ Тест: Збір та підготовка новини...")
        threading.Thread(target=run_publish_cycle_by_type, args=("news", chat_id)).start()
        bot.answer_callback_query(call.id)
    elif action == "t_activity":
        bot.send_message(chat_id, "⏳ Тест: Збір та підготовка активності...")
        threading.Thread(target=run_publish_cycle_by_type, args=("activity", chat_id)).start()
        bot.answer_callback_query(call.id)
    elif action == "t_analysis":
        bot.send_message(chat_id, "⏳ Тест: Збір ринкових даних та аналітики...")
        threading.Thread(target=run_market_analysis_cycle, kwargs={"test_chat_id": chat_id}).start()
        bot.answer_callback_query(call.id)
        
    # Force Publish Actions (Live)
    elif action == "p_news":
        bot.send_message(chat_id, "⏳ Публікація Новини у канали...")
        threading.Thread(target=run_publish_cycle_by_type, args=("news",), kwargs={"test_chat_id": None}).start()
        bot.answer_callback_query(call.id)
    elif action == "p_activity":
        bot.send_message(chat_id, "⏳ Публікація Активності у канали...")
        threading.Thread(target=run_publish_cycle_by_type, args=("activity",), kwargs={"test_chat_id": None}).start()
        bot.answer_callback_query(call.id)
    elif action == "p_analysis":
        bot.send_message(chat_id, "⏳ Публікація Аналізу Ринку у канали...")
        threading.Thread(target=run_market_analysis_cycle, kwargs={"test_chat_id": None}).start()
        bot.answer_callback_query(call.id)

# --- REPLY KEYBOARD COMMAND HANDLERS ---

@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    user_id = message.from_user.id
    owner_id = get_owner_id()
    
    # Bootstrap Owner
    if owner_id is None:
        set_owner_id(user_id)
        owner_id = user_id
        bot.send_message(
            message.chat.id, 
            f"👑 <b>Вітаємо!</b>\nВи автоматично зареєстровані як <b>Власник</b> цього бота (Ваш ID: <code>{user_id}</code>).", 
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
        
    if not is_admin(user_id):
        bot.reply_to(
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
        "👋 <b>Панель Адміністратора (v3.2):</b>\n\n"
        "Використовуйте кнопки меню нижче для управління каналами, RSS-стрічками, розкладом та публікаціями.\n\n"
        "👑 <b>Команди Власника:</b>\n"
        "/list_admins — Список адміністраторів\n"
        "/add_admin [ID] [Нікнейм] — Додати адміністратора\n"
        "/delete_admin [ID] — Видалити адміністратора\n"
        "/regenerate — Примусово перегенерувати розклад на сьогодні за поточними налаштуваннями"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="HTML", reply_markup=main_menu_keyboard())

@bot.message_handler(commands=["regenerate"])
@admin_only
def handle_regenerate(message):
    generate_daily_schedule()
    bot.reply_to(message, "🔄 Розклад на сьогодні успішно перегенеровано на основі актуальних налаштувань!")

# Map Text Buttons
@bot.message_handler(func=lambda message: message.text in ["📊 Статус", "⚙️ Налаштування", "📢 Канали", "🔗 RSS-Джерела", "📝 Тест-Пости", "⏳ Опублікувати зараз"])
@admin_only
def handle_menu_buttons(message):
    btn_text = message.text
    
    if btn_text == "📊 Статус":
        handle_status(message)
        
    elif btn_text == "⚙️ Налаштування":
        text, markup = get_settings_menu()
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        
    elif btn_text == "📢 Канали":
        text, markup = get_channels_menu()
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        
    elif btn_text == "🔗 RSS-Джерела":
        text, markup = get_feeds_menu()
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        
    elif btn_text == "📝 Тест-Пости":
        bot.send_message(
            message.chat.id, 
            "📝 <b>Оберіть тип тестової публікації:</b>\n(Пост прийде сюди у чат без відправки в канали та запису в БД)",
            parse_mode="HTML",
            reply_markup=get_test_menu()
        )
        
    elif btn_text == "⏳ Опублікувати зараз":
        bot.send_message(
            message.chat.id, 
            "⏳ <b>Оберіть тип примусової публікації у канали:</b>",
            parse_mode="HTML",
            reply_markup=get_publish_menu()
        )

# Fallback direct commands (keep them active for compatibility)
@bot.message_handler(commands=["status"])
@admin_only
def handle_status(message):
    global scheduled_news, scheduled_activities, scheduled_analysis, scheduled_date
    try:
        now = datetime.now()
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
            f"🤖 <b>Статус Crypto Publisher Bot:</b>\n\n"
            f"📊 Посилань в БД: <code>{db_count}</code> | Джерел: <code>{feed_count}</code>\n"
            f"📅 Розклад на сьогодні ({curr_date}):\n"
            f"📰 <b>Новини:</b> {news_str}\n"
            f"🎁 <b>Активності:</b> {act_str}\n"
            f"📊 <b>Аналітика:</b> {an_str}\n\n"
            f"Системний час: <code>{now.strftime('%H:%M:%S')}</code>"
        )
        bot.reply_to(message, status_msg, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Помилка: {e}")

@bot.message_handler(commands=["list_feeds"])
@admin_only
def handle_list_feeds_command(message):
    handle_list_feeds(message)

# --- OWNER ONLY: ACCESS MANAGEMENT COMMANDS ---

@bot.message_handler(commands=["list_admins"])
@owner_only
def handle_list_admins_cmd(message):
    handle_list_admins(message)

@bot.message_handler(commands=["add_admin"])
@owner_only
def handle_add_admin_cmd(message):
    handle_add_admin(message)

@bot.message_handler(commands=["delete_admin"])
@owner_only
def handle_delete_admin_cmd(message):
    handle_delete_admin(message)

if __name__ == "__main__":
    logging.info("Starting bot services (v3.2 with security, keyboards, and Render support)...")
    
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
