import sys
import logging
from dotenv import load_dotenv

# Load env variables
load_dotenv()

import database
import scraper

# Configure logging
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

def main():
    print("=" * 60)
    print("           X (TWITTER) ACCOUNTS LATEST TWEET TEST          ")
    print("=" * 60)
    
    # Ensure database is initialized
    database.init_db()
    
    # Get all X accounts from database
    sources = database.get_sources()
    x_sources = [s for s in sources if s['type'] == 'x_account']
    
    if not x_sources:
        print("No X accounts found in the database. Please add some first.")
        return
        
    print(f"Found {len(x_sources)} X accounts in the database.")
    
    # Initialize the Scraper (detects if credentials are set in .env)
    print("\nInitializing X Scraper...")
    x_scraper = scraper.XScraper()
    
    if x_scraper.mock_mode:
        print("\n⚠️  Running in SIMULATOR (MOCK) MODE because X credentials are not set in .env.")
        print("To fetch real tweets, configure X_USERNAME, X_PASSWORD, and X_EMAIL in your .env file.\n")
    else:
        print("\n🚀 Running in REAL scraping mode using Twikit.\n")
        
    print("-" * 60)
    
    for idx, src in enumerate(x_sources, 1):
        handle = src['value']
        team = src['team_tag']
        print(f"[{idx}/{len(x_sources)}] Checking {handle} ({team})...")
        
        try:
            # Fetch the latest 1 tweet
            tweets = x_scraper.get_latest_tweets(handle, team, limit=1)
            if tweets:
                tweet = tweets[0]
                print(f"  🐦 Tweet ID: {tweet['id']}")
                print(f"  📝 Content:  {tweet['content']}")
                if tweet['media_url']:
                    print(f"  🖼️ Media URL: {tweet['media_url']}")
            else:
                print("  ❌ No tweets found.")
        except Exception as e:
            print(f"  ❌ Error fetching from {handle}: {e}")
            
        print("-" * 60)

if __name__ == "__main__":
    main()
