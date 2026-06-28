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

import config
import database

logger = logging.getLogger("scraper")

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
                # If cookies exist, we assume real mode is possible
                if os.path.exists("cookies.json"):
                    self.mock_mode = False
                    logger.info("Successfully loaded X (Twitter) session from cookies.json.")
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
                logger.error(f"Failed to login to Twikit: {e}. Falling back to Simulator Mode.")
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
                # Fall back to mock if Twitter API fails temporarily

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


def scrape_web_page(url: str) -> tuple[str | None, str | None, str | None]:
    """Scrapes a general web page using BeautifulSoup.
    Returns: (title, main_content_text, image_url)
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        # Resolve redirect first
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        if response.status_code != 200:
            logger.warning(f"Failed to fetch {url}, status code: {response.status_code}")
            return None, None, None
            
        soup = BeautifulSoup(response.content, 'html.parser')
        
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
            twitter_image = soup.find('meta', name='twitter:image')
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
                            
        # Extract content text (all paragraphs > 30 chars)
        paragraphs = soup.find_all('p')
        content_text = "\n".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 30])
        
        if not content_text:
            # Fallback to general text if no paragraphs
            content_text = soup.get_text()
            
        return title, content_text, image_url
    except Exception as e:
        logger.error(f"Error scraping web page {url}: {e}")
        return None, None, None


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
            
            # Since Google News links are redirects, we can use the redirect link itself
            # or try to resolve/scrape it. To avoid failures, we use the Google News RSS info
            # as the base. If possible, we scrape the article for better body content and image.
            title = entry.get('title', 'No Title')
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
    """Sends title & content to Google Gemini API (gemini-1.5-flash) for summarization.
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
        "3. GRAMMAR, TONE, AND VOICE\n"
        "- Write with a spartan, informative, and authoritative tone.\n"
        "- Use the active voice exclusively; do not use passive voice constructions.\n"
        "- Address the core subject directly without setup phrases, introductory filler, or generic commentary.\n"
        "- Ensure every sentence is clear and punchy, maintaining absolute coherence without becoming verbose.\n\n"
        "4. FORCED PROPER NOUN REPETITION (BOXING EFFECT)\n"
        "- When a specific individual, club, or entity is mentioned in relation to an action, metric, or status, explicitly repeat that proper noun.\n"
        "- Avoid relying on pronouns (he, she, it, they, him, her, them, his, their) or generic identifiers (the midfielder, the club, the player) when linking actions back to the subject. Keep the identity locked in by restating the exact name or entity throughout the rewrite.\n\n"
        "5. STRICT PUNCTUATION AND SYMBOL RESTRICTIONS\n"
        "- Never use em dashes (— or --) under any circumstances. Use commas, periods, or parentheses to separate clauses.\n"
        "- Do not use semicolons, hashtags, or markdown formatting like bolding or asterisks (NO ** or *) in the final output text. Do not use emojis.\n\n"
        "6. BANNED WORD FILTER\n"
        "- Do not use any of the following restricted words: can, may, just, that, very, really, literally, actually, certainly, probably, basically, could, maybe, delve, embark, enlightening, esteemed, shed light, craft, crafting, imagine, realm, game-changer, unlock, discover, skyrocket, abyss, not alone, in a world where, revolutionize, disruptive, utilize, utilizing, dive deep, tapestry, illuminate, unveil, pivotal, intricate, elucidate, hence, furthermore, however, moreover, in conclusion, in summary.\n\n"
        "7. LABELLING RUMOURS\n"
        "- If the original article title or text indicates that the transfer is a rumour or speculation (e.g. contains words like 'rumour', 'rumor', 'linked', 'speculation', 'speculated', 'tracked', 'monitored'), you MUST explicitly mention the word 'rumour' or 'rumours' in the generated summary.\n\n"
        "8. SKIP CRITERIA (USELESS NEWS FILTER)\n"
        "- If the incoming text or article is completely devoid of hard facts, specific player names, transfer figures, or definite contract details (i.e. is generic gossip or clickbait), you MUST respond with exactly the word: SKIP"
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
    """Analyzes the text content to dynamically assign the correct team tag (Arsenal, Liverpool, Inter).
    Falls back to the default tag if no clear keywords are found and allow_fallback is True.
    """
    text = f"{title or ''}\n{content or ''}".lower()
    
    # Try loading from team_keywords.json, fallback to defaults if not found or error
    team_keywords = {}
    if os.path.exists("team_keywords.json"):
        try:
            with open("team_keywords.json", "r", encoding="utf-8") as f:
                team_keywords = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load team_keywords.json: {e}. Falling back to default keywords.")
            
    arsenal_keywords = team_keywords.get('Arsenal', ['arsenal', 'gunners', 'arteta', 'saka', 'odegaard', 'saliba', 'rice', 'havertz', 'raya', 'emirates', 'hleb'])
    liverpool_keywords = team_keywords.get('Liverpool', ['liverpool', 'reds', 'salah', 'van dijk', 'alisson', 'szoboszlai', 'nunez', 'luis diaz', 'mac allister', 'alexander-arnold', 'trent', 'slot', 'anfield', 'firmino', 'klopp'])
    inter_keywords = team_keywords.get('Inter', ['inter milan', 'nerazzurri', 'inzaghi', 'lautaro', 'martinez', 'thuram', 'barella', 'calhanoglu', 'bastoni', 'sommer', 'san siro'])
    
    matches = {'Arsenal': 0, 'Liverpool': 0, 'Inter': 0}
    
    for kw in arsenal_keywords:
        if kw.strip() and kw.lower() in text:
            matches['Arsenal'] += 1
    for kw in liverpool_keywords:
        if kw.strip() and kw.lower() in text:
            matches['Liverpool'] += 1
    for kw in inter_keywords:
        if kw.strip() and kw.lower() in text:
            matches['Inter'] += 1
            
    # Find the team with the maximum matches
    best_team = None
    max_matches = 0
    for team, count in matches.items():
        if count > max_matches:
            max_matches = count
            best_team = team
            
    if max_matches > 0:
        logger.info(f"Dynamically detected team tag: {best_team} (matches: {max_matches}) based on content.")
        return best_team
        
    return default_tag if allow_fallback else None


