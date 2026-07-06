import os
import re
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
]
# Note: mirror.co.uk, liverpoolecho.co.uk (and football.london, givemesport.com) are
# handled via the residential HTTP proxy path (config.PROXY_DOMAINS), not the browser.

# Last-fetch time per proxy-routed source URL, to throttle metered proxy traffic.
_proxy_last_fetch = {}

# Domains that are not Cloudflare-blocked but render their article feed with
# client-side JavaScript, so plain requests returns no article links. These are
# routed through the headless-browser (DrissionPage) path just like protected sites.
JS_RENDERED_DOMAINS = [
    'goal.com', 'sports.yahoo.com'
]


def _needs_browser(domain: str) -> bool:
    """True if the domain needs a headless browser (Cloudflare-protected or JS-rendered)."""
    domain = (domain or '').lower()
    return any(d in domain for d in PROTECTED_DOMAINS) or any(d in domain for d in JS_RENDERED_DOMAINS)


_drission_patched = False

def _patch_drission_websocket():
    """Chrome 132+ rejects DevTools WebSocket connections whose Host header is an IP
    address (it returns HTTP 404 on the handshake). DrissionPage 4.1.1.4 connects via
    127.0.0.1, so we force the Host header to 'localhost'. Without this, all headless
    Chromium scraping fails on modern Chrome. Idempotent."""
    global _drission_patched
    if _drission_patched:
        return
    try:
        import DrissionPage._base.driver as drv
        _orig_create_connection = drv.create_connection

        def _patched_create_connection(url, **kwargs):
            kwargs.setdefault('host', 'localhost')
            return _orig_create_connection(url, **kwargs)

        drv.create_connection = _patched_create_connection
        _drission_patched = True
        logger.info("Applied DrissionPage WebSocket Host-header patch (Chrome 132+ compatibility).")
    except Exception as e:
        logger.warning(f"Could not patch DrissionPage WebSocket: {e}")

class XScraper:
    """Handles fetching tweets from X (Twitter) using Twikit.
    If credentials are missing or login fails, live ingestion is disabled and NO
    tweets are produced. This scraper never fabricates/simulates tweet data.
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
                logger.error(f"X login/verification failed: {e}. X live ingestion disabled (no tweets will be produced).")
                self.mock_mode = True
        else:
            logger.info("X credentials not fully provided. X live ingestion disabled (no tweets will be produced).")
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

                    # Detect retweets. twikit exposes the original as `retweeted_tweet`.
                    rt = getattr(t, 'retweeted_tweet', None)
                    is_retweet = rt is not None or t.text.strip().upper().startswith('RT ')

                    if is_retweet and not config.X_INCLUDE_RETWEETS:
                        logger.info(f"Skipping Retweet from @{handle}: {t.text[:50]}...")
                        continue
                    if is_retweet and rt is None:
                        # A retweet we can't expand to the full original (would only have the
                        # truncated "RT @user: ..." wrapper) — skip to keep posts clean.
                        logger.info(f"Skipping unexpandable retweet from @{handle}: {t.text[:50]}...")
                        continue

                    # Skip Replies (only applies to original tweets; retweets are not replies)
                    if not is_retweet:
                        if t.text.strip().startswith('@') or getattr(t, 'in_reply_to', None) is not None:
                            logger.info(f"Skipping Reply tweet from @{handle}: {t.text[:50]}...")
                            continue

                    # Skip items older than 24 hours. For a retweet this uses the retweet
                    # time (when THIS account surfaced it), so active curators come through.
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

                    # Resolve the source tweet: for a retweet, use the ORIGINAL tweet's full
                    # text, author, id and media (post the complete original, not the
                    # truncated wrapper) and de-duplicate by the original tweet id.
                    if rt is not None:
                        src = rt
                        src_author = getattr(getattr(rt, 'user', None), 'screen_name', None) or handle
                        content_text = getattr(rt, 'full_text', None) or rt.text or t.text
                        title = f"Update from @{handle} (RT @{src_author})"
                    else:
                        src = t
                        src_author = handle
                        content_text = getattr(t, 'full_text', None) or t.text
                        title = f"Update from @{handle}"

                    # Extract media from the source tweet if present
                    media_url = None
                    src_media = getattr(src, 'media', None)
                    if src_media:
                        for media in src_media:
                            if getattr(media, 'type', None) == 'photo' and getattr(media, 'media_url', None):
                                media_url = media.media_url
                                break

                    tweet_url = f"https://x.com/{src_author}/status/{src.id}"
                    result.append({
                        'id': tweet_url,
                        'title': title,
                        'content': content_text,
                        'media_url': media_url,
                        'url': tweet_url
                    })
                return result

            try:
                return asyncio.run(_async_fetch())
            except Exception as e:
                logger.error(f"Error fetching tweets for @{handle}: {e}. Skipping this account this cycle.")
                return []

        # No live X session: do NOT fabricate tweets. Returning simulated content here
        # posted fake transfer news to the channel, and because each mock id embedded a
        # timestamp it was always "new" so the duplicate check never suppressed it.
        logger.warning(f"X session unavailable (mock mode); skipping @{handle} instead of simulating.")
        return []


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


# Promo/UI boilerplate lines to drop from extracted article text (e.g. Metro injects
# newsletter sign-ups, "Use AI to go deeper" widgets, and video-modal dialog text that
# dilute the real content and quotes sent to Gemini).
_BOILERPLATE_MARKERS = (
    'use ai to go deeper', 'sign up', 'newsletter', 'powered by metro',
    'get it all in our daily', 'this is a modal window', 'escape will cancel',
    'beginning of dialog', 'end of dialog window', 'activating the close button',
    'advertisement', 'follow metro on',
)


def _is_boilerplate(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in _BOILERPLATE_MARKERS)


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
            paragraphs_text = [t for p in paragraphs
                               if len(t := p.get_text().strip()) > 15 and not _is_boilerplate(t)]
            content_text = "\n\n".join(paragraphs_text)

        if not content_text:
            paragraphs = soup.find_all('p')
            paragraphs_text = [t for p in paragraphs
                               if len(t := p.get_text().strip()) > 15 and not _is_boilerplate(t)]
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
        # Proxy-listed domains (Reach plc etc.) are fetched through the residential proxy.
        if _domain_uses_proxy(url):
            html = fetch_via_proxy(url)
            return parse_article_html(html) if html else (None, None, None)
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


def scrape_transferfeed_latest(url: str) -> tuple[str | None, str | None, str | None]:
    """Scrapes a TransferFeed player transfer page and returns ONLY the most recent
    update, not the full history. TransferFeed lists every update for a player in
    `.transfer-news-card` blocks (newest first, tagged `--recent`); we take just the
    first one. Returns (title, latest_update_text, image_url)."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.google.com/',
        }
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code != 200:
            logger.warning(f"Failed to fetch TransferFeed page {url}: status {res.status_code}")
            return None, None, None
        soup = BeautifulSoup(res.text, 'html.parser')

        title_tag = soup.find('h1') or soup.find('title')
        title = title_tag.get_text().strip().replace(' - TransferFeed', '') if title_tag else None

        cards = soup.select('.transfer-news-card')
        latest = cards[0].get_text(' ', strip=True) if cards else None

        image = None
        og = soup.find('meta', property='og:image')
        if og and og.get('content'):
            image = og['content']

        return title, latest, image
    except Exception as e:
        logger.error(f"Error scraping TransferFeed page {url}: {e}")
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


