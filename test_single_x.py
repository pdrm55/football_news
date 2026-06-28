import sys
import logging
from dotenv import load_dotenv

# Load env variables
load_dotenv()

import scraper

# Enable info logging to see the login steps
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def main():
    print("=" * 60)
    print("        LIVE TWITTER LOGIN & SCRAPING TEST         ")
    print("=" * 60)
    
    # Initialize the Scraper (will attempt login)
    print("Attempting to initialize XScraper and log in to Twitter...")
    x_scraper = scraper.XScraper()
    
    if x_scraper.mock_mode:
        print("\n❌ FAILED: XScraper fell back to Mock Mode. Login was unsuccessful.")
        print("Please check your .env credentials or check if Twitter requires email verification/OTP.")
        return
        
    print("\n✅ SUCCESS: Login was successful! Scraper is in LIVE mode.")
    print("Fetching the latest tweets for @FabrizioRomano as a test...")
    
    try:
        # Fetch tweets
        tweets = x_scraper.get_latest_tweets("@FabrizioRomano", "Arsenal", limit=2)
        print(f"\nSuccessfully retrieved {len(tweets)} tweets:")
        print("-" * 60)
        for idx, t in enumerate(tweets, 1):
            print(f"Tweet #{idx}:")
            print(f"  ID: {t['id']}")
            print(f"  Text: {t['content']}")
            print(f"  Media: {t['media_url']}")
            print(f"  Link: {t['url']}")
            print("-" * 60)
    except Exception as e:
        print(f"\n❌ Error during tweet retrieval: {e}")

if __name__ == "__main__":
    main()