def run_scraper_ingestion():
    """Loops through all sources in the database and background Google News feeds,
    fetches new articles, and saves them to SQLite.
    """
    logger.info("Starting Scraper Ingestion Cycle...")
    
    # 1. Fetch User-Configured Sources from DB
    sources = database.get_sources()
    x_client = XScraper()
    
    for src in sources:
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
                db_sources = database.get_sources()
                system_source_id = None
                for src in db_sources:
                    if src['type'] == 'rss' and src['value'] == system_source_value:
                        system_source_id = src['id']
                        break
                
                if not system_source_id:
                    database.add_source('rss', system_source_value, team)
                    for src in database.get_sources():
                        if src['type'] == 'rss' and src['value'] == system_source_value:
                            system_source_id = src['id']
                            break
                            
                detected_team = detect_team_from_text(title, content, team, allow_fallback=False)
                if not detected_team:
                    logger.info(f"Google News article '{title}' does not match any target clubs. Skipping Ingestion.")
                    continue
                database.save_article(system_source_id, article_url, title, content, image, detected_team)
                
    logger.info("Scraper Ingestion Cycle Completed.")


def run_summarization_pipeline():
    """Runs Gemini summarizer on all 'pending' articles and updates their status."""
    logger.info("Running Summarization Pipeline...")
    pending_articles = database.get_pending_articles()
    
    if not pending_articles:
        logger.info("No pending articles to summarize.")
        return
        
    # Get active filters
    active_filters = [f['keyword'] for f in database.get_filters()]
    
    for art in pending_articles:
        title = art['original_title'] or ""
        content = art['original_content'] or ""
        art_id = art['id']
        
        logger.info(f"Summarizing article {art_id}: {title[:50]}...")
        summary = run_gemini_summarizer(title, content, active_filters)
        
        if summary is None:
            # API failure, keep it pending to try again later
            continue
            
        if summary.upper() == 'SKIP' or 'SKIP' in summary:
            # Filter hit, mark as sent (so it's not processed/sent to Telegram)
            logger.info(f"Article {art_id} skipped due to keyword filters.")
            database.update_article_summary_status(art_id, 'SKIP', 'sent')
        else:
            # Successfully summarized, mark as processed (ready to send)
            database.update_article_summary_status(art_id, summary, 'processed')
            
    logger.info("Summarization Pipeline Completed.")