# Per-domain CSS selectors pointing at the container that holds the article feed/list.
# Link extraction is restricted to these containers so the crawler does not harvest
# unrelated URLs from site headers, footers, nav bars, or trending/most-read widgets.
# Selectors are tried in order; the first one that matches wins. <main> is the last,
# most generic option before falling back to the whole (de-noised) document.
_FEED_CONTAINER_SELECTORS = {
    'skysports.com': ['.news-list', '.sdc-site-tiles', '.page__main', 'main'],
    'mirror.co.uk': ['.publication-body', '[data-component="ArticleList"]', 'main'],
    'liverpoolecho.co.uk': ['.publication-body', '[data-component="ArticleList"]', 'main'],
    'thesun.co.uk': ['.sun-row', '.teadit', '.feed', 'main', '#content'],
    'teamtalk.com': ['.archive-list', '.posts', 'main', '#main', '.site-main'],
    'dailymail.co.uk': ['.author-articles', '#content', 'main'],
    'dailymail.com': ['.author-articles', '#content', 'main'],
    'telegraph.co.uk': ['.card-grid', '.card__content', 'main'],
    'theguardian.com': ['#maincontent', 'main'],
    'independent.co.uk': ['.author-page', 'main'],
    'standard.co.uk': ['main'],
    'thetimes.co.uk': ['main'],
    'thetimes.com': ['main'],
    'theathletic.com': ['main'],
    'nytimes.com': ['main'],
}

# Class/id substrings that mark non-feed chrome (nav, promos, related/trending widgets).
_NOISE_TAGS = ('header', 'footer', 'nav', 'aside')
_NOISE_KEYWORDS = (
    'header', 'footer', 'nav', 'menu', 'masthead', 'breadcrumb',
    'related', 'trending', 'most-read', 'most-popular', 'popular',
    'promo', 'sidebar', 'newsletter', 'subscribe', 'social', 'share',
    'recommend', 'sponsor', 'advert', 'banner', 'cookie',
    'also-read', 'more-on', 'you-may', 'read-more', 'outbrain', 'taboola',
    'video-playlist', 'betting', 'odds',
    # NOTE: do not add broad words like 'latest'/'watch'/'editor' here -- they
    # often appear in the class of the MAIN feed container ("latest news") and
    # would cause us to strip the very articles we want.
)


