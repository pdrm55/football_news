import os
import json
import time
import random
import logging
import urllib.parse
import html
import requests
import feedparser
import asyncio
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

import datetime
import config
import database

logger = logging.getLogger("scraper")

# Note: requirements.txt installs 'twifork' as a drop-in replacement for 'twikit'.
# We import from the 'twikit' namespace because the fork maintains namespace compatibility.

PROTECTED_DOMAINS = [
    'nytimes.com', 'thetimes.com', 'telegraph.co.uk',
    'theguardian.com', 'independent.co.uk', 'standard.co.uk',
    'dailymail.com', 'dailymail.co.uk', 'thesun.co.uk', 'skysports.com',
    'liverpoolecho.co.uk', 'mirror.co.uk'
]

class XScraper:
    """Handles fetching tweets from X (Twitter) using Twikit.
    Falls back to a Simulator Mode if credentials are missing or login fails.
    """
    def __init__(self):
        self.username = config.X_USERNAME
        self.password = config.X_PASSWORD
        self.email = config.X_EMAIL
        self.client = None
        self.mock_mode = True

        if config.X_USERNAME or os.path.exists("cookies.json"):
            try:
                # If cookies exist, verify they work by performing a lightweight fetch
                if os.path.exists("cookies.json"):
                    from twikit import Client
                    async def verify_cookies():
                        client = Client('en-US', proxy=config.PROXY_URL) if config.PROXY_URL else Client('en-US')
                        client.load_cookies("cookies.json")
                        # Perform a lightweight API call to verify session
                        await client.get_user_by_screen_name('X')
                    
                    asyncio.run(verify_cookies())
                    self.mock_mode = False
                    logger.info("Successfully loaded X (Twitter) session from cookies.json and verified connection.")
                elif self.username and self.password:
                    # Perform initial login to generate cookies
                    from twikit import Client
                    async def do_init_login():
                        client = Client('en-US', proxy=config.PROXY_URL) if config.PROXY_URL else Client('en-US')
                        await client.login(
                            auth_info_1=self.username,
                            auth_info_2=self.email,
                            password=self.password
                        )
                        client.save_cookies("cookies.json")
                    
                    asyncio.run(do_init_login())
                    self.mock_mode = False
                    logger.info("Successfully logged in to X and saved session to cookies.json.")
            except Exception as e:
                logger.error(f"X login/verification failed: {e}. Falling back to Simulator Mode.")
                self.mock_mode = True
        else:
            logger.info("X credentials not fully provided. Running X Scraper in Simulator Mode.")
            self.mock_mode = True

    def get_latest_tweets(self, account_handle: str, team_tag: str, limit: int = 3):
        """Fetches the latest tweets for a given handle.
        Staggers requests randomly between 8 to 15 seconds if in real mode.
        """
        handle = account_handle.lstrip('@')
        if not self.mock_mode:
            # Staggered sleep to avoid bans/rate limits
            delay = random.randint(8, 15)
            logger.info(f"Staggering: Sleeping for {delay} seconds before checking @{handle}")
            time.sleep(delay)

            async def _async_fetch():
                from twikit import Client
                # Instantiate Client fresh in this specific event loop
                client = Client('en-US', proxy=config.PROXY_URL) if config.PROXY_URL else Client('en-US')
                
                if os.path.exists("cookies.json"):
                    client.load_cookies("cookies.json")
                elif self.username and self.password:
                    await client.login(
                        auth_info_1=self.username,
                        auth_info_2=self.email,
                        password=self.password
                    )
                    client.save_cookies("cookies.json")
                    
                user = await client.get_user_by_screen_name(handle)
                tweets = await client.get_user_tweets(user.id, 'Tweets')
                
                result = []
                for t in tweets:
                    if len(result) >= limit:
                        break
                        
                    # Skip Retweets (RTs)
                    is_retweet = False
                    if t.text.strip().upper().startswith('RT ') or t.text.strip().upper().startswith('RT @'):
                        is_retweet = True
                    elif getattr(t, 'retweeted_status', None) is not None:
                        is_retweet = True
                        
                    if is_retweet:
                        logger.info(f"Skipping Retweet from @{handle}: {t.text[:50]}...")
                        continue
                        
                    # Skip Replies
                    is_reply = False
                    if t.text.strip().startswith('@'):
                        is_reply = True
                    elif getattr(t, 'in_reply_to', None) is not None:
                        is_reply = True
                        
                    if is_reply:
                        logger.info(f"Skipping Reply tweet from @{handle}: {t.text[:50]}...")
                        continue
                        
                    # Skip tweets older than 24 hours (or if they lack a creation date)
                    if not t.created_at:
                        logger.info(f"Skipping tweet from @{handle} - missing creation date.")
                        continue
                        
                    try:
                        from email.utils import parsedate_to_datetime
                        import datetime
                        tweet_dt = parsedate_to_datetime(t.created_at)
                        now = datetime.datetime.now(datetime.timezone.utc)
                        age = now - tweet_dt
                        if age > datetime.timedelta(hours=24):
                            logger.info(f"Skipping tweet from @{handle} - older than 24 hours (age: {age.days} days)")
                            continue
                    except Exception as date_err:
                        logger.warning(f"Could not parse tweet date '{t.created_at}': {date_err}")
                        continue
                        
                    # Extract media if present
                    media_url = None
                    if hasattr(t, 'media') and t.media:
                        for media in t.media:
                            m_type = getattr(media, 'type', None)
                            m_url = getattr(media, 'media_url', None)
                            if m_type == 'photo' and m_url:
                                media_url = m_url
                                break
                    
                    tweet_url = f"https://x.com/{handle}/status/{t.id}"
                    result.append({
                        'id': tweet_url,
                        'title': f"Update from @{handle}",
                        'content': t.text,
                        'media_url': media_url,
                        'url': tweet_url
                    })
                return result

            try:
                return asyncio.run(_async_fetch())
            except Exception as e:
                logger.error(f"Error fetching tweets for @{handle}: {e}. Falling back to mock data.")
                self.mock_mode = True

        # Mock Mode / Fallback Simulator
        logger.info(f"[Mock Mode] Generating simulated tweets for @{handle} ({team_tag})")
        mock_templates = [
            "Massive update! The manager has just confirmed team news ahead of the weekend clash. Key players returning to training. 🏃‍♂️🔥 #{team_tag}",
            "Rumors circulating about a new contract extension for our star midfielder. Talks are progressing well. Negotiations should conclude soon! ✍️⚽ #{team_tag}",
            "Tactical breakdown of yesterday's training session shows new shape and set-piece drills being prioritized. Big match preparation in full swing. 📈🔴 #{team_tag}",
            "Injury update: Standard scans showed no serious tear. Expected to be back on the pitch within 10-14 days. Great news for the squad! 💪🏥 #{team_tag}",
            "Transfer update: Negotiations between clubs have advanced. Personal terms are agreed. Final paperwork is being prepared. Here we go! 🛫📰 #{team_tag}"
        ]
        
        simulated = []
        # Generate 2 simulated tweets for testing
        for i in range(2):
            tweet_id = f"mock_tweet_{handle}_{int(time.time())}_{i}"
            content = random.choice(mock_templates).format(team_tag=team_tag)
            tweet_url = f"https://x.com/{handle}/status/{tweet_id}"
            simulated.append({
                'id': tweet_url,
                'title': f"Tweet update from @{handle}",
                'content': content,
                'media_url': "https://images.unsplash.com/photo-1508098682722-e99c43a406b2?w=500" if i == 0 else None,
                'url': tweet_url
            })
        return simulated


