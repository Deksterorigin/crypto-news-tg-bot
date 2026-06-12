from fetcher import fetch_all_new_items, extract_image_url
from processor import generate_single_post_by_type
import sys

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        
    print("=== Testing Gemini Single Post Processor ===")
    
    # 1. Fetch live new items
    items = fetch_all_new_items()
    if not items:
        print("No items fetched. Cannot run test.")
        sys.exit(1)
        
    print(f"Total live new items fetched: {len(items)}")
    
    # Take a small batch of 10 items
    batch = items[:10]
    print(f"\nProcessing first {len(batch)} items through Gemini...")
    for idx, item in enumerate(batch, 1):
        print(f"  {idx}. [{item['source']}] {item['title']}")
        
    selected_link, post_text, poll = generate_single_post_by_type(batch, "news")
    
    print("\n" + "=" * 20 + " GENERATED POST PREVIEW " + "=" * 20)
    if post_text:
        print(f"Selected Link: {selected_link}")
        
        # Test image scraping for this selected link
        print("Extracting image for selected link...")
        img_url = extract_image_url(selected_link)
        print(f"Extracted Image: {img_url}")
        
        print("\n--- Post Content ---")
        print(post_text)
        print("--------------------")
        print(f"Post Length: {len(post_text)} characters (limit: 1024)")
        if len(post_text) > 1024:
            print("⚠️ WARNING: Post exceeds Telegram photo caption limit!")
        else:
            print("✅ Post size is within the safety limits.")
            
        if poll:
            print("\n--- Poll Details ---")
            print(f"Question: {poll.get('question')}")
            print(f"Options : {poll.get('options')}")
            print("--------------------")
    else:
        print("❌ FAILED: No post generated or error occurred.")
    print("=" * 64)

if __name__ == "__main__":
    main()