def _decompose_noise(soup) -> None:
    """Strips header/footer/nav/aside and promo/trending/related widgets from the soup,
    in place, so that even fallback link scanning stays inside real content."""
    # Never decompose structural roots: sites put feature-flag classes (e.g.
    # 'subscribe', 'logged-in') on <html>/<body>, which would otherwise match a
    # noise keyword and wipe out the entire document.
    _skip = ('html', 'body', 'main')
    targets = list(soup.find_all(_NOISE_TAGS))
    for el in soup.find_all(attrs={'class': True}):
        if el.name in _skip:
            continue
        classes = ' '.join(el.get('class', [])).lower()
        if any(kw in classes for kw in _NOISE_KEYWORDS):
            targets.append(el)
    for el in soup.find_all(attrs={'id': True}):
        if el.name in _skip:
            continue
        if any(kw in (el.get('id') or '').lower() for kw in _NOISE_KEYWORDS):
            targets.append(el)
    for el in targets:
        try:
            el.decompose()
        except Exception:
            # Element may already be detached because a parent was decomposed first.
            pass


def _resolve_feed_containers(soup, author_domain: str) -> list:
    """Returns the elements that hold the article feed for a domain, after removing
    chrome. Falls back to <main>/[role=main]. For known portals (those listed in
    _FEED_CONTAINER_SELECTORS) it deliberately does NOT fall back to the whole
    document, so a selector miss yields nothing rather than harvesting the entire
    site (the 'scraping the whole site' problem on big portals like The Sun)."""
    _decompose_noise(soup)

    selectors = []
    is_known_portal = False
    for known_domain, sels in _FEED_CONTAINER_SELECTORS.items():
        if known_domain in author_domain:
            selectors = sels
            is_known_portal = True
            break

    for selector in selectors:
        containers = soup.select(selector)
        if containers:
            return containers

    containers = soup.select('main, [role="main"]')
    if containers:
        return containers

    # No container matched. For portals whose article-URL rule is broad or whose
    # recirculation widgets are aggressive, scanning the whole document risks
    # whole-site harvesting, so extract nothing instead. Other domains have a
    # precise per-domain URL rule, so the de-noised whole document is safe.
    _STRICT_NO_FALLBACK = ('thesun.co.uk',)
    if any(d in author_domain for d in _STRICT_NO_FALLBACK):
        logger.warning(f"No feed container matched for '{author_domain}'; skipping to "
                       "avoid whole-site scraping. Selector tuning needed.")
        return []
    return [soup]


def _is_article_url(author_domain: str, url_path: str) -> bool:
    """Domain-aware test: does this path point to a specific article (not a hub,
    tag page, topic page, or author landing page)?"""
    if 'athletic' in author_domain or 'nytimes.com' in author_domain:
        parts = [p for p in url_path.split('/') if p]
        return len(parts) >= 2 and parts[0] == 'athletic' and parts[1].isdigit()

    if 'thetimes.co.uk' in author_domain or 'thetimes.com' in author_domain:
        return '/article/' in url_path

    if 'telegraph.co.uk' in author_domain or 'theguardian.com' in author_domain:
        return any(f'/{year}/' in url_path for year in ('2025', '2026', '2027'))

    if 'independent.co.uk' in author_domain:
        return url_path.endswith('.html') and '/author/' not in url_path

    if 'standard.co.uk' in author_domain:
        # Standard articles: /sport/football/<slug>-b<numeric-id>.html
        return bool(re.search(r'/sport/football/.+-b\d+\.html$', url_path))

    if 'dailymail.co.uk' in author_domain or 'dailymail.com' in author_domain:
        # Require /football/ so other sections (/sport/tennis/, /sport/boxing/, ...) are
        # excluded. DailyMail football articles are /sport/football/article-<id>/...
        return '/football/' in url_path and '/article-' in url_path and url_path.endswith('.html')

    if 'thesun.co.uk' in author_domain:
        # Require /football/ so /sport/<id>/... tennis/other-sport articles are excluded.
        if '/football/' in url_path:
            parts = [p for p in url_path.split('/') if p]
            return len(parts) >= 3 and any(p.isdigit() for p in parts)
        return False

    if 'skysports.com' in author_domain:
        return '/football/news/' in url_path

    if 'teamtalk.com' in author_domain:
        # Articles live under section slugs like /arsenal/<slug> or /news/<slug>;
        # the final segment of a real article is a hyphenated headline slug.
        # Exclude author/tag/category hubs, section landing pages, and pagination.
        parts = [p for p in url_path.split('/') if p]
        if not parts or parts[0] in ('author', 'authors', 'tag', 'tags', 'category', 'categories', 'page'):
            return False
        return len(parts) >= 2 and '-' in parts[-1] and 'page' not in parts

    if 'metro.co.uk' in author_domain:
        # Metro articles: /YYYY/MM/DD/<slug>-<id>/
        parts = [p for p in url_path.split('/') if p]
        return (len(parts) >= 4 and len(parts[0]) == 4 and parts[0].isdigit()
                and parts[1].isdigit() and parts[2].isdigit())

    if 'hayters.com' in author_domain:
        # Hayters articles are top-level hyphenated slugs: /<headline-slug>/
        parts = [p for p in url_path.split('/') if p]
        if not parts or parts[0] in ('author', 'authors', 'category', 'tag', 'tags', 'page'):
            return False
        return len(parts) == 1 and '-' in parts[0]

    if 'football.london' in author_domain:
        # Reach plc article: /<club>-fc/<section>/<slug>-<numeric-id>
        return bool(re.search(r'-\d{6,}$', url_path.rstrip('/')))

    if 'goal.com' in author_domain:
        # Goal article: /<locale>/lists/<slug>/blt<alphanumeric-id>
        parts = [p for p in url_path.split('/') if p]
        return any(p.startswith('blt') for p in parts)

    if 'sports.yahoo.com' in author_domain:
        # Yahoo article: /articles/<slug>-<numeric-id>.html
        return '/articles/' in url_path and url_path.endswith('.html')

    # Fallback for Reach plc (liverpoolecho, mirror) and other domains.
    # Reach hosts betting/affiliate content under /sport/ too, so exclude it explicitly.
    _BETTING = ('betting', 'odds', 'free-bet', 'free-bets', 'bookmaker', 'bet365',
                'casino', 'gambling', '/tips/', 'acca', 'promo', 'sign-up-offer')
    if any(b in url_path for b in _BETTING):
        return False
    return (('/sport/' in url_path or '/football/' in url_path)
            and not any(x in url_path for x in
                        ('/author/', '/tag/', '/topic/', '/category/', '/all-about/', '/rss/')))


