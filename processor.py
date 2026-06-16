import logging
import json
import difflib
import asyncio
from typing import List, Dict, Any, Tuple, Optional, TypedDict

class Poll(TypedDict):
    question: str
    options: List[str]

class NewsResponse(TypedDict):
    selected_link: Optional[str]
    post_text: str
    poll: Optional[Poll]

class UrgencyResponse(TypedDict):
    is_really_urgent: bool

NEWS_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_link": {
            "type": "string",
            "nullable": True
        },
        "post_text": {
            "type": "string"
        },
        "poll": {
            "type": "object",
            "nullable": True,
            "properties": {
                "question": {
                    "type": "string"
                },
                "options": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            },
            "required": ["question", "options"]
        }
    },
    "required": ["selected_link", "post_text", "poll"]
}

URGENCY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_really_urgent": {
            "type": "boolean"
        }
    },
    "required": ["is_really_urgent"]
}


def robust_json_loads(text: str) -> dict:
    text_clean = text.strip()
    if text_clean.startswith("```"):
        first_nl = text_clean.find("\n")
        if first_nl != -1:
            text_clean = text_clean[first_nl:].strip()
        if text_clean.endswith("```"):
            text_clean = text_clean[:-3].strip()
            
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse JSON. Error: {e}. Raw response text was:\n{text}")
        raise e
from config import GEMINI_API_KEY, POST_LANGUAGE

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Configure Gemini Orchestrator
from gemini_orchestrator import GeminiOrchestrator, AllModelsExhaustedError
orchestrator = GeminiOrchestrator(api_key=GEMINI_API_KEY)

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
Your job is to read a list of crypto articles, select the SINGLE most important, high-impact, or interesting general news story (e.g., regulations, price movements, listings, major announcements), and write a highly engaging Telegram post about it completely in {LANG_NAME}.

CRITICAL REQUIREMENT:
Even though the input articles are in English, the generated Telegram post must be written 100% in {LANG_NAME}. You must translate the content. Do NOT output any English text in the post body, headers, or hashtags (except proper names of tokens or protocols like BTC, Linea, Binance).

CRITICAL DEDUPLICATION REQUIREMENT:
If the user provides a "recently_published_titles" list, you MUST NOT select any article that covers the same event, story, news, or announcement as any of those recently published titles. We want 100% unique news and absolutely no repeated topics!

Guidelines:
1. Written entirely in {LANG_NAME}.
2. Tone: Professional crypto blogger style, natural, engaging. Write like a real person who follows the market 24/7.
3. Crypto Slang: Adapt the text using professional crypto slang (in Ukrainian transliteration, e.g. use "улетів на місяць" or "дав ікси" instead of "ціна зросла", "дамп" instead of "падіння", "памп", "холд", "рект", "буллран", "ведмежка" etc.).
4. The post must be strictly under 950 characters (including HTML tags, emojis, and hashtags) so it fits as a photo caption.
5. Structure:
   - A bolded title with an emoji (e.g. "📰 <b>Регуляція крипти в США</b>").
   - A brief summary (2-3 sentences max) explaining why this matters.
   - Expert Commentary / Smart Comment: Include a distinct block: "🤖 <b>Думки ШІ:</b> [Analyst commentary connecting this event to potential price movements of relevant coins like BTC, ETH, or SOL, e.g. 'це подія може підштовхнути ціну SOL до зони $180, оскільки...']".
   - A direct source link: `<a href="LINK">Читати деталі</a>`.
   - 3-5 relevant Ukrainian hashtags at the very end (e.g. `#крипта #новини #біткоїн`).
6. Output format: You must respond ONLY with a valid JSON object. Do NOT wrap in markdown code blocks like ```json ... ```. The JSON must contain exactly:
   - "selected_link": The exact URL string of the article you chose.
   - "post_text": The complete HTML-formatted post text.
   - "poll": (Optional) If the news is highly controversial, important, or open-ended, include a "poll" object. If not, set to null. The poll object must contain:
     - "question": A short, engaging question (max 80 chars, e.g. "Чи вплине схвалення ETF на ціну ETH?").
     - "options": An array of exactly 2 to 4 short options (max 30 chars each, e.g. ["🚀 Так, летимо на місяць!", "📉 Ні, це sell the news", "🤷‍♂️ Подивимося"]).
7. If the input list is empty or no good news found, return:
   {{
     "selected_link": null,
     "post_text": "",
     "poll": null
   }}
