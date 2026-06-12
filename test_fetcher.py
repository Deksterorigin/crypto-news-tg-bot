from fetcher import fetch_all_new_items

def main():
    print("=== Testing RSS Feeds Fetcher ===")
    items = fetch_all_new_items()
    print(f"\nFetched total of {len(items)} unposted items from all feeds.")
    
    if items:
        print("\nDisplaying first 3 fetched items with their extracted image URLs:")
        for idx, item in enumerate(items[:3], 1):
            print(f"\n--- Item {idx} ---")
            print(f"Source: {item['source']}")
            print(f"Title : {item['title']}")
            print(f"Link  : {item['link']}")
            
            from fetcher import extract_image_url
            print("Fetching og:image...")
            img_url = extract_image_url(item['link'])
            print(f"Image : {img_url}")
            print(f"Summary: {item['summary'][:150]}...")
    else:
        print("No items fetched. (Check internet connection or feed availability.)")

if __name__ == "__main__":
    main()