def extract_articles_from_author_page(author_url: str, html_text: str) -> list[str]:
    """Parses the HTML of an author/topic page and extracts the top 5 article detail URLs.

    Link extraction is restricted to the page's article-feed container(s) so that links
    from headers, footers, nav bars, and trending/most-read widgets are never queued.
    Avoids returning the author/topic URL itself, topic hubs, or non-article links.
    """
    if not html_text:
        return []

    from urllib.parse import urljoin, urlparse, urldefrag

    soup = BeautifulSoup(html_text, 'html.parser')
    parsed_author = urlparse(author_url)
    author_domain = parsed_author.netloc.lower()

    containers = _resolve_feed_containers(soup, author_domain)

    links = []
    seen = set()

    for container in containers:
        for tag in container.find_all('a'):
            href = tag.get('href', '').strip()
            if not href or href.startswith('#') or href.startswith('javascript:'):
                continue

            full_url = urldefrag(urljoin(author_url, href))[0]  # drop #fragments
            parsed_url = urlparse(full_url)

            # Stay on the same domain and skip the author/topic landing page itself.
            if parsed_url.netloc.lower() != author_domain:
                continue
            url_path = parsed_url.path
            if url_path.rstrip('/') == parsed_author.path.rstrip('/'):
                continue

            if _is_article_url(author_domain, url_path) and full_url not in seen:
                seen.add(full_url)
                links.append(full_url)

    return links[:5]


def _domain_uses_proxy(url: str) -> bool:
    """True if this URL's domain should be fetched through the residential scraping proxy."""
    if not getattr(config, 'SCRAPER_PROXY_URL', None):
        return False
    from urllib.parse import urlparse
    domain = (urlparse(url).netloc or '').lower()
    return any(d in domain for d in getattr(config, 'PROXY_DOMAINS', []))


def fetch_via_proxy(url: str, timeout: int = 35) -> str | None:
    """Fetches a URL through the residential proxy using curl_cffi with a browser TLS
    fingerprint (the WAFs on these sites block the datacenter IP AND the default TLS, so we
    need both a residential IP and a browser-like handshake). HTML only — no assets — so
    proxy data stays low. Returns HTML on 200, else None."""
    try:
        from curl_cffi import requests as cffi
        proxies = {'http': config.SCRAPER_PROXY_URL, 'https': config.SCRAPER_PROXY_URL}
        res = cffi.get(
            url, impersonate=config.PROXY_IMPERSONATE, proxies=proxies, timeout=timeout,
            headers={'Accept-Language': 'en-GB,en;q=0.9'},
        )
        if res.status_code == 200:
            return res.text
        logger.warning(f"Proxy fetch failed for {url}, status code: {res.status_code}")
    except Exception as e:
        logger.error(f"Error proxy-fetching {url}: {e}")
    return None


def _fetch_html(url: str, timeout: int = 10) -> str | None:
    """Fetches raw HTML. Proxy-listed domains go through the residential proxy; everything
    else uses a normal HTTP request."""
    if _domain_uses_proxy(url):
        return fetch_via_proxy(url, timeout=max(timeout, 35))
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        res = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if res.status_code == 200:
            return res.text
        logger.warning(f"Failed to fetch {url}, status code: {res.status_code}")
    except Exception as e:
        logger.error(f"Error fetching HTML for {url}: {e}")
    return None