def extract_rss_image(entry) -> str | None:
    """Helper to extract featured images from feedparser entry fields."""
    # 1. Check media:content or media:thumbnail
    media_content = entry.get('media_content')
    if media_content and isinstance(media_content, list):
        for media in media_content:
            if 'url' in media:
                return media['url']
                
    # 2. Check enclosures
    enclosures = entry.get('enclosures')
    if enclosures and isinstance(enclosures, list):
        for enc in enclosures:
            if enc.get('type', '').startswith('image/') and 'href' in enc:
                return enc['href']
                
    # 3. Check links
    links = entry.get('links')
    if links and isinstance(links, list):
        for link in links:
            if link.get('type', '').startswith('image/') and 'href' in link:
                return link['href']
                
    # 4. Scrape the HTML description/summary for <img> tags
    for field in ['summary', 'description']:
        html_content = entry.get(field)
        if html_content:
            try:
                soup = BeautifulSoup(html_content, 'html.parser')
                img = soup.find('img')
                if img and img.get('src'):
                    return img['src']
            except Exception:
                pass
                
    return None


def parse_article_html(html_content: str) -> tuple[str | None, str | None, str | None]:
    """Parses HTML content using BeautifulSoup and extracts (title, content, image_url)
    using the standard selector logic.
    """
    if not html_content:
        return None, None, None
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Extract title
        title = ""
        title_tag = soup.find('h1') or soup.find('title')
        if title_tag:
            title = title_tag.get_text().strip()
            
        # Extract image (OG tags preferred)
        image_url = None
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image['content']
        else:
            twitter_image = soup.find('meta', attrs={'name': 'twitter:image'})
            if twitter_image and twitter_image.get('content'):
                image_url = twitter_image['content']
            else:
                # Fallback: Find first large image
                for img in soup.find_all('img'):
                    src = img.get('src')
                    if src and src.startswith('http'):
                        if not any(x in src.lower() for x in ['logo', 'icon', 'header', 'footer', 'avatar', 'sprite']):
                            image_url = src
                            break
                            
        # Target the main article body container if available for cleaner, full text
        body_selectors = [
            'article',
            'div.article-body',
            'div.article__body',
            'div.article-content',
            'div.entry-content',
            'div.post-content',
            'div.story-body',
            'div[itemprop="articleBody"]',
            'main'
        ]
        
        content_text = ""
        article_body = None
        for selector in body_selectors:
            el = soup.select_one(selector)
            if el:
                paragraphs = el.find_all('p')
                if len(paragraphs) >= 2:
                    article_body = el
                    break
        
        if article_body:
            paragraphs = article_body.find_all('p')
            paragraphs_text = [p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 15]
            content_text = "\n\n".join(paragraphs_text)
            
        if not content_text:
            paragraphs = soup.find_all('p')
            paragraphs_text = [p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 15]
            content_text = "\n\n".join(paragraphs_text)
            
        if not content_text:
            content_text = soup.get_text()
            
        return title, content_text, image_url
    except Exception as e:
        logger.error(f"Error parsing HTML content: {e}")
        return None, None, None