"""
    elif post_type == "breaking":
        return f"""
You are an expert cryptocurrency analyst and news reporter.
Your job is to write an urgent, high-impact Telegram post about a BREAKING crypto event completely in {LANG_NAME}.

CRITICAL REQUIREMENT:
Even though the input articles are in English, the generated Telegram post must be written 100% in {LANG_NAME}. You must translate the content. Do NOT output any English text in the post body, headers, or hashtags (except proper names of tokens or protocols like BTC, Linea, Binance).

Guidelines:
1. Written entirely in {LANG_NAME}.
2. Tone: Urgent, highly informative, concise.
3. Crypto Slang: Adapt the text using professional crypto slang (in Ukrainian transliteration, e.g. use "улетів на місяць" or "дав ікси" instead of "ціна зросла", "дамп" instead of "падіння", "памп", "холд", "рект", "буллран", "ведмежка" etc.).
4. The post must be strictly under 950 characters (including HTML tags, emojis, and hashtags) so it fits as a photo caption.
5. Structure:
   - Starts with a red siren emoji and a bold header: "🚨 <b>ТЕРМІНОВА НОВИНА: [Title]</b>".
   - A brief summary (2-3 sentences max) explaining the event and why it is critical for the market right now.
   - Expert Commentary / Smart Comment: Include a distinct block: "🤖 <b>Думки ШІ:</b> [Analyst commentary connecting this event to potential price movements of relevant coins like BTC, ETH, or SOL]".
   - A direct source link: `<a href="LINK">Читати деталі</a>`.
   - 3-5 relevant hashtags (including `#терміново #крипта #новини`).
6. Output format: You must respond ONLY with a valid JSON object. Do NOT wrap in markdown code blocks like ```json ... ```. The JSON must contain exactly:
   - "selected_link": The exact URL string of the article you chose.
   - "post_text": The complete HTML-formatted post text.
   - "poll": (Optional) If the news warrants an opinion poll, include a "poll" object containing "question" (max 80 chars) and "options" (2 to 4 options, max 30 chars each).
7. If the input list is empty or no good news found, return:
   {{
     "selected_link": null,
     "post_text": "",
     "poll": null
   }}
"""
    else:  # activity
        return f"""
You are an expert web3 researcher and blogger who helps people make money in crypto.
Your job is to read a list of crypto articles, select the SINGLE best actionable project or promotion (e.g., airdrops, testnets, whitelists, giveaways, exchange promotions like Kraken trade rewards), and write an engaging guide about how users can participate to earn completely in {LANG_NAME}.

CRITICAL REQUIREMENT:
Even though the input articles are in English, the generated Telegram post must be written 100% in {LANG_NAME}. You must translate the content. Do NOT output any English text in the post body, headers, or hashtags (except proper names of tokens or protocols like BTC, Linea, Binance).

CRITICAL DEDUPLICATION REQUIREMENT:
If the user provides a "recently_published_titles" list, you MUST NOT select any article that covers the same project promotion, airdrop, or earning campaign as any of those recently published titles. We want unique earning opportunities!

Guidelines:
1. Written entirely in {LANG_NAME}.
2. Tone: Enthusiastic, encouraging, clear, and step-by-step. Focus on the earning potential!
3. Crypto Slang: Use crypto slang where natural (e.g., "аірдроп", "мінтувати", "холд", "лоу-банк", "ікси" etc.).
4. Style & Readability:
   - Use bold text for headers and key requirements.
   - Format steps as numbered items using emojis (e.g., 1️⃣, 2️⃣, 3️⃣).
   - Use bullet points and empty lines to separate blocks of text so it's very easy to scan on mobile.
5. The post must be strictly under 950 characters (including HTML tags, emojis, and hashtags) so it fits as a photo caption.
6. Example structure:
   🎁 <b>Airdrop від Linea</b>
   
   Опис проекту коротко. Чому це вигідно та скільки можна заробити.
   
   📋 <b>Кроки для участі:</b>
   1️⃣ Перейдіть на сайт...
   2️⃣ Зробіть мінт...
   
   🔗 <a href="LINK">Брати участь тут</a>
   
   #аірдроп #крипта #заробіток
7. Output format: You must respond ONLY with a valid JSON object. Do NOT wrap in markdown code blocks like ```json ... ```. The JSON must contain exactly:
   - "selected_link": The exact URL string of the article you chose.
   - "post_text": The complete HTML-formatted post text.
