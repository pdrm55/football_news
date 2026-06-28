import os
import re
import time
import requests
import asyncio
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def run_phase_1():
    print("\n" + "="*60)
    print("🚀 --- PHASE 1: DIRECT WEB SCRAPING (transferfeed.com) ---")
    print("="*60)
    
    url = "https://www.transferfeed.com"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }
    
    try:
        print(f"📡 Fetching URL: {url}...")
        response = requests.get(url, headers=headers, timeout=12)
        print(f"📥 Response Code: {response.status_code}")
        
        # Check for Cloudflare/DDoS-Guard/Anti-bot screens
        html_lower = response.text.lower()
        is_cloudflare = ("just a moment" in html_lower or "enable javascript" in html_lower or 
                         ("cloudflare" in html_lower and "attention required" in html_lower) or
                         (response.status_code in (403, 503) and "cloudflare" in html_lower))
        if is_cloudflare:
            print("⚠️  CLOUDFLARE OR ANTI-BOT SCREEN DETECTED!")
            print("Server returned a challenge screen instead of the actual webpage.")
            print("\n--- RESPONSE SNIPPET ---")
            print(response.text[:800].strip())
            print("------------------------\n")
            return False
            
        if response.status_code == 200:
            print("✅ HTTP 200 OK! Parsing with BeautifulSoup...")
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Find title / anchor elements that look like articles
            items = []
            for tag in soup.find_all(['h1', 'h2', 'h3', 'a']):
                text = tag.get_text().strip()
                href = tag.get('href', '')
                if text and len(text) > 15 and not text.startswith(('Home', 'Contact', 'Privacy', 'About')):
                    items.append((text, href))
            
            print(f"📊 Extracted {len(items)} potential article/link elements.")
            
            if not items:
                print("❓ Page parsed successfully, but no matching article elements were found.")
                print("\n--- RAW HTML SNIPPET ---")
                print(response.text[:600].strip())
                print("------------------------\n")
            else:
                print("\n--- TOP 5 EXTRACTED UPDATES ---")
                for idx, (title, href) in enumerate(items[:5], 1):
                    full_href = href if href.startswith('http') else f"https://www.transferfeed.com{href}"
                    print(f" [{idx}] Title: {title}")
                    print(f"     Link:  {full_href}")
                print("-------------------------------\n")
            return True
        else:
            print(f"❌ Server rejected request. Status code: {response.status_code}")
            print("\n--- RESPONSE SNIPPET ---")
            print(response.text[:500].strip())
            print("------------------------\n")
            return False
            
    except requests.Timeout:
        print("❌ Request Timed Out (server did not respond in time).")
        return False
    except Exception as e:
        print(f"❌ Phase 1 Scraping failed: {e}")
        return False


def run_phase_2():
    print("\n" + "="*60)
    print("🐦 --- PHASE 2: X (TWITTER) TARGET SCRAPER SIMULATION ---")
    print("="*60)
    
    target_handle = "transferfeed"
    username = os.getenv("X_USERNAME")
    password = os.getenv("X_PASSWORD")
    email = os.getenv("X_EMAIL")
    
    # Session cookies check
    cookies_path = "cookies.json"
    cookies_exist = os.path.exists(cookies_path)
    print(f"🍪 Checking session: {'✅ Found cookies.json' if cookies_exist else '❌ No cookies.json found'}")
    
    if not username or not password:
        print("⚠️  Warning: X credentials not configured in .env. Running Mock Mode.")
        run_mock_x(target_handle)
        return
        
    async def _fetch_tweets():
        from twikit import Client
        print(f"📡 Connecting to X as @{username}...")
        client = Client('en-US')
        
        if cookies_exist:
            client.load_cookies(cookies_path)
            print("Session cookies loaded successfully.")
        else:
            print("Logging in with credentials...")
            await client.login(
                auth_info_1=username,
                auth_info_2=email,
                password=password
            )
            client.save_cookies(cookies_path)
            print("Logged in successfully and saved session cookies.")
            
        print(f"🔍 Fetching user profile for @{target_handle}...")
        user = await client.get_user_by_screen_name(target_handle)
        print(f"👤 Profile details: ID {user.id} | Followers: {user.followers_count}")
        
        print("📥 Fetching latest 3 tweets...")
        tweets = await client.get_user_tweets(user.id, 'Tweets')
        
        print("\n--- LATEST 3 TWEETS ---")
        for idx, t in enumerate(tweets[:3], 1):
            # Extract links in tweet
            urls = []
            if hasattr(t, 'urls') and t.urls:
                urls = t.urls
            
            print(f" [{idx}] Tweet ID: {t.id}")
            print(f"     Created:  {getattr(t, 'created_at', 'Unknown')}")
            print(f"     Content:  {t.text}")
            if urls:
                print(f"     Links:    {urls}")
            print("-" * 40)
            
    try:
        asyncio.run(_fetch_tweets())
    except Exception as e:
        print(f"⚠️  X Scraping failed: {e}")
        print("Executing Fallback to Mock Simulation...")
        run_mock_x(target_handle)


def run_mock_x(handle):
    print("\n[MOCK MODE] Simulating latest tweets from X:")
    mock_tweets = [
        {
            "id": "1805123456789012345",
            "text": "BREAKING: Chelsea agree personal terms with Nico Williams. Release clause of €58m ready to be triggered. Here we go! 🛫🔵 #CFC #Chelsea",
            "created_at": "Sun Jun 28 09:30:15 +0000 2026",
            "urls": ["https://transferfeed.com/news/nico-williams-chelsea"]
        },
        {
            "id": "1805098765432109876",
            "text": "UPDATE: Liverpool make first official inquiry for Bayern Munich midfielder Joshua Kimmich. Player open to Premier League move. 🔴🇩🇪 #LFC #FCBayern",
            "created_at": "Sun Jun 28 08:15:42 +0000 2026",
            "urls": ["https://transferfeed.com/news/kimmich-liverpool"]
        },
        {
            "id": "1805076543210987654",
            "text": "DONE DEAL: Inter Milan complete medical tests for Piotr Zielinski. Official statement expected soon. 3-year contract signed. 🔵⚫️ #Inter",
            "created_at": "Sun Jun 28 07:05:10 +0000 2026",
            "urls": ["https://transferfeed.com/news/zielinski-inter-done"]
        }
    ]
    for idx, t in enumerate(mock_tweets, 1):
        print(f" [{idx}] Tweet ID: {t['id']}")
        print(f"     Created:  {t['created_at']}")
        print(f"     Content:  {t['text']}")
        print(f"     Links:    {t['urls']}")
        print("-" * 40)


if __name__ == "__main__":
    print("============================================================")
    print("🔍 DIAGNOSTIC FEED TEST: TRANSFERFEED.COM")
    print("============================================================")
    
    # Run Phase 1
    run_phase_1()
    
    # Wait briefly between phases
    time.sleep(2)
    
    # Run Phase 2
    run_phase_2()
    
    print("\n🏁 Diagnostic Tests Completed.")