def _ingest_feed_article_urls(article_urls: list[str], source_id, team_tag: str,
                              allow_fallback: bool = False) -> None:
    """Scrapes each article URL via plain HTTP, applies the strict 24h + relevance
    filters, and saves new articles. Used for non-Cloudflare feed/author/team pages
    (e.g. teamtalk.com/arsenal). allow_fallback=False drops articles whose text does
    not match the source's club, which prevents cross-club mixing on section pages."""
    import datetime
    for art_url in article_urls:
        if database.article_exists(art_url):
            continue

        art_html = _fetch_html(art_url)
        if not art_html:
            continue

        pub_dt = extract_article_published_date(art_html)
        if not pub_dt:
            logger.info(f"Skipping article '{art_url}' - missing/unparseable publication date.")
            continue
        now = datetime.datetime.now(datetime.timezone.utc)
        if (now - pub_dt) > datetime.timedelta(hours=24):
            logger.info(f"Skipping article '{art_url}' - older than 24 hours.")
            continue

        title, content, image_url = parse_article_html(art_html)
        if not (title or content):
            continue

        detected_team = detect_team_from_text(title, content, team_tag, allow_fallback=allow_fallback)
        if not detected_team:
            logger.info(f"Article '{art_url}' does not match club '{team_tag}'; skipping.")
            continue

        database.save_article(source_id, art_url, title, content, image_url, detected_team)


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
            # Only auto-discover a site-wide RSS feed when the user gave the site
            # ROOT. If they gave a section/team/journalist path (e.g. /arsenal), keep
            # it as a web_link so our club-specific feed extraction runs, instead of
            # silently replacing it with a global feed that mixes all clubs.
            is_root = parsed.path.rstrip('/') in ('',) and not parsed.query

            if is_root:
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

                return 'web_link', url, "Accessible site root with no RSS feed. Registered as regular Web Link."

            return 'web_link', url, ("Section/author/team page detected. Registered as a Web Link "
                                     "so individual articles are extracted (not replaced by a site-wide feed).")
            
        else:
            return 'web_link', url, f"Server responded with status code {res.status_code}. Registered as regular Web Link."
            
    except Exception as e:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        return 'web_link', url, f"Connection failed ({e}). Registered as Web Link (DrissionPage fallback)."


def fetch_google_news(team_query: str) -> list[dict]:
    """Searches Google News via its RSS feed for the query. Returns real RSS entries only (no synthetic data)."""
    encoded_query = urllib.parse.quote_plus(team_query)
    feed_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    
    logger.info(f"Fetching Google News RSS feed for query: '{team_query}'")
    try:
        feed = feedparser.parse(feed_url)
        results = []
        for entry in feed.entries[:3]:  # Strict: only the 3 most recent entries per check
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
        "2. CONCISE BUT COMPREHENSIVE STRUCTURE\n"
        "- For each identified talking point, deliver exactly one response block. Do not include multiple options, headings, intro text, or conversational filler.\n"
        "- Write a concise yet COMPREHENSIVE update. Include every key contextual detail present in the source: names, clubs, specific monetary figures, fees, wages, contract lengths and terms, dates, timelines, the outlet or source of the report, and all parties involved. Never omit important context for the sake of brevity.\n"
        "- Use as many sentences as the detail genuinely requires. When the source contains rich detail, a direct quote, or lengthy information, preserve that fullness rather than compressing it away. Prioritise completeness and clarity over shortness; only be brief when the source itself is brief.\n\n"
        "3. HANDLING DIRECT QUOTES (MANDATORY)\n"
        "- CRITICAL: If the source text contains ANY direct quotation (any text enclosed in quotation marks, whether double \" \" or single ' '), you MUST reproduce that quote VERBATIM. You are strictly forbidden from paraphrasing, shortening, or rewording a direct quote. Preserving the speaker's exact words is the single most important rule.\n"
        "- EXTRACT EVERY QUOTE (NO EXCEPTIONS): You must find and reproduce EVERY distinct direct quote in the source, not only the first or the most prominent one. If the article contains multiple direct quotes, output a SEPARATE talking point (---TALKING_POINT---) for EACH quote, each ending with that quote reproduced verbatim. You are strictly forbidden from reproducing one quote while summarizing, paraphrasing, or omitting another. If there are three direct quotes in the source, your output MUST contain three verbatim quotes. Never choose to summarize a passage that is actually a direct quotation.\n"
        "- When a talking point contains a direct quote from a manager, player, or official, first write a single, highly concise, and coherent plain sentence that summarizes what the quote is talking about.\n"
        "- Do not break up this summary intro with periods or unnecessary punctuation. Keep it short, fluid, and straight to the point.\n"
        "- End this single summary sentence with a colon (:).\n"
        "- Directly after the colon, insert the EXACT, word-for-word quote from the source text (copied character for character, inside quotation marks). Never replace a quote with your own summary of it.\n\n"
        "4. GRAMMAR, TONE, AND VOICE\n"
        "- Write with a clear, informative, and authoritative tone.\n"
        "- Use the active voice exclusively; do not use passive voice constructions.\n"
        "- Address the core subject directly without setup phrases, introductory filler, or generic commentary.\n"
        "- Keep every sentence clear and coherent. Be efficient and avoid padding or repetition, but do NOT sacrifice important detail, context, or quoted material in order to be short.\n\n"
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

    # In strict mode (author/section/Google sources) only look at the title + lead, not
    # the whole page. A real article's subject is in its headline/opening; an incidental
    # club mention in a "related stories" sidebar deep in the page must not qualify an
    # off-topic (e.g. tennis) article. Permissive mode (trusted single-club RSS/X) keeps
    # scanning the full text.
    if allow_fallback:
        text = f"{title or ''}\n{content or ''}".lower()
    else:
        text = f"{title or ''}\n{(content or '')[:800]}".lower()
    
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


