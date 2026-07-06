import os
import logging
from dotenv import load_dotenv

# Setup basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("football_news_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("config")

# Load environment variables from .env file if it exists
load_dotenv()

# Mandatory Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ADMIN_USER_ID_RAW = os.getenv("ADMIN_USER_ID")

# Optional Topic/Thread IDs for Forums
THREAD_ID_ARSENAL = os.getenv("THREAD_ID_ARSENAL")
THREAD_ID_LIVERPOOL = os.getenv("THREAD_ID_LIVERPOOL")
THREAD_ID_INTER = os.getenv("THREAD_ID_INTER")
# "General" topic: X posts that aren't about any of the 3 clubs go here to keep the
# club tabs clean (e.g. Fabrizio Romano's non-Arsenal/Liverpool/Inter tweets).
THREAD_ID_GENERAL = os.getenv("THREAD_ID_GENERAL")

# Optional X/Twitter Credentials for Twikit
X_USERNAME = os.getenv("X_USERNAME")
X_PASSWORD = os.getenv("X_PASSWORD")
X_EMAIL = os.getenv("X_EMAIL")

# Optional Proxy Configuration for Twikit (HTTP/SOCKS5)
PROXY_URL = os.getenv("PROXY_URL")

# Residential scraping proxy for sites that block the server's datacenter IP (Reach plc /
# CloudFront / Akamai). ONLY the domains in PROXY_DOMAINS are routed through it, and only
# via HTTP (curl_cffi with a browser TLS fingerprint the WAFs accept) — not a browser — so
# proxy data stays minimal. Set SCRAPER_PROXY_URL in .env to enable.
SCRAPER_PROXY_URL = os.getenv("SCRAPER_PROXY_URL")
PROXY_IMPERSONATE = os.getenv("PROXY_IMPERSONATE", "chrome131")
PROXY_DOMAINS = [
    d.strip().lower() for d in os.getenv(
        "PROXY_DOMAINS",
        "mirror.co.uk,liverpoolecho.co.uk,football.london,givemesport.com,thesun.co.uk"
    ).split(",") if d.strip()
]
# Minimum seconds between proxy fetches of the same source (throttles metered proxy data).
PROXY_MIN_INTERVAL_SECONDS = int(os.getenv("PROXY_MIN_INTERVAL_SECONDS", "1200"))

# Include retweets from monitored X accounts. Many curator/aggregator accounts (e.g. the
# Arabic football accounts) mostly retweet rather than post originals, so with this off
# they look empty. When on, the ORIGINAL tweet's full text is posted and de-duplicated by
# the original tweet id (so the same post retweeted by several accounts appears once).
X_INCLUDE_RETWEETS = os.getenv("X_INCLUDE_RETWEETS", "true").lower() in ("1", "true", "yes", "on")

# TikTok Monitor module configuration
# Where TikTok videos are posted. Defaults to the main chat; set TIKTOK_THREAD_ID to a
# dedicated "TikTok" forum topic in the same group.
TIKTOK_CHAT_ID = os.getenv("TIKTOK_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
TIKTOK_THREAD_ID = os.getenv("TIKTOK_THREAD_ID")

# Parse Admin User ID(s)
ADMIN_USER_IDS = []
ADMIN_USER_ID = None
if ADMIN_USER_ID_RAW:
    for part in ADMIN_USER_ID_RAW.split(","):
        try:
            if part.strip():
                ADMIN_USER_IDS.append(int(part.strip()))
        except ValueError:
            logger.error(f"Admin User ID part '{part}' must be a valid integer.")
    if ADMIN_USER_IDS:
        ADMIN_USER_ID = ADMIN_USER_IDS[0]

# Parse Thread IDs to integers if provided
def _parse_thread_id(val):
    if val:
        try:
            return int(val)
        except ValueError:
            logger.warning(f"Thread ID value '{val}' is not a valid integer. Treating as None.")
    return None

THREAD_ID_ARSENAL = _parse_thread_id(THREAD_ID_ARSENAL)
THREAD_ID_LIVERPOOL = _parse_thread_id(THREAD_ID_LIVERPOOL)
THREAD_ID_INTER = _parse_thread_id(THREAD_ID_INTER)
THREAD_ID_GENERAL = _parse_thread_id(THREAD_ID_GENERAL)
TIKTOK_THREAD_ID = _parse_thread_id(TIKTOK_THREAD_ID)

# Validation check
missing_vars = []
if not TELEGRAM_BOT_TOKEN:
    missing_vars.append("TELEGRAM_BOT_TOKEN")
if not GEMINI_API_KEY:
    missing_vars.append("GEMINI_API_KEY")
if not TELEGRAM_CHAT_ID:
    missing_vars.append("TELEGRAM_CHAT_ID")
if not ADMIN_USER_ID:
    missing_vars.append("ADMIN_USER_ID")

if missing_vars:
    logger.error(
        f"Critical Configuration Error: Missing environment variables: {', '.join(missing_vars)}. "
        "Please create a .env file or export these variables."
    )
else:
    logger.info("Configuration loaded successfully.")

# Scheduler & Ingestion Parameters
# Fast loop: RSS + plain web + X + Google News (latency-sensitive). Slow loop: the
# Cloudflare/headless-browser sources, which are heavy and less time-critical. Splitting
# them keeps breaking news fast instead of waiting behind the ~18-min DrissionPage batch.
FAST_CYCLE_SECONDS = 180       # Sleep between fast (RSS/web/X/Google) cycles
PROTECTED_CYCLE_SECONDS = 1200 # Sleep between slow (Cloudflare/DrissionPage) cycles
SCHEDULER_CYCLE_SECONDS = FAST_CYCLE_SECONDS  # backward-compat alias
MAX_BATCH_SIZE = 30            # Max pending articles to process in one cycle
MAX_BACKLOG = 15               # Max backlog articles to process on startup
DB_RETENTION_DAYS = 7          # DB retention policy period

# TikTok Monitor parameters
TIKTOK_CYCLE_SECONDS = 600     # Polling interval for the TikTok monitor loop
TIKTOK_FETCH_LIMIT = 5         # How many latest videos to check per creator per cycle
TIKTOK_MAX_VIDEO_MB = 49       # Max size to upload natively (Telegram bot limit is 50MB)
TIKTOK_SEEN_RETENTION_DAYS = 30  # Prune tiktok_seen_videos older than this