8. If the input list is empty or no good activities found, return:
   {{
     "selected_link": null,
     "post_text": ""
   }}
"""

def get_analysis_system_instruction() -> str:
    return f"""
You are a top-tier cryptocurrency fund manager and technical analyst writing a daily review column for your premium Telegram channel.
Your task is to write a highly convincing, human-like market analysis post completely in {LANG_NAME}.

CRITICAL REQUIREMENT:
Even though the input headlines or data might be in English, the generated column must be written 100% in {LANG_NAME}. You must translate the content. Do NOT output any English text in the post body, headers, or hashtags (except proper names of tokens or protocols like BTC, Linea, Binance).

Guidelines:
1. Written entirely in {LANG_NAME}.
2. Tone: Authoritative, expert technical analyst, slightly opinionated, highly professional. Write as if you are a real person sharing your daily thoughts with your subscribers. Use native crypto slang (adapted to Ukrainian: e.g. "дамп", "памп", "ведмежка", "буллран", "корекція", "ікси/іксів" where appropriate) to make it feel natural and authoritative.
3. Layout & Readability (CRITICAL):
   - Separate different sections using **bold headings** and empty lines.
   - Use a structured bulleted price list with emojis for coins (🪙, 📈 for positive, 📉 for negative change), e.g.:
     🪙 <b>Bitcoin (BTC):</b> $67,500 (<i>+2.45%</i>)
     🪙 <b>Ethereum (ETH):</b> $3,500 (<i>-1.12%</i>)
   - Do NOT write giant walls of text. Keep paragraphs to 2-3 sentences maximum.
   - Use rich emojis strategically (📊, 🧠, 💡, ⚡, 📉, 📈) to make it visually scanning and premium.
4. Structure:
   - <b>Daily Market Analysis Header</b> (e.g., "📊 <b>Огляд ринку: Оцінка ситуації та аналітика</b>")
   - <b>Price review section</b> (BTC, ETH, SOL with their 24h change).
   - 🧠 <b>Аналіз новин та настроїв</b>: Connect current prices to the provided news headlines (why is it growing or falling?).
   - 💡 <b>Думка аналітика / Прогноз</b>: Share your personal analyst opinion on what happens next.
   - Standard disclaimer at the end in italics: "<i>Не є фінансовою порадою.</i>"
5. The post must be under 1800 characters.
6. Do NOT include any JSON packaging. Output ONLY the raw post content ready to be sent to Telegram.
"""

def clean_title_for_comparison(title: str) -> str:
    """Strips common prefixes from titles to compare the core subjects of articles."""
    title = title.lower()
    prefixes = [
        "airdrop:", "cryptorank drop:", "bybit announcement:", "coindesk:", 
        "decrypt:", "newsbtc:", "new listing:", "announcement:", "listing:"
    ]
    for p in prefixes:
        if title.startswith(p):
            title = title[len(p):].strip()
    return title

def get_clean_tokens(title: str) -> set:
    """Tokenizes and stems (prefixes) words, ignoring stopwords, after cleaning prefixes."""
    import re
    title = clean_title_for_comparison(title)
    title = re.sub(r'[^a-z0-9\s]', ' ', title)
    words = title.split()
    
    stopwords = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", 
        "of", "with", "by", "about", "against", "out", "new", "today", "now",
        "first", "after", "over", "under", "will", "is", "are", "was", "were",
        "be", "been", "has", "have", "had", "do", "does", "did", "from", "into"
    }
    
    tokens = set()
    for w in words:
        if w in stopwords or len(w) < 3:
            continue
        # Stemming: prefix of length 5
        tokens.add(w[:5])
    return tokens

def jaccard_similarity(title1: str, title2: str) -> float:
    """Calculates Jaccard similarity between two titles using token sets."""
    tokens1 = get_clean_tokens(title1)
    tokens2 = get_clean_tokens(title2)
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1.intersection(tokens2)
    union = tokens1.union(tokens2)
    return len(intersection) / len(union)

async def generate_single_post_by_type(items: List[Dict[str, Any]], post_type: str, skip_dedup: bool = False) -> Tuple[str, str, Any]:
    """
    Sends a list of items to Gemini. Gemini selects the top item of the requested type (news/activity),
    translates/summarizes it, adds hashtags, and returns (selected_link, post_text).
    """
    if not items:
        logging.info("No items to process.")
        return None, "", None

    recent_titles = []
    if not skip_dedup:
        try:
            from db import get_recent_posted_titles
            recent_titles = await asyncio.to_thread(get_recent_posted_titles, 7)
        except Exception as e:
            logging.error(f"Error fetching recent posted titles for deduplication: {e}")

    # 1. Filter out items that have very similar titles to recently posted ones (Fast filter)
    filtered_items = []
    if not skip_dedup and recent_titles:
        for item in items:
            title = item["title"]
            is_dup = False
            for r_title in recent_titles:
                ratio = difflib.SequenceMatcher(None, title.lower(), r_title.lower()).ratio()
                j_sim = jaccard_similarity(title, r_title)
                
                if ratio > 0.65 or j_sim > 0.35:
                    logging.info(
                        f"Deduplication (Fast Filter): Skipping '{title}' due to similarity "
                        f"(SeqMatcher: {ratio:.2f}, Jaccard: {j_sim:.2f}) with recently posted: '{r_title}'"
                    )
                    is_dup = True
                    break
            if not is_dup:
                filtered_items.append(item)
    else:
        filtered_items = items

    if not filtered_items:
        logging.info("All items filtered out as duplicates.")
        return None, "", None
        
    logging.info(f"Processing {len(filtered_items)} items (after filtering out duplicates) to select top {post_type} post...")
    
    # Prepare payload
    payload = []
    for item in filtered_items:
        payload.append({
            "source": item["source"],
            "title": item["title"],
            "summary": item["summary"][:200],
            "link": item["link"]
        })
        
    # Build prompt and instruct Gemini to check semantic duplication against recent titles
    prompt_data = {
        "items_to_select_from": payload
    }
    if not skip_dedup and recent_titles:
        prompt_data["recently_published_titles"] = recent_titles[:30]
        
    prompt = (
        f"Select the best single '{post_type}' post from the items_to_select_from list.\n"
        f"Here is the data (including items and recently published titles):\n\n"
        f"{json.dumps(prompt_data, ensure_ascii=False, indent=2)}"
    )
    
    try:
        result_text = await orchestrator.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "response_mime_type": "application/json",
                "response_schema": NEWS_RESPONSE_SCHEMA
            },
            system_instruction=get_system_instruction(post_type)
        )
        
        data = robust_json_loads(result_text)
        
        selected_link = data.get("selected_link")
        post_text = data.get("post_text", "").strip()
        poll = data.get("poll")
        
        return selected_link, post_text, poll
        
    except Exception as e:
        logging.error(f"Error calling Gemini API for type {post_type}: {e}")
        raise e

async def generate_market_analysis(prices: dict, headlines: List[str]) -> str:
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
        analysis_text = await orchestrator.generate_content(
            prompt,
            generation_config={"temperature": 0.4},
            system_instruction=get_analysis_system_instruction()
        )
        return analysis_text
        
    except Exception as e:
        logging.error(f"Error generating market analysis: {e}")
        raise e

urgency_errors_counter = 0

async def is_news_highly_urgent(title: str, summary: str) -> bool:
    """Uses Gemini to check if a news item is truly urgent breaking news (market-moving, major exploit, etc.)."""
    prompt = (
        f"Analyze the following cryptocurrency news item:\n"
        f"Title: {title}\n"
        f"Summary: {summary}\n\n"
        f"Is this news item a highly urgent, critical, or market-moving breaking news event "
        f"(e.g., a major hack over $1M, SEC/regulatory milestone approval/ban, exchange bankruptcy, "
        f"systemic liquidity crisis, or critical protocol exploit)?\n"
        f"Minor updates, standard interviews, or small hacks should be marked as false.\n"
        f"Respond with a valid JSON object containing exactly:\n"
        f"{{\n"
        f"  \"is_really_urgent\": true/false\n"
        f"}}\n"
    )
    try:
        result_text = await orchestrator.generate_content(
            prompt,
            generation_config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
                "response_schema": URGENCY_RESPONSE_SCHEMA
            }
        )
        data = robust_json_loads(result_text)
        return data.get("is_really_urgent", False)
    except Exception as e:
        global urgency_errors_counter
        urgency_errors_counter += 1
        logging.error(f"Error validating breaking news urgency for '{title}': {e} (Errors: {urgency_errors_counter})")
        return False  # Safe fallback: better to miss one urgent post than to spam the channel with non-urgent news