def run_scraper_ingestion(x_scraper=None, include_regular=True,
                          include_protected=True, include_google=True):
    """Loops through sources and Google News feeds, fetches new articles, saves to SQLite.

    The work is split so callers can run the FAST sources (RSS/web/X + Google News) on a
    short cycle and the SLOW Cloudflare/headless-browser sources on a longer cycle:
      - include_regular:   RSS, plain web links, X accounts, TransferFeed
      - include_protected: Cloudflare/JS sources scraped with DrissionPage (the slow part)
      - include_google:    Google News feeds
    """
    logger.info(f"Starting Scraper Ingestion (regular={include_regular}, "
                f"protected={include_protected}, google={include_google})...")
    
    # 1. Fetch User-Configured Sources from DB
    sources = database.get_sources()
    
    # Cache sources by (type, value, team_tag) to optimize DB access
    global _sources_cache
    _sources_cache = {(s['type'], s['value'], s['team_tag']): s['id'] for s in sources}
    
    # Only needed for x_account sources (regular loop); the slow/protected loop skips it.
    x_client = x_scraper if x_scraper is not None else (XScraper() if include_regular else None)

    # Separate standard sources from Cloudflare-protected web link sources
    regular_sources = []
    protected_web_sources = []
    
    for src in sources:
        if src['type'] == 'web_link' and not 'transferfeed.com' in src['value']:
            from urllib.parse import urlparse
            parsed_url = urlparse(src['value'])
            domain = parsed_url.netloc.lower()
            if _needs_browser(domain):
                protected_web_sources.append(src)
                continue
        regular_sources.append(src)
        
    # Process Regular Sources
    for src in (regular_sources if include_regular else []):
        source_id = src['id']
        source_type = src['type']
        value = src['value']
        team_tag = src['team_tag']

        # Data optimisation: proxy-routed domains are metered (residential proxy), so don't
        # re-fetch them on every fast cycle — throttle to PROXY_MIN_INTERVAL_SECONDS.
        if _domain_uses_proxy(value):
            import time as _t
            if _t.time() - _proxy_last_fetch.get(value, 0) < getattr(config, 'PROXY_MIN_INTERVAL_SECONDS', 1200):
                logger.info(f"Throttling proxy source (recently fetched): {value}")
                continue
            _proxy_last_fetch[value] = _t.time()

        logger.info(f"Scraping source: {source_type} - {value} ({team_tag})")
        
        if source_type == 'rss':
            try:
                # Proxy-listed feeds (e.g. mirror.co.uk RSS) are blocked for the datacenter
                # IP, so fetch the feed XML through the residential proxy, then parse it.
                if _domain_uses_proxy(value):
                    feed_xml = fetch_via_proxy(value)
                    feed = feedparser.parse(feed_xml) if feed_xml else feedparser.parse(value)
                else:
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
                        # Process top 5 players. For each, take ONLY the most recent update
                        # (not the player's whole history). The unique id embeds a hash of
                        # that latest update, so a genuinely newer update posts once while
                        # the same update is never re-posted.
                        import hashlib
                        for title_text, rumour_url in items[:5]:
                            web_title, latest_update, web_image = scrape_transferfeed_latest(rumour_url)
                            content = latest_update if latest_update else title_text
                            if not content:
                                continue
                            sig = hashlib.md5(content.strip().lower().encode('utf-8')).hexdigest()[:10]
                            uid = f"{rumour_url}#{sig}"
                            if database.article_exists(uid):
                                continue
                            logger.info(f"New TransferFeed update: {rumour_url}")
                            title = web_title if web_title else title_text
                            detected_team = detect_team_from_text(title, content, team_tag, allow_fallback=False)
                            if not detected_team:
                                logger.info(f"TransferFeed rumour '{title}' does not match any target clubs. Skipping Ingestion.")
                                continue
                            database.save_article(source_id, uid, title, content, web_image, detected_team)
                except Exception as e:
                    logger.error(f"Error processing TransferFeed Hub {value}: {e}")
            else:
                # Treat a generic (non-Cloudflare) web_link as a feed/author/team page:
                # extract its individual article links and ingest each one. Only if no
                # article feed is detected do we fall back to scraping the URL itself as a
                # single article (preserving support for direct single-article links).
                listing_html = _fetch_html(value)
                article_urls = extract_articles_from_author_page(value, listing_html) if listing_html else []

                if article_urls:
                    logger.info(f"Found {len(article_urls)} feed articles on {value}")
                    _ingest_feed_article_urls(article_urls, source_id, team_tag, allow_fallback=False)
                elif not database.article_exists(value):
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
    if include_protected and protected_web_sources:
        logger.info(f"Starting DrissionPage batch scraping for {len(protected_web_sources)} protected sources...")
        _patch_drission_websocket()
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
                                # allow_fallback=False: author/section pages carry off-club and
                                # off-topic items (e.g. a tennis story on a football author page),
                                # so only keep articles whose text actually matches the club.
                                detected_team = detect_team_from_text(title, content, team_tag, allow_fallback=False)
                                if not detected_team:
                                    logger.info(f"Protected article '{art_url}' does not match club '{team_tag}'; skipping.")
                                    continue
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
    } if include_google else {}

    for team, query in google_queries.items():
        articles = fetch_google_news(query)
        # Titles that mark aggregation / live-blog / multi-rumour entries. Google News
        # links cannot be resolved to the real article (they stay on news.google.com), so
        # we work from the RSS title + snippet. These aggregation entries are exactly the
        # ones that carry several (often stale) rumours and get split into multiple wrong
        # posts, so we reject them under the strict filter.
        _AGGREGATION_MARKERS = ('live:', 'live blog', 'as it happened', 'round-up', 'roundup',
                                'rumours:', 'rumors:', 'latest:', 'transfer news live',
                                'every ', 'all the', 'wrap:', 'gossip')
        for a in articles:
            article_url = a['url']
            if database.article_exists(article_url):
                continue

            title = a['title'] or ''
            content = a['content'] or ''
            image = a['media_url']

            # STRICT: reject aggregation/live entries.
            if any(m in title.lower() for m in _AGGREGATION_MARKERS):
                logger.info(f"Google News (strict): rejecting aggregation entry '{title[:60]}'.")
                continue

            # STRICT: content must be football-related and free of paywall/consent junk.
            cl = content.lower()
            garbage_keywords = ['accept cookies', 'cookie policy', 'privacy policy', 'subscribe to read',
                                'premium content', 'sign in', 'log in', 'enable javascript']
            football_terms = [team.lower(), 'football', 'transfer', 'signing', 'contract', 'deal', 'move']
            if any(k in cl for k in garbage_keywords) or not any(t in (title.lower() + ' ' + cl) for t in football_terms):
                logger.info(f"Google News (strict): '{title[:60]}' failed football/validity check.")
                continue

            # Find or create the virtual system source for database schema integrity
            system_source_value = f"system_google_news_{team.lower()}"
            cache_key = ('rss', system_source_value, team)
            system_source_id = _sources_cache.get(cache_key)

            if not system_source_id:
                database.add_source('rss', system_source_value, team)
                updated_sources = database.get_sources()
                _sources_cache = {(s['type'], s['value'], s['team_tag']): s['id'] for s in updated_sources}
                system_source_id = _sources_cache.get(cache_key)

            # Strict relevance: club must be the subject (title + lead), not a passing mention.
            detected_team = detect_team_from_text(title, content, team, allow_fallback=False)
            if not detected_team:
                logger.info(f"Google News article '{title}' does not match any target clubs. Skipping Ingestion.")
                continue
            database.save_article(system_source_id, article_url, title, content, image, detected_team)
                
    logger.info("Scraper Ingestion Cycle Completed.")




