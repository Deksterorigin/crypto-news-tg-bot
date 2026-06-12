import telebot
import sys
from config import BOT_TOKEN, CHANNEL_ID

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        
    print("=== Testing Telegram Channel Connection ===")
    print(f"Bot Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:] if len(BOT_TOKEN) > 15 else ''}")
    print(f"Channel ID: {CHANNEL_ID}")
    
    bot = telebot.TeleBot(BOT_TOKEN)
    
    test_msg = (
        "🤖 <b>Тестове повідомлення</b>\n\n"
        "Вітаємо! Ваш Telegram-бот успішно підключений до каналу та має права на публікацію."
    )
    
    try:
        print("\nSending test message to channel...")
        sent_message = bot.send_message(
            chat_id=CHANNEL_ID,
            text=test_msg,
            parse_mode="HTML"
        )
        print(f"✅ SUCCESS! Message sent. Message ID: {sent_message.message_id}")
    except Exception as e:
        print(f"❌ FAILED to send message: {e}")
        print("\nPlease make sure that:")
        print("1. The bot is added to the channel as an Administrator.")
        print("2. The bot has 'Post Messages' permission.")
        print("3. The Channel ID starts with -100 (e.g. -1001668264285).")

if __name__ == "__main__":
    main()
