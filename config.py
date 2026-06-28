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

# Optional X/Twitter Credentials for Twikit
X_USERNAME = os.getenv("X_USERNAME")
X_PASSWORD = os.getenv("X_PASSWORD")
X_EMAIL = os.getenv("X_EMAIL")

# Optional Proxy Configuration for Twikit (HTTP/SOCKS5)
PROXY_URL = os.getenv("PROXY_URL")

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
SCHEDULER_CYCLE_SECONDS = 600  # 10 minutes loop
MAX_BATCH_SIZE = 30            # Max pending articles to process in one cycle
MAX_BACKLOG = 15               # Max backlog articles to process on startup
DB_RETENTION_DAYS = 7          # DB retention policy period