# ---------------------------------------------------------------------------
# Read-only source diagnostics (used by the Telegram "Test Source" feature).
# These functions reuse the SAME helpers as run_scraper_ingestion, so their
# output reflects exactly what production would do. They write NOTHING to the
# database or Telegram.
# ---------------------------------------------------------------------------

def _open_chromium():
    """Opens a headless Chromium page for testing protected domains. Returns (page, error)."""
    try:
        _patch_drission_websocket()
        from DrissionPage import ChromiumPage, ChromiumOptions
        co = ChromiumOptions()
        co.headless(True)
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-gpu')
        co.set_user_agent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        return ChromiumPage(co), None
    except Exception as e:
        return None, str(e)


def _chromium_get(page, url, wait=5):
    """Loads a URL in an existing Chromium page and returns its HTML (or None)."""
    try:
        page.get(url)
        time.sleep(wait)
        return page.html
    except Exception as e:
        logger.error(f"DrissionPage fetch failed for {url}: {e}")
        return None


def _which_feed_container(html_text, domain):
    """Reports which feed container selector matched, for the diagnostic output."""
    soup = BeautifulSoup(html_text, 'html.parser')
    _decompose_noise(soup)
    selectors = []
    is_known = False
    for known_domain, sels in _FEED_CONTAINER_SELECTORS.items():
        if known_domain in domain:
            selectors = sels
            is_known = True
            break
    for sel in selectors:
        if soup.select(sel):
            return f"matched selector '{sel}'"
    if soup.select('main, [role="main"]'):
        return "matched generic <main>"
    if is_known and any(d in domain for d in ('thesun.co.uk',)):
        return ("NO container matched -> extracting nothing to avoid whole-site "
                "scraping (selector tuning needed)")
    return "no specific container; scanning de-noised document (filtered by URL rule)"


