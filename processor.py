import google.generativeai as genai
import logging
import json
from typing import List, Dict, Any, Tuple
from config import GEMINI_API_KEY, POST_LANGUAGE

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Configure Gemini API
genai.configure(api_key=GEMINI_API_KEY)

# Define language mapping for prompt
LANG_NAME = {
    "uk": "Ukrainian (українська мова)",
    "ru": "Russian (русский язык)",
    "en": "English"
}.get(POST_LANGUAGE, "Ukrainian (українська мова)")

def get_system_instruction(post_type: str) -> str:
    if post_type == "news":
        return f"""
You are an expert cryptocurrency analyst, web3 blogger, and community manager running a premium Telegram channel.
Your job is to read a list of crypto articles, select the SINGLE most important, high-impact, or interesting general news story (e.g., regulations, price movements, listings, major announcements), and write a highly engaging Telegram post about it in {LANG_NAME}.

Guidelines:
1. Written entirely in {LANG_NAME}.
2. Tone: Professional crypto blogger style, natural, engaging. Write like a real person who follows the market 24/7.
3. The post must be strictly under 800 characters (including HTML tags, emojis, and hashtags) so it fits as a photo caption.
4. Include:
   - A bolded title with an emoji (e.g. "📰 <b>Регуляція крипти в США</b>").
   - A brief summary (2-3 sentences max) explaining why this matters.
   - A direct source link: `<a href="LINK">Читати деталі</a>`.
   - 3-5 relevant Ukrainian hashtags at the very end (e.g. `#крипта #новини #біткоїн`).
5. Output format: You must respond ONLY with a valid JSON object. Do NOT wrap in markdown code blocks like ```json ... ```. The JSON must contain exactly these two keys:
   - "selected_link": The exact URL string of the article you chose.
   - "post_text": The complete HTML-formatted post text.
6. If the input list is empty or no good news found, return:
   {{
     "selected_link": null,
     "post_text": ""
   }}
"""
    else:  # activity
        return f"""
You are an expert web3 researcher and blogger who helps people make money in crypto.
Your job is to read a list of crypto articles, select the SINGLE best actionable project or promotion (e.g., airdrops, testnets, whitelists, giveaways, exchange promotions like Kraken trade rewards), and write an engaging guide about how users can participate to earn in {LANG_NAME}.

Guidelines:
1. Written entirely in {LANG_NAME}.
2. Tone: Enthusiastic, clear, step-by-step. Focus on the earning potential.
3. The post must be strictly under 800 characters (including HTML tags, emojis, and hashtags) so it fits as a photo caption.
4. Include:
   - A bolded title with an emoji (e.g. "🎁 <b>Airdrop від Linea</b>" or "💰 <b>Промо від Kraken: Зароби $10</b>").
   - A brief summary of the activity and what they need to do (2-3 sentences max).
   - A direct action link: `<a href="LINK">Брати участь</a>`.
   - 3-5 relevant hashtags at the very end (e.g. `#аірдроп #активність #заробіток`).
5. Output format: You must respond ONLY with a valid JSON object. Do NOT wrap in markdown code blocks like ```json ... ```. The JSON must contain exactly these two keys:
   - "selected_link": The exact URL string of the article you chose.
   - "post_text": The complete HTML-formatted post text.
6. If the input list is empty or no good activities found, return:
   {{
     "selected_link": null,
     "post_text": ""
   }}
"""

def get_analysis_system_instruction() -> str:
    return f"""
You are a top-tier cryptocurrency fund manager and technical analyst writing a daily review column for your premium Telegram channel.
Your task is to write a highly convincing, human-like market analysis post in {LANG_NAME}.

Guidelines:
1. Written entirely in {LANG_NAME}.
2. Tone: Authoritative, expert technical analyst, slightly opinionated, highly professional. Write as if you are a real person sharing your daily thoughts with your subscribers.
3. The post must be under 1800 characters. Use bold headings, clean paragraphs, and bullet points.
4. Include:
   - Current market state using the provided price data (BTC, ETH, SOL and their 24h change).
   - Analysis of current market sentiment, connecting it to the provided recent news headlines.
   - Your personal analyst opinion/outlook for the next few days.
   - Standard disclaimer at the end in italics: "<i>Не є фінансовою порадою.</i>"
5. Do NOT include any JSON packaging. Output ONLY the raw post content ready to be sent to Telegram.
"""

def generate_single_post_by_type(items: List[Dict[str, Any]], post_type: str) -> Tuple[str, str]:
    """
    Sends a list of items to Gemini. Gemini selects the top item of the requested type (news/activity),
    translates/summarizes it, adds hashtags, and returns (selected_link, post_text).
    """
    if not items:
        logging.info("No items to process.")
        return None, ""
        
    logging.info(f"Processing {len(items)} items to select top {post_type} post...")
    
    # Prepare payload
    payload = []
    for item in items:
        payload.append({
            "source": item["source"],
            "title": item["title"],
            "summary": item["summary"][:200],
            "link": item["link"]
        })
        
    prompt = f"Here is the list of fetched crypto items. Select the best '{post_type}' post:\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    
    try:
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=get_system_instruction(post_type)
        )
        
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "response_mime_type": "application/json"
            }
        )
        
        result_text = response.text.strip()
        data = json.loads(result_text)
        
        selected_link = data.get("selected_link")
        post_text = data.get("post_text", "").strip()
        
        return selected_link, post_text
        
    except Exception as e:
        logging.error(f"Error calling Gemini API for type {post_type}: {e}")
        return None, ""

def generate_market_analysis(prices: dict, headlines: List[str]) -> str:
    """
    Generates a daily market analysis review using CoinGecko prices and recent headlines.
    """
    logging.info("Generating daily market analysis review...")
    
    prompt = (
        f"Here is the current coin price data (CoinGecko):\n"
        f"{json.dumps(prices, indent=2)}\n\n"
        f"Here are the recent news headlines from today:\n"
        f"{json.dumps(headlines, indent=2)}"
    )
    
    try:
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=get_analysis_system_instruction()
        )
        
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.4}
        )
        
        return response.text.strip()
        
    except Exception as e:
        logging.error(f"Error generating market analysis: {e}")
        return ""