def scrape_web_page(url: str) -> tuple[str | None, str | None, str | None]:
    """Scrapes a general web page using BeautifulSoup.
    Returns: (title, main_content_text, image_url)
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        if response.status_code != 200:
            logger.warning(f"Failed to fetch {url}, status code: {response.status_code}")
            return None, None, None
            
        return parse_article_html(response.content)
    except Exception as e:
        logger.error(f"Error scraping web page {url}: {e}")
        return None, None, None


def extract_article_published_date(html_text: str) -> datetime.datetime | None:
    """Attempts to extract the publication date of an article from its HTML metadata.
    Returns a timezone-aware datetime in UTC, or None if not found.
    """
    if not html_text:
        return None
    try:
        import json
        import datetime
        from email.utils import parsedate_to_datetime
        
        soup = BeautifulSoup(html_text, 'html.parser')
        
        # 1. Check common meta tags
        meta_selectors = [
            {'property': 'article:published_time'},
            {'name': 'pubdate'},
            {'property': 'og:pubdate'},
            {'name': 'publish-date'},
            {'property': 'article:published'},
            {'name': 'publication_date'}
        ]
        for attrs in meta_selectors:
            tag = soup.find('meta', attrs=attrs)
            if tag and tag.get('content'):
                try:
                    val = tag['content'].strip()
                    if 't' in val.lower() or '-' in val:
                        val = val.replace('Z', '+00:00')
                        return datetime.datetime.fromisoformat(val).astimezone(datetime.timezone.utc)
                    else:
                        return parsedate_to_datetime(val).astimezone(datetime.timezone.utc)
                except Exception:
                    pass
                    
        # 2. Check JSON-LD
        for script in soup.find_all('script', type='application/ld+json'):
            if script.string:
                try:
                    data = json.loads(script.string.strip())
                    if isinstance(data, dict):
                        date_str = data.get('datePublished') or data.get('dateCreated')
                        if not date_str and '@graph' in data:
                            for item in data['@graph']:
                                date_str = item.get('datePublished') or item.get('dateCreated')
                                if date_str:
                                    break
                        if date_str:
                            date_str = date_str.replace('Z', '+00:00')
                            return datetime.datetime.fromisoformat(date_str).astimezone(datetime.timezone.utc)
                    elif isinstance(data, list):
                        for item in data:
                            date_str = item.get('datePublished') or item.get('dateCreated')
                            if date_str:
                                date_str = date_str.replace('Z', '+00:00')
                                return datetime.datetime.fromisoformat(date_str).astimezone(datetime.timezone.utc)
                except Exception:
                    pass
                    
        # 3. Check <time> tag
        time_tag = soup.find('time')
        if time_tag and time_tag.get('datetime'):
            try:
                val = time_tag['datetime'].strip().replace('Z', '+00:00')
                return datetime.datetime.fromisoformat(val).astimezone(datetime.timezone.utc)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Error in extract_article_published_date: {e}")
    return None


def extract_articles_from_author_page(author_url: str, html_text: str) -> list[str]:
    """Parses the HTML of an author profile page and extracts the top 5 article detail URLs.
    Avoids returning the author profile URL itself, topic hubs, or non-article links.
    """
    if not html_text:
        return []
        
    from urllib.parse import urljoin, urlparse
    
    soup = BeautifulSoup(html_text, 'html.parser')
    parsed_author = urlparse(author_url)
    author_domain = parsed_author.netloc.lower()
    
    links = []
    seen = set()
    
    for tag in soup.find_all('a'):
        href = tag.get('href', '').strip()
        if not href or href.startswith('#') or href.startswith('javascript:'):
            continue
            
        full_url = urljoin(author_url, href)
        parsed_url = urlparse(full_url)
        
        if parsed_url.netloc.lower() != author_domain:
            continue
            
        url_path = parsed_url.path
        
        if url_path.rstrip('/') == parsed_author.path.rstrip('/'):
            continue
            
        is_article = False
        
        if 'athletic' in author_domain or 'nytimes.com' in author_domain:
            parts = [p for p in url_path.split('/') if p]
            if len(parts) >= 2 and parts[0] == 'athletic' and parts[1].isdigit():
                is_article = True
                
        elif 'thetimes.co.uk' in author_domain or 'thetimes.com' in author_domain:
            if '/article/' in url_path:
                is_article = True
                
        elif 'telegraph.co.uk' in author_domain:
            if any(f'/{year}/' in url_path for year in ('2025', '2026', '2027')):
                is_article = True
                
        elif 'theguardian.com' in author_domain:
            if any(f'/{year}/' in url_path for year in ('2025', '2026', '2027')):
                is_article = True
                
        elif 'independent.co.uk' in author_domain:
            if url_path.endswith('.html') and not '/author/' in url_path:
                is_article = True
                
        elif 'standard.co.uk' in author_domain:
            if not any(x in url_path for x in ('/author/', '/tag/', '/topic/', '/category/')) and len(url_path.split('/')) >= 3:
                is_article = True
                
        elif 'dailymail.co.uk' in author_domain or 'dailymail.com' in author_domain:
            if '/article-' in url_path and url_path.endswith('.html'):
                is_article = True
                
        elif 'thesun.co.uk' in author_domain:
            if '/sport/' in url_path or '/football/' in url_path:
                parts = [p for p in url_path.split('/') if p]
                if len(parts) >= 3 and any(p.isdigit() for p in parts):
                    is_article = True
                    
        elif 'skysports.com' in author_domain:
            if '/football/news/' in url_path:
                is_article = True
                
        else:
            # Fallback for Reach plc (liverpoolecho, mirror) and other domains
            if ('/sport/' in url_path or '/football/' in url_path) and not any(x in url_path for x in ('/author/', '/tag/', '/topic/', '/category/', '/all-about/', '/rss/')):
                is_article = True
                
        if is_article and full_url not in seen:
            seen.add(full_url)
            links.append(full_url)
            
    return links[:5]


def auto_detect_source_classification(url: str) -> tuple[str, str, str]:
    """Helper to analyze a URL and auto-detect if it is a valid RSS feed,
    a regular web page, or a Cloudflare-protected web page.
    Returns: (detected_type, resolved_url, description_message)
    """
    url = url.strip()
    if not url.startswith('http'):
        return 'web_link', url, "Link lacks http/https protocol. Registered as regular Web Link."
        
    from urllib.parse import urljoin, urlparse
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    # Force protected domains to web_link for DrissionPage crawling
    if any(d in domain for d in PROTECTED_DOMAINS):
        return 'web_link', url, f"Domain '{domain}' is whitelisted as Cloudflare-protected. Registered as Web Link for DrissionPage crawling."
    import requests
    import feedparser
    from bs4 import BeautifulSoup
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    # 1. Test if the entered URL is already a valid RSS feed
    try:
        feed = feedparser.parse(url)
        if len(feed.entries) > 0:
            return 'rss', url, "Valid RSS feed detected directly!"
    except Exception:
        pass
        
    # 2. Try fetching the page HTML using standard requests
    try:
        res = requests.get(url, headers=headers, timeout=8, allow_redirects=True)
        
        # If it returns Cloudflare status code or signature
        cloudflare_signals = ['cloudflare', 'sucuri', 'ddos-guard', 'ray id', 'javascript is required', 'enable javascript']
        res_text_lower = res.text.lower()
        
        if res.status_code in (403, 503) or any(sig in res_text_lower for sig in cloudflare_signals):
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            domain_parts = domain.split('.')
            if len(domain_parts) > 2:
                clean_domain = '.'.join(domain_parts[-2:])
            else:
                clean_domain = domain
                
            if clean_domain not in PROTECTED_DOMAINS:
                PROTECTED_DOMAINS.append(clean_domain)
                logger.info(f"Dynamically added '{clean_domain}' to PROTECTED_DOMAINS list.")
                
            return 'web_link', url, f"Cloudflare/Paywall protection detected on {clean_domain}. Configured for Headless Chromium (DrissionPage) scraping."
            
        if res.status_code == 200:
            soup = BeautifulSoup(res.content, 'html.parser')
            rss_link = None
            for link_tag in soup.find_all('link', rel='alternate'):
                l_type = link_tag.get('type', '').lower()
                l_href = link_tag.get('href', '').strip()
                if ('rss+xml' in l_type or 'atom+xml' in l_type) and l_href:
                    rss_link = urljoin(url, l_href)
                    break
                    
            if rss_link:
                try:
                    test_feed = feedparser.parse(rss_link)
                    if len(test_feed.entries) > 0:
                        return 'rss', rss_link, f"RSS feed auto-discovered in page header: {rss_link}"
                except Exception:
                    pass
                    
            # Check if common paths work as RSS
            common_rss_paths = ['/feed', '/feed/', '/rss', '/rss.xml', '?service=rss']
            for path in common_rss_paths:
                try:
                    test_url = urljoin(url, path)
                    test_res = requests.get(test_url, headers=headers, timeout=4)
                    if test_res.status_code == 200:
                        test_feed = feedparser.parse(test_url)
                        if len(test_feed.entries) > 0:
                            return 'rss', test_url, f"RSS feed auto-discovered at common path: {test_url}"
                except Exception:
                    pass
                    
            return 'web_link', url, "Accessible website with no RSS feed. Registered as regular Web Link."
            
        else:
            return 'web_link', url, f"Server responded with status code {res.status_code}. Registered as regular Web Link."
            
    except Exception as e:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        return 'web_link', url, f"Connection failed ({e}). Registered as Web Link (DrissionPage fallback)."


def fetch_google_news(team_query: str) -> list[dict]:
    """Simulates searching all of the internet by parsing Google News RSS feed for the query."""
    encoded_query = urllib.parse.quote_plus(team_query)
    feed_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    
    logger.info(f"Fetching Google News RSS feed for query: '{team_query}'")
    try:
        feed = feedparser.parse(feed_url)
        results = []
        for entry in feed.entries[:5]:  # Limit to top 5 news entries per check
            link = entry.get('link')
            unique_id = link
            title = entry.get('title', 'No Title')
            
            # Skip entries older than 24 hours (or if they lack a published date)
            pub_parsed = entry.get('published_parsed')
            if not pub_parsed:
                logger.info(f"Skipping Google News entry '{title}' - missing publication date.")
                continue
                
            try:
                import datetime
                pub_dt = datetime.datetime(*pub_parsed[:6], tzinfo=datetime.timezone.utc)
                now = datetime.datetime.now(datetime.timezone.utc)
                age = now - pub_dt
                if age > datetime.timedelta(hours=24):
                    logger.info(f"Skipping Google News entry '{title}' - older than 24 hours (published {age.days} days ago)")
                    continue
            except Exception as date_err:
                logger.warning(f"Could not parse Google News publication date for '{title}': {date_err}")
                continue
            content = entry.get('summary', entry.get('description', ''))
            image_url = extract_rss_image(entry)
            
            results.append({
                'id': unique_id,
                'title': title,
                'content': content,
                'media_url': image_url,
                'url': link
            })
        return results
    except Exception as e:
        logger.error(f"Error fetching Google News for {team_query}: {e}")
        return []


def run_gemini_summarizer(title: str, content: str, active_filters: list[str]) -> str | None:
    """Sends title & content to Google Gemini API (configured model) for summarization.
    Returns: 'SKIP' or the summarized social media post.
    """
    if not config.GEMINI_API_KEY:
        logger.error("Missing GEMINI_API_KEY. Cannot run AI Summarization Pipeline.")
        return None

    # Handle instances where input text is too short, missing, or malformed
    clean_title = (title or "").strip()
    clean_content = (content or "").strip()
    if not clean_title and not clean_content:
        logger.warning("Empty article title and content. Aborting summarization.")
        return 'SKIP'
    if len(clean_title) + len(clean_content) < 30:
        logger.info(f"Article text is too short ({len(clean_title) + len(clean_content)} chars). Skipping.")
        return 'SKIP'
        
    # Note: Local keyword filtering based on the 'filters' table is disabled per client request
    # to prevent filtering out valid transfer rumours.
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    
    system_instruction = (
        "You are an expert sports journalism and digital media copywriter specializing in real-time news curation for automated platforms. Your task is to process incoming text data or browse and extract information from a provided website link, article URL, or RSS feed source to generate highly dense, clear, and actionable news updates optimized for a fast-paced sports news feed.\n\n"
        "Strictly adhere to the following execution rules:\n\n"
        "1. EXTRACTING MULTIPLE TALKING POINTS (FOR LONG-FORM ARTICLES AND LINKS)\n"
        "- When a web link or long-form source text is provided, read through the entire article to extract the most critical, independent updates or talking points.\n"
        "- Do not combine the entire article into a single generic overview. Instead, identify the distinct news developments or key angles present within the text (e.g., player transfer status, contract negotiations, manager quotes, or tactical updates).\n"
        "- Generate exactly one compact, distinct update for each valid talking point identified. Separate each distinct talking point using the exact delimiter string: ---TALKING_POINT---\n\n"
        "2. COMPACT AND DIRECT STRUCTURE\n"
        "- For each identified talking point, deliver exactly one response block. Do not include multiple options, headings, intro text, or conversational filler.\n"
        "- Pack all critical factual data (names, clubs, specific monetary figures, dates, and historical context) into 1 to 3 tightly constructed sentences per talking point.\n\n"
        "3. HANDLING DIRECT QUOTES\n"
        "- When a talking point contains a direct quote from a manager, player, or official, first write a single, highly concise, and coherent plain sentence that summarizes what the quote is talking about.\n"
        "- Do not break up this summary intro with periods or unnecessary punctuation. Keep it short, fluid, and straight to the point.\n"
        "- End this single summary sentence with a colon (:).\n"
        "- Directly after the colon, insert the exact quote from the source text to ensure the direct quote is fully preserved under the summary.\n\n"
        "4. GRAMMAR, TONE, AND VOICE\n"
        "- Write with a spartan, informative, and authoritative tone.\n"
        "- Use the active voice exclusively; do not use passive voice constructions.\n"
        "- Address the core subject directly without setup phrases, introductory filler, or generic commentary.\n"
        "- Ensure every sentence is clear and punchy, maintaining absolute coherence without becoming verbose.\n\n"
        "5. FORCED PROPER NOUN REPETITION (BOXING EFFECT) AND EXCEPTION\n"
        "- When linking a specific player, club, or entity to an action, metric, or status, explicitly repeat that proper noun instead of relying on generic identifiers (e.g., the midfielder, the player, the club) or ambiguous pronouns.\n"
        "- EXCEPTION FOR THE SUBJECT'S OWN ACTIONS: Do not unnaturally repeat the identical subject's name twice within the same independent clause or immediate action string when it refers back to the self (e.g., do not write 'Thomas Tuchel confirmed that Thomas Tuchel will manage'). Use standard pronouns like 'he' or 'she' only when the subject is performing their own continuous action, ensuring the sentence remains grammatically coherent and natural while still enforcing proper noun repetition across different entities.\n\n"
        "6. STRICT PUNCTUATION AND SYMBOL RESTRICTIONS\n"
        "- Never use em dashes (— or --) under any circumstances. Use commas, periods, or parentheses to separate clauses.\n"
        "- Do not use semicolons, hashtags, or markdown formatting like bolding or asterisks (NO ** or *) in the final output text. Emojis are strictly forbidden.\n\n"
        "7. BANNED WORD FILTER\n"
        "- Do not use any of the following restricted words: can, may, just, that, very, really, literally, actually, certainly, probably, basically, could, maybe, delve, embark, enlightening, esteemed, shed light, craft, crafting, imagine, realm, game-changer, unlock, discover, skyrocket, abyss, not alone, in a world where, revolutionize, disruptive, utilize, utilizing, dive deep, tapestry, illuminate, unveil, pivotal, intricate, elucidate, hence, furthermore, however, moreover, in conclusion, in summary.\n\n"
        "8. SKIP CRITERIA (USELESS NEWS FILTER)\n"
        "- If the incoming text or article is completely devoid of hard facts, specific player names, transfer figures, or definite contract details (i.e. is generic gossip or clickbait), you MUST respond with exactly the word: SKIP\n\n"
        "9. LABELLING RUMOURS\n"
        "- If the original article title or text indicates that the transfer is a rumour or speculation (e.g. contains words like 'rumour', 'rumor', 'linked', 'speculation', 'speculated', 'tracked', 'monitored'), you MUST explicitly mention the word 'rumour' or 'rumours' in the generated summary."
    )
    
    prompt = f"Title: {clean_title}\n\nContent:\n{clean_content}"
    
    try:
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7,
            )
        )
        reply = response.text.strip()
        logger.info(f"Gemini Response: {reply[:100]}...")
        return reply
    except Exception as e:
        logger.error(f"Gemini API invocation error: {e}")
        return None


def detect_team_from_text(title: str, content: str, default_tag: str | None, allow_fallback: bool = True) -> str | None:
    """Analyzes the text content to dynamically assign the correct team tag.
    To prevent club mixing and cross-posting, it only checks keywords for the source's designated default_tag.
    If it matches the default_tag keywords, it returns that tag.
    Otherwise, if allow_fallback is True, it returns the default_tag; if False, it returns None.
    """
    if not default_tag or default_tag not in ('Arsenal', 'Liverpool', 'Inter'):
        return default_tag
        
    text = f"{title or ''}\n{content or ''}".lower()
    
    # Try loading from team_keywords.json, fallback to defaults if not found or error
    team_keywords = {}
    if os.path.exists("team_keywords.json"):
        try:
            with open("team_keywords.json", "r", encoding="utf-8") as f:
                team_keywords = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load team_keywords.json: {e}. Falling back to default keywords.")
            
    keywords = team_keywords.get(default_tag, [])
    if not keywords:
        # Fallback to default lists
        if default_tag == 'Arsenal':
            keywords = ['arsenal', 'gunners', 'arteta', 'saka', 'odegaard', 'saliba', 'rice', 'havertz', 'raya', 'emirates', 'hleb']
        elif default_tag == 'Liverpool':
            keywords = ['liverpool', 'reds', 'salah', 'van dijk', 'alisson', 'szoboszlai', 'nunez', 'luis diaz', 'mac allister', 'alexander-arnold', 'trent', 'slot', 'anfield', 'firmino', 'klopp']
        elif default_tag == 'Inter':
            keywords = ['inter milan', 'nerazzurri', 'inzaghi', 'lautaro', 'martinez', 'thuram', 'barella', 'calhanoglu', 'bastoni', 'sommer', 'san siro']
            
    matches = 0
    for kw in keywords:
        if kw.strip() and kw.lower() in text:
            matches += 1
            
    if matches > 0:
        logger.info(f"Confirmed team tag: {default_tag} (matches: {matches}) based on content.")
        return default_tag
        
    return default_tag if allow_fallback else None


def run_scraper_ingestion(x_scraper=None):
    """Loops through all sources in the database and background Google News feeds,
    fetches new articles, and saves them to SQLite.
    """
    logger.info("Starting Scraper Ingestion Cycle...")
    
    # 1. Fetch User-Configured Sources from DB
    sources = database.get_sources()
    
    # Cache sources by (type, value, team_tag) to optimize DB access
    global _sources_cache
    _sources_cache = {(s['type'], s['value'], s['team_tag']): s['id'] for s in sources}
    
    x_client = x_scraper if x_scraper is not None else XScraper()
    
    # Separate standard sources from Cloudflare-protected web link sources
    regular_sources = []
    protected_web_sources = []
    
    for src in sources:
        if src['type'] == 'web_link' and not 'transferfeed.com' in src['value']:
            from urllib.parse import urlparse
            parsed_url = urlparse(src['value'])
            domain = parsed_url.netloc.lower()
            if any(d in domain for d in PROTECTED_DOMAINS):
                protected_web_sources.append(src)
                continue
        regular_sources.append(src)
        
    # Process Regular Sources
    for src in regular_sources:
        source_id = src['id']
        source_type = src['type']
        value = src['value']
        team_tag = src['team_tag']
        
        logger.info(f"Scraping source: {source_type} - {value} ({team_tag})")
        
        if source_type == 'rss':
            try:
                feed = feedparser.parse(value)
                for entry in feed.entries[:5]:  # Process top 5 entries
                    link = entry.get('link')
                    unique_id = link
                    if not unique_id or database.article_exists(unique_id):
                        continue
                        
                    title = entry.get('title', 'No Title')
                    
                    # Skip entries older than 24 hours (or if they lack a published date)
                    pub_parsed = entry.get('published_parsed')
                    if not pub_parsed:
                        logger.info(f"Skipping RSS entry '{title}' - missing publication date.")
                        continue
                        
                    try:
                        import datetime
                        pub_dt = datetime.datetime(*pub_parsed[:6], tzinfo=datetime.timezone.utc)
                        now = datetime.datetime.now(datetime.timezone.utc)
                        age = now - pub_dt
                        if age > datetime.timedelta(hours=24):
                            logger.info(f"Skipping RSS entry '{title}' - older than 24 hours (published {age.days} days ago)")
                            continue
                    except Exception as date_err:
                        logger.warning(f"Could not parse RSS publication date for '{title}': {date_err}")
                        continue
                    # Scrape full text from web page for better quality content
                    web_title, web_content, web_image = scrape_web_page(link)
                    
                    content = web_content if web_content else entry.get('summary', entry.get('description', ''))
                    image_url = web_image if web_image else extract_rss_image(entry)
                    
                    detected_team = detect_team_from_text(title, content, team_tag)
                    database.save_article(source_id, unique_id, title, content, image_url, detected_team)
            except Exception as e:
                logger.error(f"Error processing RSS source {value}: {e}")
                
        elif source_type == 'web_link':
            if 'transferfeed.com' in value:
                try:
                    logger.info(f"Processing TransferFeed Hub: {value}")
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.google.com/"
                    }
                    res = requests.get(value, headers=headers, timeout=12)
                    if res.status_code == 200:
                        soup = BeautifulSoup(res.text, 'html.parser')
                        items = []
                        for tag in soup.find_all('a'):
                            href = tag.get('href', '')
                            text = tag.get_text().strip()
                            if href and '/transfers/' in href and text and len(text) > 10:
                                full_url = href if href.startswith('http') else f"https://www.transferfeed.com{href}"
                                items.append((text, full_url))
                        
                        logger.info(f"Extracted {len(items)} rumours from TransferFeed.")
                        # Process top 5 latest rumours
                        for title_text, rumour_url in items[:5]:
                            if not database.article_exists(rumour_url):
                                logger.info(f"Scraping new TransferFeed rumour: {rumour_url}")
                                web_title, web_content, web_image = scrape_web_page(rumour_url)
                                title = web_title if web_title else title_text
                                content = web_content if web_content else title_text
                                detected_team = detect_team_from_text(title, content, team_tag, allow_fallback=False)
                                if not detected_team:
                                    logger.info(f"TransferFeed rumour '{title}' does not match any target clubs. Skipping Ingestion.")
                                    continue
                                database.save_article(source_id, rumour_url, title, content, web_image, detected_team)
                except Exception as e:
                    logger.error(f"Error processing TransferFeed Hub {value}: {e}")
            else:
                # Check if this link itself was processed
                if not database.article_exists(value):
                    title, content, image_url = scrape_web_page(value)
                    if title or content:
                        detected_team = detect_team_from_text(title, content, team_tag)
                        database.save_article(source_id, value, title, content, image_url, detected_team)
                    
        elif source_type == 'x_account':
            try:
                tweets = x_client.get_latest_tweets(value, team_tag)
                for t in tweets:
                    if not database.article_exists(t['id']):
                        detected_team = detect_team_from_text(t['title'], t['content'], team_tag)
                        database.save_article(source_id, t['id'], t['title'], t['content'], t['media_url'], detected_team)
            except Exception as e:
                logger.error(f"Error processing X account {value}: {e}")

    # Process Cloudflare-Protected Web Sources (Throttled Headless Chromium)
    if protected_web_sources:
        logger.info(f"Starting DrissionPage batch scraping for {len(protected_web_sources)} protected sources...")
        from DrissionPage import ChromiumPage, ChromiumOptions
        import datetime
        
        co = ChromiumOptions()
        co.headless(True)
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')
        co.set_user_agent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        page = None
        try:
            page = ChromiumPage(co)
            
            for idx, src in enumerate(protected_web_sources, 1):
                source_id = src['id']
                value = src['value']
                team_tag = src['team_tag']
                
                logger.info(f"[{idx}/{len(protected_web_sources)}] DrissionPage scraping author: {value} ({team_tag})")
                
                try:
                    page.get(value)
                    time.sleep(5)
                    author_html = page.html
                    
                    article_urls = extract_articles_from_author_page(value, author_html)
                    logger.info(f"Found {len(article_urls)} articles for author {value}")
                    
                    for art_url in article_urls:
                        if not database.article_exists(art_url):
                            logger.info(f"Scraping protected article details: {art_url}")
                            
                            page.get(art_url)
                            time.sleep(5)
                            art_html = page.html
                            
                            pub_dt = extract_article_published_date(art_html)
                            if not pub_dt:
                                logger.info(f"Skipping article '{art_url}' - missing/unparseable publication date.")
                                continue
                                 
                            now = datetime.datetime.now(datetime.timezone.utc)
                            age = now - pub_dt
                            if age > datetime.timedelta(hours=24):
                                logger.info(f"Skipping article '{art_url}' - older than 24 hours (published {age.days} days ago)")
                                continue
                                    
                            title, content, image_url = parse_article_html(art_html)
                            if title or content:
                                detected_team = detect_team_from_text(title, content, team_tag)
                                database.save_article(source_id, art_url, title, content, image_url, detected_team)
                                
                    # Sequential sleep to avoid rate limiting and VPS RAM spikes
                    if idx < len(protected_web_sources):
                        delay = random.randint(15, 20)
                        logger.info(f"Staggering: Sleeping for {delay} seconds before next author...")
                        time.sleep(delay)
                        
                except Exception as e:
                    logger.error(f"Error scraping protected source {value}: {e}")
                    
        except Exception as init_err:
            logger.error(f"Failed to initialize DrissionPage: {init_err}")
        finally:
            if page:
                try:
                    page.quit()
                    logger.info("Successfully terminated Headless Chromium browser session.")
                except Exception as quit_err:
                    logger.error(f"Error closing ChromiumPage: {quit_err}")
                
    # 2. Run Global Google News Feeds (Mentions Cover)
    google_queries = {
        'Arsenal': '"Arsenal FC"',
        'Liverpool': '"Liverpool FC"',
        'Inter': '"Inter Milan"'
    }
    
    for team, query in google_queries.items():
        articles = fetch_google_news(query)
        for a in articles:
            # Use clickable URL as the unique identifier for checks and saves
            article_url = a['url']
            if not database.article_exists(article_url):
                # Resolve the Google News link to the actual page to scrape content and image
                resolved_title, resolved_content, resolved_image = scrape_web_page(article_url)
                
                # Verify if resolved content is valid and not a paywall/consent wall
                is_valid = True
                if resolved_content:
                    garbage_keywords = [
                        'accept cookies', 'cookie policy', 'privacy policy', 'terms of service', 
                        'adblocker', 'subscribe to read', 'premium content', 'sign in', 'log in', 
                        'enable javascript'
                    ]
                    content_lower = resolved_content.lower()
                    if any(kw in content_lower for kw in garbage_keywords):
                        is_valid = False
                    
                    # Must contain team tag or general football terms to be valid
                    football_terms = [team.lower(), 'football', 'soccer', 'transfer', 'player', 'match', 'league', 'cup']
                    if not any(term in content_lower for term in football_terms):
                        is_valid = False
                else:
                    is_valid = False
                    
                if resolved_content and is_valid:
                    content = resolved_content
                    title = resolved_title if resolved_title else a['title']
                    image = resolved_image if resolved_image else a['media_url']
                else:
                    # Fallback to high-quality RSS metadata provided by Google News
                    content = a['content']
                    title = a['title']
                    image = a['media_url']
                
                # Find or create the virtual system source for database schema integrity
                system_source_value = f"system_google_news_{team.lower()}"
                cache_key = ('rss', system_source_value, team)
                system_source_id = _sources_cache.get(cache_key)
                
                if not system_source_id:
                    database.add_source('rss', system_source_value, team)
                    # Re-fetch and update cache
                    updated_sources = database.get_sources()
                    _sources_cache = {(s['type'], s['value'], s['team_tag']): s['id'] for s in updated_sources}
                    system_source_id = _sources_cache.get(cache_key)
                            
                detected_team = detect_team_from_text(title, content, team, allow_fallback=False)
                if not detected_team:
                    logger.info(f"Google News article '{title}' does not match any target clubs. Skipping Ingestion.")
                    continue
                database.save_article(system_source_id, article_url, title, content, image, detected_team)
                
    logger.info("Scraper Ingestion Cycle Completed.")