def diagnose_source(url: str, team_tag: str | None = None, deep_limit: int = 3) -> str:
    """Read-only dry run of the ingestion pipeline for a single URL.
    Returns a plain-text English report. Writes nothing to the DB or Telegram."""
    import datetime
    from urllib.parse import urlparse

    out = []
    url = (url or '').strip()
    out.append(f"Source test:\n{url}\n")
    if not url.startswith('http'):
        out.append("Not a valid http(s) URL.")
        return "\n".join(out)

    domain = urlparse(url).netloc.lower()

    try:
        stype, resolved_url, _desc = auto_detect_source_classification(url)
    except Exception as e:
        stype, resolved_url = 'web_link', url
        out.append(f"(classification error: {e})")
    out.append(f"Detected type: {stype.upper()}")
    if resolved_url != url:
        out.append(f"Resolved URL: {resolved_url}")

    now = datetime.datetime.now(datetime.timezone.utc)

    # --- RSS ---
    if stype == 'rss':
        try:
            feed = feedparser.parse(resolved_url)
            out.append(f"RSS entries found: {len(feed.entries)}")
            for entry in feed.entries[:5]:
                title = (entry.get('title', 'No Title') or '')[:70]
                pub = entry.get('published_parsed')
                if pub:
                    pub_dt = datetime.datetime(*pub[:6], tzinfo=datetime.timezone.utc)
                    age_h = (now - pub_dt).total_seconds() / 3600
                    status = "within 24h OK" if age_h <= 24 else f"{int(age_h // 24)}d old SKIP"
                else:
                    status = "no date SKIP"
                out.append(f"  - {title}  [{status}]")
            out.append("Verdict: OK" if feed.entries else "Verdict: no entries (not a usable RSS feed)")
        except Exception as e:
            out.append(f"RSS parse error: {e}")
        return "\n".join(out)

    # --- web_link / feed page ---
    is_protected = _needs_browser(domain)
    page = None
    try:
        if is_protected:
            page, err = _open_chromium()
            if not page:
                out.append(f"This is a Cloudflare-protected domain and headless Chromium failed: {err}")
                return "\n".join(out)
            fetch = lambda u: _chromium_get(page, u)
            out.append("Access method: headless Chromium (DrissionPage)")
        else:
            fetch = _fetch_html
            out.append("Access method: plain HTTP (requests)")

        listing_html = fetch(resolved_url)
        if not listing_html:
            out.append("Could not fetch the page (blocked or unreachable).")
            return "\n".join(out)

        out.append(f"Feed container: {_which_feed_container(listing_html, domain)}")

        article_urls = extract_articles_from_author_page(resolved_url, listing_html)
        out.append(f"Article links extracted: {len(article_urls)}")
        for u in article_urls:
            out.append(f"  - {u}")

        if not article_urls:
            out.append("\nNo article feed detected -> in production this URL would be scraped "
                        "as a SINGLE page. If this is a journalist/team feed page, the container "
                        "selector for this domain needs tuning.")
            return "\n".join(out)

        out.append(f"\nSample article checks (first {deep_limit}):")
        saved = skipped = 0
        for art_url in article_urls[:deep_limit]:
            art_html = fetch(art_url)
            if not art_html:
                out.append(f"  SKIP (fetch failed): {art_url}")
                skipped += 1
                continue
            pub_dt = extract_article_published_date(art_html)
            title, content, _img = parse_article_html(art_html)
            disp = (title or '(no title)').strip()[:60]
            if not pub_dt:
                out.append(f"  SKIP {disp} -- no parseable date")
                skipped += 1
                continue
            age_h = (now - pub_dt).total_seconds() / 3600
            if age_h > 24:
                out.append(f"  SKIP {disp} -- {int(age_h // 24)}d old (24h rule)")
                skipped += 1
                continue
            if team_tag:
                detected = detect_team_from_text(title, content, team_tag, allow_fallback=False)
                if not detected:
                    out.append(f"  SKIP {disp} -- not about {team_tag}")
                    skipped += 1
                    continue
                out.append(f"  SAVE {disp} -- date ok, club={detected}")
            else:
                out.append(f"  SAVE {disp} -- date ok (no club filter)")
            saved += 1

        out.append(f"\nVerdict: {len(article_urls)} links; sample -> {saved} would save, {skipped} skipped.")
        if saved == 0:
            out.append("Nothing from the sample would be saved -- check dates/relevance.")
        return "\n".join(out)
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass
