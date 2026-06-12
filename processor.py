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

def get_system_instruction() -> str:
    return f"""
You are an expert cryptocurrency analyst, web3 researcher, and community manager running a premium Telegram channel.
Your job is to read a list of crypto articles, airdrops, testnets, and blogs, select the SINGLE most important, high-impact, or interesting news story or project, and write an engaging Telegram post about it in {LANG_NAME}.

Guidelines:
1. Written entirely in {LANG_NAME}. Use correct crypto terminology.
2. The post must be extremely punchy and strictly under 800 characters (including HTML tags, emojis, and hashtags). This is critical because it will be sent as a photo caption, which has a hard limit of 1024 characters in Telegram. Keep summaries brief (1-3 sentences).
3. The post must include:
   - A bolded title with an emoji (e.g. "🚀 <b>Airdrop від Scroll</b>" or "📰 <b>Регуляція крипти в США</b>").
   - A short, interesting summary of why this matters or what needs to be done.
   - A direct, clickable link to the source/action using Telegram HTML syntax, e.g. `<a href="LINK">Читати деталі</a>` or `<a href="LINK">Брати участь</a>`. Make sure the link is exactly the URL of the selected item.
   - At the very end, append 3-5 relevant Ukrainian hashtags separated by spaces (e.g. `#крипта #аірдроп #новини`).
4. Output MUST use valid Telegram HTML formatting tags:
   - <b>bold</b>
   - <i>italic</i>
   - <code>code</code>
   - <a href="url">link</a>
   Do NOT use any other HTML tags (like <ul>, <li>, <h1>, <br> - use standard line breaks instead).
5. Output format: You must respond ONLY with a valid JSON object. Do NOT wrap in markdown code blocks like ```json ... ```. The JSON must contain exactly these two keys:
   - "selected_link": The exact URL string of the article you chose.
   - "post_text": The complete HTML-formatted post text.
6. If the input list is empty or there are absolutely no high-quality, high-value updates worth posting, return:
   {{
     "selected_link": null,
     "post_text": ""
   }}
"""

def generate_single_post(items: List[Dict[str, Any]]) -> Tuple[str, str]:
    """
    Sends a list of items to Gemini. Gemini selects the top item,
    translates/summarizes it, adds hashtags, and returns (selected_link, post_text).
    """
    if not items:
        logging.info("No items to process.")
        return None, ""
        
    logging.info(f"Processing {len(items)} items to select the top post using Gemini API...")
    
    # Prepare payload
    payload = []
    for item in items:
        payload.append({
            "source": item["source"],
            "title": item["title"],
            "summary": item["summary"][:200], # keep it short for tokens
            "link": item["link"]
        })
        
    prompt = f"Here is the list of fetched crypto items. Select the best one and write a post:\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    
    try:
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=get_system_instruction()
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
        logging.error(f"Error calling Gemini API for single post: {e}")
        # Secondary fallback parse if JSON decode failed but response text exists
        try:
            if "response" in locals() and response.text:
                text = response.text.strip()
                # Try simple cleaning of code fences
                if text.startswith("```"):
                    lines = text.splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    text = "\n".join(lines).strip()
                data = json.loads(text)
                return data.get("selected_link"), data.get("post_text", "").strip()
        except Exception as nest_e:
            logging.error(f"Nested JSON parsing fallback failed: {nest_e}")
            
        return None, ""

if __name__ == "__main__":
    # Quick visual mock test
    mock_items = [
        {
            "source": "Cointelegraph",
            "title": "Bitcoin price hits new all-time high above $120k",
            "summary": "Bitcoin has surged past $120,000 following positive regulation news in the United States and massive institutional inflows into spot ETFs.",
            "link": "https://cointelegraph.com/news/bitcoin-all-time-high"
        },
        {
            "source": "AirdropAlert",
            "title": "Scroll network announces Scroll Session 2 Airdrop",
            "summary": "Scroll has officially launched Session 2 of its loyalty program. Users can bridge assets, swap on native DEXs, and lock marks to qualify for the upcoming token airdrop.",
            "link": "https://airdropalert.com/scroll-session-2-airdrop"
        }
    ]
    print("Generating mockup single post...")
    link, post = generate_single_post(mock_items)
    print(f"Selected link: {link}")
    print("\n--- GENERATED POST ---")
    print(post)
    print("----------------------")
