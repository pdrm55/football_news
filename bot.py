import os
import time
import logging
import re
import threading
import html
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, CopyTextButton,
)

import config
import database
import scraper
import tiktok_monitor
import translator

# Initialize logging
logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)

# Check configuration on startup
if not config.TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not found in environment. Exiting.")
    exit(1)

# Initialize bot client
bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)

# Helpers for Gates
def is_admin(user_id: int) -> bool:
    return user_id in getattr(config, 'ADMIN_USER_IDS', [config.ADMIN_USER_ID])

def clean_text_formatting(text: str) -> str:
    """Removes bolding, asterisks, emojis, hashtags, semicolons, and em dashes."""
    if not text:
        return ""
    
    # 1. Remove bold/italic markdown characters (*, **, _, __, `)
    text = text.replace('**', '').replace('*', '').replace('__', '').replace('_', '').replace('`', '')
    
    # 2. Remove em dashes (— and --) and replace with commas
    text = text.replace('—', ', ').replace('--', ', ')
    
    # 3. Remove semicolons (;)
    text = text.replace(';', ', ')
    
    # 4. Remove hashtags (#)
    text = text.replace('#', '')
    
    # 5. Remove emojis
    emoji_pattern = re.compile(
        '['
        '\U0001f600-\U0001f64f'  # emoticons
        '\U0001f300-\U0001f5ff'  # symbols & pictographs
        '\U0001f680-\U0001f6ff'  # transport & map symbols
        '\U0001f1e0-\U0001f1ff'  # flags (iOS)
        '\U00002700-\U000027bf'  # dingbats
        '\U00002600-\U000026ff'  # miscellaneous symbols
        '\U0001f900-\U0001f9ff'  # supplemental symbols
        '\U0001fa70-\U0001faff'  # symbols and pictographs extended
        ']+', flags=re.UNICODE
    )
    text = emoji_pattern.sub('', text)
    
    # Clean up spaces around punctuation
    text = re.sub(r' +', ' ', text)
    text = text.replace(' ,', ',').replace(' .', '.')
    
    return text.strip()

def get_thread_id(team_tag: str) -> int | None:
    """Resolves the thread ID for a specific team."""
    if team_tag == 'Arsenal':
        return config.THREAD_ID_ARSENAL
    elif team_tag == 'Liverpool':
        return config.THREAD_ID_LIVERPOOL
    elif team_tag == 'Inter':
        return config.THREAD_ID_INTER
    elif team_tag == 'General':
        return config.THREAD_ID_GENERAL
    return None

# Copy text: the clean, full post text used for copying (translation flag stripped, since
# the reader copies this to repost on X and shouldn't paste our translation notice).
def copy_body(summary: str) -> str:
    text = (summary or "").strip()
    if text.startswith(translator.TRANSLATION_FLAG):
        parts = text.split("\n", 1)
        text = parts[1].strip() if len(parts) > 1 else ""
    return text

# Message Formatting
def format_broadcast(summary: str, url: str) -> str:
    """Formats the news post (HTML): the readable text, the source URL, and a monospace
    copy-block holding the FULL text. The <pre> block has Telegram's native one-tap copy,
    which — unlike the copy_text button — has no 256-char limit, so nothing is left hanging."""
    # Escape only &, <, > for HTML mode (quote=False keeps apostrophes/quotes as-is, so a
    # quote like Gunners' is not turned into Gunners&#x27;). Also decode any HTML entities
    # already present in the scraped text so nothing shows raw.
    body = html.escape(html.unescape(summary or ""), quote=False)
    lines = ["BREAKING UPDATE", "====================", body, "===================="]
    if url and (url.startswith('http://') or url.startswith('https://')):
        lines.append(f"Source URL: {html.escape(url, quote=False)}")
    msg = "\n".join(lines)

    full = html.unescape(copy_body(summary))
    if full:
        msg += "\n\n📋 Copy:\n<pre>" + html.escape(full, quote=False) + "</pre>"
    return msg

# Copy-to-clipboard button (kept for short posts as a quick one-tap; long posts are
# fully covered by the copy-block in the message above, which has no length limit).
def build_copy_markup(summary: str) -> InlineKeyboardMarkup | None:
    """One-tap '📋 Copy Text' button (Telegram copy_text). It is capped at 256 chars by
    Telegram, so it's only attached when the whole text fits — otherwise the message's
    copy-block is the full-text path and we don't show a button that would truncate."""
    text = copy_body(summary)
    if not text or len(text) > 256:
        return None
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📋 Copy Text", copy_text=CopyTextButton(text=text)))
    return markup

# Broadcasting Layer
def send_telegram_broadcast(summary: str, url: str, media_url: str | None, thread_id: int | None, art_id: int, team_tag: str | None) -> bool:
    """Delivers the post to Telegram as plain text. If thread_id fails (e.g. topic not found),
    it automatically falls back to sending directly to the main chat.
    """
    formatted_msg = format_broadcast(summary, url)
    copy_markup = build_copy_markup(summary)

    # Try photo first if media_url is provided
    if media_url:
        try:
            bot.send_photo(
                chat_id=config.TELEGRAM_CHAT_ID,
                photo=media_url,
                caption=formatted_msg,
                message_thread_id=thread_id,
                reply_markup=copy_markup, parse_mode='HTML'
            )
            logger.info(f"Successfully sent photo for article {art_id} to {team_tag} (thread: {thread_id})")
            return True
        except Exception as e:
            logger.warning(f"Failed to send photo with thread_id {thread_id} for article {art_id}: {e}")
            
            # If the specific thread failed, try without thread (main chat)
            if thread_id is not None and "thread not found" in str(e).lower():
                try:
                    logger.info(f"Retrying photo article {art_id} without thread_id (main chat fallback)...")
                    bot.send_photo(
                        chat_id=config.TELEGRAM_CHAT_ID,
                        photo=media_url,
                        caption=formatted_msg,
                        message_thread_id=None,
                        reply_markup=copy_markup, parse_mode='HTML'
                    )
                    logger.info(f"Successfully sent photo for article {art_id} to main chat.")
                    return True
                except Exception as e_fallback:
                    logger.error(f"Fallback photo send to main chat failed: {e_fallback}")
            
            # If it failed due to other issues (e.g. photo URL expired), fall back to text message
            logger.info(f"Falling back to text broadcast for article {art_id}...")

    # Text message fallback/default
    try:
        bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=formatted_msg,
            message_thread_id=thread_id,
            reply_markup=copy_markup, parse_mode='HTML'
        )
        logger.info(f"Successfully sent text for article {art_id} to {team_tag} (thread: {thread_id})")
        return True
    except Exception as e:
        logger.warning(f"Failed to send text with thread_id {thread_id} for article {art_id}: {e}")
        
        # If the specific thread failed, try without thread (main chat)
        if thread_id is not None and "thread not found" in str(e).lower():
            try:
                logger.info(f"Retrying text article {art_id} without thread_id (main chat fallback)...")
                bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=formatted_msg,
                    message_thread_id=None,
                    reply_markup=copy_markup, parse_mode='HTML'
                )
                logger.info(f"Successfully sent text for article {art_id} to main chat.")
                return True
            except Exception as e_fallback:
                logger.error(f"Fallback text send to main chat failed: {e_fallback}")
                
    return False


def broadcast_processed_articles():
    """Fetches articles with status 'processed' and posts them to the group topics.
    Includes anti-flood protection for large backlogs.
    """
    articles = database.get_processed_articles()
    if not articles:
        logger.info("No processed articles to broadcast.")
        return

    logger.info(f"Found {len(articles)} processed articles in database.")
    
    # Anti-flood protection
    if len(articles) > config.MAX_BACKLOG:
        logger.warning(f"Backlog of {len(articles)} articles exceeds threshold. Broadcasting only the {config.MAX_BACKLOG} most recent, marking the rest as sent.")
        to_mark_sent = articles[:-config.MAX_BACKLOG]
        to_broadcast = articles[-config.MAX_BACKLOG:]
        
        for art in to_mark_sent:
            database.update_article_status(art['id'], 'sent')
            logger.info(f"Marked backlog article {art['id']} as sent without broadcasting (anti-flood).")
    else:
        to_broadcast = articles

    logger.info(f"Broadcasting {len(to_broadcast)} processed articles...")
    for art in to_broadcast:
        art_id = art['id']
        team_tag = art['team_tag']
        summary = art['ai_summary']
        url = art['unique_identifier']
        media_url = art['media_url']
        
        thread_id = get_thread_id(team_tag)
        sent_successfully = send_telegram_broadcast(summary, url, media_url, thread_id, art_id, team_tag)

        if sent_successfully:
            database.update_article_status(art_id, 'sent')
            
        time.sleep(2)


def is_skip_text(text: str) -> bool:
    """Helper to detect if Gemini returned the word 'SKIP' as a complete word."""
    return bool(re.search(r'\bSKIP\b', text.upper()))


def process_and_broadcast_pipeline():
    """Fetches pending articles, processes them through filters/Gemini,
    and immediately broadcasts them to Telegram one-by-one.
    """
    
    # 1. Clear any already processed backlog first
    broadcast_processed_articles()
    
    # 2. Get pending articles
    pending_articles = database.get_pending_articles()
    if not pending_articles:
        logger.info("No pending articles to process.")
        return
        
    # Limit processing batch size per cycle to prevent infinite blocks
    if len(pending_articles) > config.MAX_BATCH_SIZE:
        logger.info(f"Batch size {len(pending_articles)} exceeds limit. Processing only first {config.MAX_BATCH_SIZE} items.")
        pending_articles = pending_articles[:config.MAX_BATCH_SIZE]
        
    active_filters = [f['keyword'] for f in database.get_filters()]
    
    logger.info(f"Processing and broadcasting {len(pending_articles)} pending articles...")
    for art in pending_articles:
        title = art['original_title'] or ""
        content = art['original_content'] or ""
        art_id = art['id']
        team_tag = art['team_tag']
        media_url = art['media_url']
        url = art['unique_identifier']
        source_type = art.get('source_type')

        # 1. Skip Gemini for sources that are ALREADY concise, self-contained updates:
        #    - X/Twitter posts, and
        #    - TransferFeed cards (each card is a complete, well-written transfer update;
        #      re-summarizing it just compresses away the context — figures, contract
        #      terms, sources — so we post the full card text as-is).
        _headline_only = any(d in (url or '').lower() for d in getattr(config, 'HEADLINE_ONLY_DOMAINS', []))
        if source_type == 'x_account' or 'transferfeed.com' in (url or '') or _headline_only:
            logger.info(f"Article {art_id} is a concise/headline source. Bypassing Gemini, using full text.")
            summary = content
        else:
            # 2. Summarize web page or RSS articles using Gemini
            logger.info(f"Summarizing article {art_id}: {title[:50]}...")
            summary = scraper.run_gemini_summarizer(title, content, active_filters)
        
        if summary is None:
            # API failure, keep it pending to try again later
            continue
            
        if is_skip_text(summary):
            logger.info(f"Article {art_id} skipped due to keyword filters or AI decision.")
            database.update_article_summary_status(art_id, 'SKIP', 'skipped')
            continue
            
        # 3. Handle multiple updates split by delimiter ---TALKING_POINT---
        chunks = [c.strip() for c in summary.split('---TALKING_POINT---') if c.strip()]
        valid_chunks = [c for c in chunks if not is_skip_text(c)]
        
        if not valid_chunks:
            logger.info(f"Article {art_id} skipped: no valid non-SKIP chunks found.")
            database.update_article_summary_status(art_id, 'SKIP', 'skipped')
            continue
            
        logger.info(f"Processing {len(valid_chunks)} distinct updates from article {art_id}...")
        
        for idx, chunk in enumerate(valid_chunks):
            # Enforce clean spartan layout (strip bolding, emojis, hashtags, etc.)
            clean_chunk = clean_text_formatting(chunk)
            if not clean_chunk:
                continue

            # X posts are raw (no Gemini): auto-translate non-English tweets to English
            # via Google Translate, with a visible flag at the top of the post.
            if source_type == 'x_account':
                clean_chunk = translator.translate_for_broadcast(clean_chunk)
                # Relevance-based routing: an account (e.g. Fabrizio Romano) may mix club
                # news with lots of unrelated tweets. Keep the club tab only when the tweet
                # is actually about that club; otherwise route it to the General tab so the
                # Arsenal/Liverpool/Inter tabs stay noise-free. Detection runs on the
                # translated text, so Arabic club mentions are matched too.
                route_team = scraper.detect_team_from_text(title, clean_chunk, team_tag, allow_fallback=False)
                thread_id = get_thread_id(route_team) if route_team else get_thread_id('General')
            else:
                thread_id = get_thread_id(team_tag)
            sent_successfully = send_telegram_broadcast(clean_chunk, url, media_url, thread_id, art_id, team_tag)
            
            if idx == 0:
                # Update the original article in DB
                database.update_article_summary_status(art_id, clean_chunk, 'processed')
                if sent_successfully:
                    database.update_article_status(art_id, 'sent')
            else:
                # Insert as a new article row in DB
                chunk_uid = f"{url}#tp_{idx}"
                if not database.article_exists(chunk_uid):
                    new_art_id = database.save_article(
                        source_id=art['source_id'],
                        unique_identifier=chunk_uid,
                        original_title=title,
                        original_content=content,
                        media_url=media_url,
                        team_tag=team_tag
                    )
                    if new_art_id:
                        database.update_article_summary_status(new_art_id, clean_chunk, 'processed')
                        if sent_successfully:
                            database.update_article_status(new_art_id, 'sent')
            
            # Rate limit safety sleep between consecutive messages
            time.sleep(2)

cookie_alert_sent = False

# Background Scheduler Thread
def scheduler_loop():
    global cookie_alert_sent
    logger.info("Starting background scheduler loop...")

    while True:
        try:
            logger.info("Running scheduled cycle...")

            # Build a fresh XScraper each cycle so the session is re-verified and
            # mock_mode reflects the *current* cookie state (not the startup state).
            # A single instance is reused within the cycle (cookie check + ingestion).
            x_scraper = scraper.XScraper()

            # Check X cookies status and alert admin if expired
            if config.X_USERNAME:
                if x_scraper.mock_mode:
                    if not cookie_alert_sent:
                        logger.warning("X live session unavailable. Sending cookie alert to admin...")
                        try:
                            bot.send_message(
                                chat_id=config.ADMIN_USER_ID,
                                text=(
                                    "⚠️ *X (Twitter) Security Alert:*\n"
                                    "The X cookie session has expired or failed. Live crawling has stopped.\n"
                                    "Please register fresh cookies via the `Update X Cookies` button in the bot's `/settings` menu."
                                ),
                                parse_mode='Markdown'
                            )
                            cookie_alert_sent = True
                        except Exception as alert_err:
                            logger.error(f"Failed to send admin cookie alert: {alert_err}")
                else:
                    cookie_alert_sent = False

            # 1. Ingest FAST sources only (RSS + web + X + Google News). The heavy
            #    Cloudflare/DrissionPage sources run in a separate slow loop, so breaking
            #    news is no longer stuck behind the ~18-min headless-browser batch.
            scraper.run_scraper_ingestion(x_scraper=x_scraper, include_protected=False)
            # 2. Process and Broadcast everything pending (incl. items the slow loop added).
            process_and_broadcast_pipeline()
        except Exception as e:
            logger.error(f"Error in background scheduler: {e}")

        logger.info(f"Fast cycle completed. Sleeping for {config.FAST_CYCLE_SECONDS} seconds.")
        time.sleep(config.FAST_CYCLE_SECONDS)


# Slow scheduler for heavy Cloudflare / headless-browser sources
def protected_scheduler_loop():
    logger.info("Starting protected (Cloudflare/DrissionPage) scheduler loop...")
    while True:
        try:
            # Ingest ONLY the protected sources. It does not broadcast; the fast loop
            # picks up and posts whatever this adds within one fast cycle.
            scraper.run_scraper_ingestion(include_regular=False, include_protected=True,
                                          include_google=False)
            # Retention cleanup lives here so it doesn't run every few minutes.
            try:
                database.delete_old_articles(days=config.DB_RETENTION_DAYS)
            except Exception as prune_err:
                logger.error(f"Error pruning database: {prune_err}")
        except Exception as e:
            logger.error(f"Error in protected scheduler: {e}")

        logger.info(f"Protected cycle completed. Sleeping for {config.PROTECTED_CYCLE_SECONDS} seconds.")
        time.sleep(config.PROTECTED_CYCLE_SECONDS)

# Persistent button label that opens the settings panel (tap instead of typing).
SETTINGS_BUTTON_TEXT = "⚙️ Settings"

def get_settings_keyboard():
    """A persistent reply keyboard with a Settings button (shown to the admin)."""
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton(SETTINGS_BUTTON_TEXT))
    return markup

def menu_button_interrupt(message) -> bool:
    """Returns True (and reopens the menu) if the user tapped the persistent Settings
    button while a step-by-step flow was waiting for text input. This prevents the
    button label from being saved as data (e.g. as a bogus X handle or cookie)."""
    if (message.text or "").strip() == SETTINGS_BUTTON_TEXT:
        open_settings(message)
        return True
    return False

# Telegram Command Handlers
@bot.message_handler(commands=['start'])
def handle_start(message):
    reply_markup = None
    # Show the Settings button to the admin in private chat, so they can tap it
    # instead of typing /settings.
    if is_admin(message.from_user.id) and message.chat.type == "private":
        reply_markup = get_settings_keyboard()
    bot.reply_to(
        message,
        "⚽ *Football News Aggregator & Summarizer Bot*\n\n"
        "This bot runs background ingestion and broadcasts compiled news to "
        "the designated group topics. Admin configurations can be opened "
        "in private using `/settings` or the *Settings* button below.",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

def open_settings(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Access Denied: You are not authorized as the Administrator.")
        return

    if message.chat.type != "private":
        bot.reply_to(message, "⚠️ The settings panel is only available in private chat.")
        return

    send_main_menu(message.chat.id)

@bot.message_handler(commands=['settings'])
def handle_settings(message):
    open_settings(message)

@bot.message_handler(func=lambda m: m.text == SETTINGS_BUTTON_TEXT)
def handle_settings_button(message):
    open_settings(message)

def get_main_menu_markup():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📁 Sources Manager", callback_data="manage_sources"))
    markup.row(InlineKeyboardButton("🔍 Filter Keywords", callback_data="manage_filters"))
    markup.row(
        InlineKeyboardButton("⚡ Run Scraper Now", callback_data="run_scr_now"),
        InlineKeyboardButton("🔑 Update X Cookies", callback_data="update_x_cookies")
    )
    markup.row(
        InlineKeyboardButton("👤 Switch X Account", callback_data="switch_x_account")
    )
    markup.row(
        InlineKeyboardButton("🧪 Test a Source URL", callback_data="test_source")
    )
    markup.row(
        InlineKeyboardButton("🎵 TikTok Monitor", callback_data="tt_menu")
    )
    markup.row(
        InlineKeyboardButton("🔎 X Lead Finder", callback_data="lead_finder")
    )
    return markup


# Menus & Keyboards
def send_main_menu(chat_id):
    bot.send_message(
        chat_id,
        "🛠 <b>Football News Bot - Admin Settings</b>\n"
        "Manage ingestion feeds, keywords, or run tasks manually.",
        reply_markup=get_main_menu_markup(),
        parse_mode='HTML'
    )

def edit_to_main_menu(chat_id, message_id):
    bot.edit_message_text(
        "🛠 <b>Football News Bot - Admin Settings</b>\n"
        "Manage ingestion feeds, keywords, or run tasks manually.",
        chat_id,
        message_id,
        reply_markup=get_main_menu_markup(),
        parse_mode='HTML'
    )

# Callback Query Handler
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Unauthorized.", show_alert=True)
        return

    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data

    if data == "main_menu":
        edit_to_main_menu(chat_id, message_id)
        
    elif data == "manage_sources":
        show_sources_menu(chat_id, message_id)
        
    elif data == "manage_filters":
        show_filters_menu(chat_id, message_id)
        
    elif data == "run_scr_now":
        bot.answer_callback_query(call.id, "Scraper and pipeline running in background...")
        threading.Thread(target=run_manual_cycle, args=(chat_id,)).start()

    elif data == "lead_finder":
        show_lead_finder_menu(chat_id, message_id)

    elif data == "lead_run":
        if _lead_scan_running.is_set():
            bot.answer_callback_query(call.id, "A scan is already running — please wait for it to finish.", show_alert=True)
        else:
            _lead_scan_running.set()
            bot.answer_callback_query(call.id, "Lead scan started in background...")
            threading.Thread(target=_run_lead_scan, args=(chat_id,), daemon=True).start()

    elif data == "lead_custom":
        bot.answer_callback_query(call.id)
        prompt_lead_min(chat_id)
        
    elif data == "update_x_cookies":
        prompt_auth_token(chat_id)
        
    elif data == "switch_x_account":
        prompt_new_username(chat_id)

    elif data == "test_source":
        prompt_test_url(chat_id)

    elif data.startswith("test_run_"):
        team = data.replace("test_run_", "")
        url = pending_test_url.pop(chat_id, None)
        if not url:
            bot.answer_callback_query(call.id, "Session expired, please start again.")
            send_main_menu(chat_id)
        else:
            team_tag = None if team == "none" else team
            bot.answer_callback_query(call.id, "Testing source, please wait...")
            threading.Thread(target=run_source_test, args=(chat_id, url, team_tag)).start()

    elif data == "add_src_type":
        show_add_source_types(chat_id, message_id)
        
    elif data.startswith("add_src_t_"):
        # Selected source type
        stype = data.replace("add_src_t_", "")
        show_add_source_teams(chat_id, message_id, stype)
        
    elif data.startswith("add_src_p_"):
        # Format: add_src_p_{type}_{team}. The type can contain an underscore
        # (web_link, x_account) while the team never does, so split on the LAST
        # underscore. Using split() here broke web_link/x_account selection.
        stype, team = data.replace("add_src_p_", "").rsplit("_", 1)
        prompt_source_value(chat_id, stype, team)
        
    elif data == "del_src_list":
        show_remove_source_list(chat_id, message_id)
        
    elif data.startswith("del_src_do_"):
        sid = int(data.replace("del_src_do_", ""))
        database.remove_source(sid)
        bot.answer_callback_query(call.id, "Source removed.")
        show_remove_source_list(chat_id, message_id)
        
    elif data == "add_flt_prompt":
        prompt_filter_value(chat_id)
        
    elif data == "del_flt_list":
        show_remove_filter_list(chat_id, message_id)
        
    elif data.startswith("del_flt_do_"):
        fid = int(data.replace("del_flt_do_", ""))
        database.remove_filter(fid)
        bot.answer_callback_query(call.id, "Filter keyword removed.")
        show_remove_filter_list(chat_id, message_id)

    # --- TikTok Monitor ---
    elif data == "tt_menu":
        show_tiktok_menu(chat_id, message_id)

    elif data == "tt_add":
        prompt_tiktok_account(chat_id)

    elif data == "tt_test":
        prompt_tiktok_test(chat_id)

    elif data == "tt_del_list":
        show_remove_tiktok_list(chat_id, message_id)

    elif data.startswith("tt_del_do_"):
        aid = int(data.replace("tt_del_do_", ""))
        database.remove_tiktok_account(aid)
        bot.answer_callback_query(call.id, "TikTok account removed.")
        show_remove_tiktok_list(chat_id, message_id)

# Manual Execution Function
def run_manual_cycle(chat_id):
    try:
        scraper.run_scraper_ingestion()
        process_and_broadcast_pipeline()
        bot.send_message(chat_id, "✅ Scraper ingestion and AI pipeline run completed.")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Manual scraper run failed: {e}")

# X Lead Finder Flow
_lead_scan_running = threading.Event()  # prevents overlapping scans hammering the X session

def show_lead_finder_menu(chat_id, message_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("▶️ Run Scan (≥ 8,500)", callback_data="lead_run"))
    markup.row(InlineKeyboardButton("🔢 Custom Minimum…", callback_data="lead_custom"))
    markup.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu"))
    bot.edit_message_text(
        "🔎 <b>X Lead Finder</b>\n"
        "Scans X for football accounts above a follower threshold and sends you an "
        "Excel sheet (Name, Handle, Followers, Bio, Profile Link).\n\n"
        "• <b>Run Scan (≥ 8,500)</b> — quick scan with the default threshold.\n"
        "• <b>Custom Minimum…</b> — enter your own number (e.g. 20000).\n\n"
        "A scan takes a few minutes (human-like delays keep the account safe).",
        chat_id, message_id, reply_markup=markup, parse_mode='HTML'
    )

def prompt_lead_min(chat_id):
    msg = bot.send_message(
        chat_id,
        "🔢 <b>Custom follower filter</b>\n\n"
        "Send either:\n"
        "• a single <b>minimum</b> — e.g. <code>20000</code>, or\n"
        "• a <b>range</b> min–max — e.g. <code>7000-20000</code> or <code>7000 to 20000</code>.\n\n"
        "Or type /cancel to abort.",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(msg, save_lead_min)

def _parse_follower_filter(raw):
    """Parses '20000' -> (20000, None) or '7000-20000'/'7,000 to 20,000' -> (7000, 20000).
    Returns None if the input isn't a valid number or range."""
    import re
    s = re.sub(r'\b(to|through)\b', '-', raw.lower())
    s = s.replace('–', '-').replace('—', '-')
    s = re.sub(r'(>=|≥|>|\+|,|\s)', '', s)
    nums = [p for p in s.split('-') if p != '']
    if len(nums) == 1 and nums[0].isdigit() and int(nums[0]) > 0:
        return int(nums[0]), None
    if len(nums) == 2 and nums[0].isdigit() and nums[1].isdigit() and int(nums[0]) > 0 and int(nums[1]) > 0:
        lo, hi = sorted((int(nums[0]), int(nums[1])))
        return lo, hi
    return None

def save_lead_min(message):
    if menu_button_interrupt(message):
        return
    raw = (message.text or "").strip()
    if raw.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
    parsed = _parse_follower_filter(raw)
    if not parsed:
        bot.send_message(message.chat.id,
                         "❌ Please send a number like 20000, or a range like 7000-20000. Cancelled.")
        send_main_menu(message.chat.id)
        return
    min_followers, max_followers = parsed
    if _lead_scan_running.is_set():
        bot.send_message(message.chat.id, "A scan is already running — please wait for it to finish.")
        return
    _lead_scan_running.set()
    threading.Thread(target=_run_lead_scan,
                     args=(message.chat.id, min_followers, max_followers), daemon=True).start()

def _run_lead_scan(chat_id, min_followers=8500, max_followers=None):
    """Runs the X lead scraper in the background, shows a live progress bar, and delivers
    the Excel file. The admin can navigate away — the file is pushed when it's ready."""
    import tempfile, shutil, os, time as _time
    tmp = tempfile.mkdtemp(prefix="leads_")
    state = {"msg_id": None, "last_edit": 0.0}
    range_label = f"{min_followers:,}–{max_followers:,}" if max_followers else f"≥{min_followers:,}"
    try:
        m = bot.send_message(chat_id, "🔎 <b>Lead scan started…</b>\nYou can leave this screen — "
                                      "the Excel file will be sent here automatically.", parse_mode='HTML')
        state["msg_id"] = m.message_id

        def progress(qi, qtotal, query, scanned, qualified):
            # Throttle edits to respect Telegram's rate limits (~1/sec).
            now = _time.time()
            if now - state["last_edit"] < 3:
                return
            state["last_edit"] = now
            filled = int(10 * qi / max(qtotal, 1))
            bar = "▰" * filled + "▱" * (10 - filled)
            try:
                bot.edit_message_text(
                    f"🔎 <b>Scanning X for football leads…</b>\n"
                    f"{bar}  {qi}/{qtotal}\n"
                    f"Current: <code>{html.escape(query)}</code>\n"
                    f"Scanned: <b>{scanned}</b>   |   Qualified ({range_label}): <b>{qualified}</b>",
                    chat_id, state["msg_id"], parse_mode='HTML')
            except Exception:
                pass  # ignore "message not modified" / transient edit errors

        import x_lead_scraper
        path = os.path.join(tmp, "x_football_leads.xlsx")
        count = x_lead_scraper.scan_to_file(path, min_followers=min_followers,
                                            max_followers=max_followers, max_pages=2,
                                            progress_cb=progress)
        try:
            bot.edit_message_text(f"✅ <b>Scan complete</b> — {count} lead(s) found. Sending the Excel file…",
                                  chat_id, state["msg_id"], parse_mode='HTML')
        except Exception:
            pass
        if not count or not os.path.exists(path):
            bot.send_message(chat_id, "No accounts matched the criteria this time. Try again later.")
            return
        with open(path, "rb") as f:
            bot.send_document(
                chat_id, f, visible_file_name="x_football_leads.xlsx",
                caption=f"✅ {count} football X leads ({range_label} followers)."
            )
    except Exception as e:
        logger.error(f"Lead scan failed: {e}")
        bot.send_message(chat_id, f"⚠️ Lead scan failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        _lead_scan_running.clear()

# Test Source (read-only dry run) Flow
pending_test_url = {}

def prompt_test_url(chat_id):
    msg = bot.send_message(
        chat_id,
        "🧪 *Test a Source URL*\n\n"
        "Send the URL you want to test (RSS feed, web/journalist/team page).\n"
        "This is a read-only dry run, nothing is saved or posted.\n\n"
        "Or type /cancel to abort.",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_test_url)

def save_test_url(message):
    if menu_button_interrupt(message):
        return
    url = (message.text or "").strip()
    if url.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
    if not url.startswith('http'):
        bot.send_message(message.chat.id, "❌ Invalid URL (must start with http/https). Cancelled.")
        send_main_menu(message.chat.id)
        return

    pending_test_url[message.chat.id] = url
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔴 Arsenal", callback_data="test_run_Arsenal"),
        InlineKeyboardButton("🔴 Liverpool", callback_data="test_run_Liverpool"),
        InlineKeyboardButton("🔵 Inter", callback_data="test_run_Inter")
    )
    markup.row(InlineKeyboardButton("No club filter", callback_data="test_run_none"))
    bot.send_message(
        message.chat.id,
        "Which club should this source be tested against? "
        "(Used to check article relevance.)",
        reply_markup=markup
    )

def run_source_test(chat_id, url, team_tag):
    try:
        bot.send_message(chat_id, "🧪 Running source test... (Cloudflare sites may take ~30s)")
        report = scraper.diagnose_source(url, team_tag=team_tag)
        # Telegram hard-limits messages to 4096 chars; send as plain text to avoid
        # Markdown parsing errors on URLs/slugs.
        if len(report) > 4000:
            report = report[:4000] + "\n... (truncated)"
        bot.send_message(chat_id, report)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Source test failed: {e}")
    finally:
        send_main_menu(chat_id)

# TikTok Monitor Views & Flows
def show_tiktok_menu(chat_id, message_id):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("➕ Add Account", callback_data="tt_add"),
        InlineKeyboardButton("❌ Remove Account", callback_data="tt_del_list")
    )
    markup.row(InlineKeyboardButton("🧪 Test an Account", callback_data="tt_test"))
    markup.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu"))

    accounts = database.get_tiktok_accounts()
    if accounts:
        body = "\n".join(f"• <code>@{html.escape(a['handle'])}</code>" for a in accounts)
        text = f"🎵 <b>TikTok Monitor</b>\n{len(accounts)} account(s) monitored:\n{body}"
    else:
        text = "🎵 <b>TikTok Monitor</b>\n<i>No accounts monitored yet.</i>"
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode='HTML')

def show_remove_tiktok_list(chat_id, message_id):
    accounts = database.get_tiktok_accounts()
    markup = InlineKeyboardMarkup()
    if not accounts:
        markup.row(InlineKeyboardButton("🔙 Back", callback_data="tt_menu"))
        bot.edit_message_text("No TikTok accounts to remove.", chat_id, message_id, reply_markup=markup)
        return
    for a in accounts:
        markup.row(InlineKeyboardButton(f"❌ @{a['handle']}", callback_data=f"tt_del_do_{a['id']}"))
    markup.row(InlineKeyboardButton("🔙 Back", callback_data="tt_menu"))
    bot.edit_message_text("Select a TikTok account to remove:", chat_id, message_id, reply_markup=markup)

def prompt_tiktok_account(chat_id):
    msg = bot.send_message(
        chat_id,
        "🎵 *Add a TikTok account to monitor*\n\n"
        "Send the creator's handle or full profile URL — e.g. `khaby.lame`, "
        "`@khaby.lame`, or `https://www.tiktok.com/@khaby.lame`.\n"
        "Only videos posted *after* you add it will be alerted.\n\n"
        "Or type /cancel to abort.",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_tiktok_account)

def save_tiktok_account(message):
    if menu_button_interrupt(message):
        return
    val = (message.text or "").strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
    handle = tiktok_monitor.normalize_handle(val)
    if not handle:
        bot.send_message(message.chat.id, "❌ Invalid handle. Cancelled.")
        send_main_menu(message.chat.id)
        return

    if database.add_tiktok_account(handle):
        bot.send_message(message.chat.id, f"✅ Now monitoring TikTok @{handle}.\nFetching the 3 most recent videos now (this may take a minute)…")
        # Post the 3 most recent videos immediately (in the background so we don't block
        # the handler), then the live monitor takes over for future posts.
        threading.Thread(target=_tiktok_initial_fetch, args=(message.chat.id, handle), daemon=True).start()
    else:
        bot.send_message(message.chat.id, f"❌ @{handle} is already monitored (or invalid).")
    send_main_menu(message.chat.id)

def _tiktok_initial_fetch(chat_id, handle):
    try:
        n = tiktok_monitor.post_recent_videos(bot, handle, limit=3)
        bot.send_message(chat_id, f"📌 Posted {n} recent video(s) from @{handle}. Now live-monitoring for new posts.")
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ @{handle} was added, but the initial fetch failed: {e}")

def prompt_tiktok_test(chat_id):
    msg = bot.send_message(
        chat_id,
        "🧪 *Test a TikTok account*\n\n"
        "Send a handle to dry-run: the bot lists the latest videos it can see "
        "(nothing is downloaded or posted).\n\nOr type /cancel to abort.",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, run_tiktok_test)

def run_tiktok_test(message):
    if menu_button_interrupt(message):
        return
    val = (message.text or "").strip()
    if val.lower() == '/cancel' or not val:
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
    handle = tiktok_monitor.normalize_handle(val)
    bot.send_message(message.chat.id, f"🧪 Checking @{handle}…")
    try:
        vids = tiktok_monitor.fetch_latest_videos(handle)
        if vids:
            lines = "\n".join(f"• {v['url']}" for v in vids)
            bot.send_message(message.chat.id, f"Found {len(vids)} recent video(s) for @{handle}:\n{lines}")
        else:
            bot.send_message(message.chat.id, f"No videos found for @{handle} (private, wrong handle, or TikTok blocked the request).")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Test failed: {e}")
    send_main_menu(message.chat.id)

# Sources Views
def _send_chunked_lines(chat_id, lines, limit=3900, final_markup=None):
    """Sends pre-formatted HTML lines as one or more messages, each kept under
    Telegram's 4096-char hard limit (which a long source list would exceed).
    If final_markup is given, it's attached to the LAST message so action buttons
    appear at the bottom of the list (no scrolling up). Returns the messages sent."""
    chunks = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) > limit:
            if buf:
                chunks.append(buf)
            buf = ""
        buf += line
    if buf:
        chunks.append(buf)

    for i, chunk in enumerate(chunks):
        markup = final_markup if i == len(chunks) - 1 else None
        bot.send_message(chat_id, chunk, parse_mode='HTML', reply_markup=markup)
    return len(chunks)

def show_sources_menu(chat_id, message_id):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("➕ Add Source", callback_data="add_src_type"),
        InlineKeyboardButton("❌ Remove Source", callback_data="del_src_list")
    )
    markup.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu"))

    sources = database.get_sources()

    if sources:
        # Show the list first (chunked), then attach the action buttons to the LAST
        # message so they sit at the bottom — no scrolling up on long lists.
        header = f"📁 <b>Sources Manager</b>\n{len(sources)} source(s) registered. Actions are at the bottom ⬇️"
        bot.edit_message_text(header, chat_id, message_id, parse_mode='HTML')
        lines = [f"• [{s['team_tag']}] <b>{s['type'].upper()}</b>: <code>{html.escape(s['value'])}</code>\n"
                 for s in sources]
        _send_chunked_lines(chat_id, lines, final_markup=markup)
    else:
        # No list: keep the buttons on the header message.
        bot.edit_message_text("📁 <b>Sources Manager</b>\n<i>No sources registered yet.</i>",
                              chat_id, message_id, reply_markup=markup, parse_mode='HTML')

def show_add_source_types(chat_id, message_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("RSS Feed", callback_data="add_src_t_rss"))
    markup.row(InlineKeyboardButton("Web Link", callback_data="add_src_t_web_link"))
    markup.row(InlineKeyboardButton("X (Twitter) Account", callback_data="add_src_t_x_account"))
    markup.row(InlineKeyboardButton("🔙 Back to Sources", callback_data="manage_sources"))
    
    bot.edit_message_text(
        "Select source type:",
        chat_id,
        message_id,
        reply_markup=markup
    )

def show_add_source_teams(chat_id, message_id, stype):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("🔴 Arsenal", callback_data=f"add_src_p_{stype}_Arsenal"))
    markup.row(InlineKeyboardButton("🔴 Liverpool", callback_data=f"add_src_p_{stype}_Liverpool"))
    markup.row(InlineKeyboardButton("🔵 Inter", callback_data=f"add_src_p_{stype}_Inter"))
    markup.row(InlineKeyboardButton("🔙 Back", callback_data="add_src_type"))
    
    bot.edit_message_text(
        f"Select team tag for new *{stype.upper()}* source:",
        chat_id,
        message_id,
        reply_markup=markup,
        parse_mode='Markdown'
    )

def prompt_source_value(chat_id, stype, team):
    type_prompts = {
        'rss': "Type the XML RSS Feed URL (e.g. https://www.arsenal.com/news/rss):",
        'web_link': "Type the direct web article/news page URL (e.g. https://www.bbc.co.in/football):",
        'x_account': "Type the X account username with @ (e.g. @FabrizioRomano):"
    }
    
    msg = bot.send_message(chat_id, type_prompts[stype])
    bot.register_next_step_handler(msg, save_source_input, stype, team)

def save_source_input(message, stype, team):
    if menu_button_interrupt(message):
        return
    value = message.text.strip()
    if not value:
        bot.send_message(message.chat.id, "❌ Cancelled: Invalid/empty value.")
        send_main_menu(message.chat.id)
        return
        
    if stype == 'x_account':
        success = database.add_source(stype, value, team)
        if success:
            bot.send_message(message.chat.id, f"✅ Successfully added source: <b>{stype.upper()}</b> for <b>{team}</b>", parse_mode='HTML')
        else:
            bot.send_message(message.chat.id, f"❌ Failed: Source <b>{html.escape(value)}</b> already exists for {team} under type {stype}.", parse_mode='HTML')
    else:
        # Run RSS & Cloudflare auto-detection!
        bot.send_message(message.chat.id, "🔍 Analyzing URL and auto-detecting configuration...")
        
        try:
            detected_stype, resolved_url, desc_msg = scraper.auto_detect_source_classification(value)
            
            success = database.add_source(detected_stype, resolved_url, team)
            if success:
                bot.send_message(
                    message.chat.id,
                    f"✅ <b>Source Added Successfully!</b>\n\n"
                    f"• <b>Auto-Detected Type:</b> <code>{detected_stype.upper()}</code>\n"
                    f"• <b>Resolved Value:</b> <code>{html.escape(resolved_url)}</code>\n"
                    f"• <b>Reason:</b> {html.escape(desc_msg)}",
                    parse_mode='HTML'
                )
            else:
                bot.send_message(
                    message.chat.id,
                    f"❌ <b>Failed:</b> Source already exists as <code>{detected_stype.upper()}</code>:\n<code>{html.escape(resolved_url)}</code>",
                    parse_mode='HTML'
                )
        except Exception as e:
            # Fallback in case of error
            success = database.add_source(stype, value, team)
            if success:
                bot.send_message(message.chat.id, f"✅ Added source: <b>{stype.upper()}</b> for <b>{team}</b> (Fallback)", parse_mode='HTML')
            else:
                bot.send_message(message.chat.id, f"❌ Failed: {html.escape(str(e))}", parse_mode='HTML')
                
    send_main_menu(message.chat.id)

def show_remove_source_list(chat_id, message_id):
    sources = database.get_sources()
    markup = InlineKeyboardMarkup()
    
    if not sources:
        markup.row(InlineKeyboardButton("🔙 Back", callback_data="manage_sources"))
        bot.edit_message_text("No sources found to remove.", chat_id, message_id, reply_markup=markup)
        return
        
    for s in sources:
        display_val = s['value'][:25] + "..." if len(s['value']) > 25 else s['value']
        markup.row(InlineKeyboardButton(f"❌ [{s['team_tag']}] {display_val}", callback_data=f"del_src_do_{s['id']}"))
        
    markup.row(InlineKeyboardButton("🔙 Back to Sources", callback_data="manage_sources"))
    bot.edit_message_text("Select a source to remove:", chat_id, message_id, reply_markup=markup)

# Filters Views
def show_filters_menu(chat_id, message_id):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("➕ Add Keyword", callback_data="add_flt_prompt"),
        InlineKeyboardButton("❌ Remove Keyword", callback_data="del_flt_list")
    )
    markup.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu"))
    
    filters = database.get_filters()
    flt_text = ""
    if not filters:
        flt_text = "\n<i>No filter keywords registered yet.</i>"
    else:
        for f in filters:
            flt_text += f"\n• <code>{html.escape(f['keyword'])}</code>"
            
    bot.edit_message_text(
        f"🔍 <b>Filter Keywords Manager</b>\n"
        f"Articles containing these keywords will be ignored by Gemini.{flt_text}",
        chat_id,
        message_id,
        reply_markup=markup,
        parse_mode='HTML'
    )

def prompt_filter_value(chat_id):
    msg = bot.send_message(chat_id, "Type the filter keyword/phrase (case-insensitive):")
    bot.register_next_step_handler(msg, save_filter_input)

def save_filter_input(message):
    if menu_button_interrupt(message):
        return
    value = message.text.strip()
    if not value:
        bot.send_message(message.chat.id, "❌ Cancelled: Invalid/empty filter.")
        send_main_menu(message.chat.id)
        return
        
    success = database.add_filter(value)
    if success:
        bot.send_message(message.chat.id, f"✅ Added filter keyword: <code>{html.escape(value.lower())}</code>", parse_mode='HTML')
    else:
        bot.send_message(message.chat.id, "❌ Filter keyword already exists.")
        
    send_main_menu(message.chat.id)

def show_remove_filter_list(chat_id, message_id):
    filters = database.get_filters()
    markup = InlineKeyboardMarkup()
    
    if not filters:
        markup.row(InlineKeyboardButton("🔙 Back", callback_data="manage_filters"))
        bot.edit_message_text("No filter keywords found to remove.", chat_id, message_id, reply_markup=markup)
        return
        
    for f in filters:
        markup.row(InlineKeyboardButton(f"❌ {f['keyword']}", callback_data=f"del_flt_do_{f['id']}"))
        
    markup.row(InlineKeyboardButton("🔙 Back to Filters", callback_data="manage_filters"))
    bot.edit_message_text("Select a filter keyword to remove:", chat_id, message_id, reply_markup=markup)

# X Account Swapping Flow
def update_env_file(username, password, email):
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"Failed to read .env file: {e}")
            
    username_found = False
    password_found = False
    email_found = False
    
    for i, line in enumerate(lines):
        if line.startswith("X_USERNAME="):
            lines[i] = f"X_USERNAME={username}\n"
            username_found = True
        elif line.startswith("X_PASSWORD="):
            lines[i] = f"X_PASSWORD={password}\n"
            password_found = True
        elif line.startswith("X_EMAIL="):
            lines[i] = f"X_EMAIL={email}\n"
            email_found = True
            
    if not username_found:
        lines.append(f"X_USERNAME={username}\n")
    if not password_found:
        lines.append(f"X_PASSWORD={password}\n")
    if not email_found:
        lines.append(f"X_EMAIL={email}\n")
        
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        logger.error(f"Failed to write .env file: {e}")
        raise

def prompt_new_username(chat_id):
    msg = bot.send_message(
        chat_id, 
        "👤 *Set up a new X (Twitter) account*\n\n"
        "Please send the *new X username* without the @ character:\n"
        "(example: `trendia_x`)\n\n"
        "Or type /cancel to abort.",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_new_username)

def save_new_username(message):
    if menu_button_interrupt(message):
        return
    val = message.text.strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    if not val:
        bot.send_message(message.chat.id, "❌ Invalid value. Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(message.chat.id, "Please send the *new X password*:", parse_mode='Markdown')
    bot.register_next_step_handler(msg, save_new_password, val)

def save_new_password(message, username):
    if menu_button_interrupt(message):
        return
    val = message.text.strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    if not val:
        bot.send_message(message.chat.id, "❌ Invalid value. Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(message.chat.id, "Please send the *email associated with the new X account*:", parse_mode='Markdown')
    bot.register_next_step_handler(msg, save_new_email, username, val)

def save_new_email(message, username, password):
    if menu_button_interrupt(message):
        return
    val = message.text.strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    if not val:
        bot.send_message(message.chat.id, "❌ Invalid value. Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(
        message.chat.id, 
        "Account details received. Now please send the new `auth_token` cookie for this new account:",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_new_auth_token, username, password, val)

def save_new_auth_token(message, username, password, email):
    if menu_button_interrupt(message):
        return
    val = message.text.strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    if not val:
        bot.send_message(message.chat.id, "❌ Invalid value. Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(
        message.chat.id, 
        "`auth_token` received. Now please send the new `ct0` cookie:",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_new_ct0, username, password, email, val)

def save_new_ct0(message, username, password, email, auth_token):
    if menu_button_interrupt(message):
        return
    ct0 = message.text.strip()
    if ct0.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    if not ct0:
        bot.send_message(message.chat.id, "❌ Invalid value. Cancelled.")
        send_main_menu(message.chat.id)
        return
        
    from twikit import Client
    from dotenv import load_dotenv
    
    cookies_data = {
        "auth_token": auth_token,
        "ct0": ct0
    }
    
    try:
        client = Client('en-US', proxy=config.PROXY_URL) if config.PROXY_URL else Client('en-US')
        client.set_cookies(cookies_data)
        client.save_cookies("cookies.json")
            
        update_env_file(username, password, email)
        
        load_dotenv(override=True)
        config.X_USERNAME = os.getenv("X_USERNAME")
        config.X_PASSWORD = os.getenv("X_PASSWORD")
        config.X_EMAIL = os.getenv("X_EMAIL")
        
        bot.send_message(
            message.chat.id, 
            "💾 The new account and cookies were saved successfully on the server.\n"
            "Testing the connection with the new credentials..."
        )
        
        test_client = scraper.XScraper()
        if test_client.mock_mode:
            bot.send_message(
                message.chat.id, 
                "❌ Authentication error: Twitter blocked the connection for the new account.\n"
                "X live ingestion is now disabled (no tweets will be posted). Please re-check the account details and cookie validity."
            )
        else:
            bot.send_message(message.chat.id, "✅ Success: Live connection established with the new account!")

    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Failed to apply the new settings: {e}")
        
    send_main_menu(message.chat.id)

# Cookie Updating Next-Step Flow
def prompt_auth_token(chat_id):
    msg = bot.send_message(
        chat_id, 
        "Please send the new `auth_token` value for Twitter:\n"
        "(example: `4a800d70d277f3da...`)\n\n"
        "Send it, or type /cancel to abort.",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_auth_token)

def save_auth_token(message):
    if menu_button_interrupt(message):
        return
    token = message.text.strip()
    if token.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Update process cancelled.")
        send_main_menu(message.chat.id)
        return
        
    if not token:
        bot.send_message(message.chat.id, "❌ Invalid value. Operation cancelled.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(
        message.chat.id, 
        "`auth_token` received.\n"
        "Now please send the new `ct0` value:\n"
        "(example: `92c20e279c...`)\n\n"
        "Send it, or type /cancel to abort.",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_ct0, token)

def save_ct0(message, auth_token):
    if menu_button_interrupt(message):
        return
    ct0 = message.text.strip()
    if ct0.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ Update process cancelled.")
        send_main_menu(message.chat.id)
        return
        
    if not ct0:
        bot.send_message(message.chat.id, "❌ Invalid value. Operation cancelled.")
        send_main_menu(message.chat.id)
        return
        
    from twikit import Client
    cookies_data = {
        "auth_token": auth_token,
        "ct0": ct0
    }
    
    try:
        client = Client('en-US', proxy=config.PROXY_URL) if config.PROXY_URL else Client('en-US')
        client.set_cookies(cookies_data)
        client.save_cookies("cookies.json")
            
        bot.send_message(message.chat.id, "💾 `cookies.json` was updated successfully on the server.\nTesting the live connection...")
        
        # Test the connection immediately
        test_client = scraper.XScraper()
        if test_client.mock_mode:
            bot.send_message(
                message.chat.id, 
                "❌ Authentication error: Twitter blocked the connection with these cookies.\n"
                "X live ingestion will stay disabled (no tweets will be posted). Please check the cookies and try again."
            )
        else:
            bot.send_message(message.chat.id, "✅ Success: Live login established! The X session is working correctly.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Failed to write the cookies file: {e}")
        
    send_main_menu(message.chat.id)


def run_preflight_checks() -> bool:
    print("=" * 60)
    print("               PRE-FLIGHT CONNECTIVITY CHECKS              ")
    print("=" * 60)
    
    success = True
    
    # 1. Check Telegram Bot Token & Chat ID
    print("Checking Telegram Bot Token...")
    try:
        me = bot.get_me()
        print(f"  ✅ Telegram Token OK: Bot Username is @{me.username}")
    except Exception as e:
        print(f"  ❌ Telegram Token Error: {e}")
        success = False
        
    print("Checking Telegram Chat ID Access...")
    try:
        chat = bot.get_chat(config.TELEGRAM_CHAT_ID)
        print(f"  ✅ Telegram Chat OK: Found group/channel '{chat.title}'")
    except Exception as e:
        print(f"  ❌ Telegram Chat Error: Could not access Chat ID {config.TELEGRAM_CHAT_ID}. {e}")
        success = False

    # 2. Check Gemini API Key
    print("Checking Gemini API Connectivity...")
    if not config.GEMINI_API_KEY:
        print("  ❌ Gemini Error: GEMINI_API_KEY is not configured in .env.")
        success = False
    else:
        try:
            from google import genai
            client = genai.Client(api_key=config.GEMINI_API_KEY)
            client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents="Test"
            )
            print(f"  ✅ Gemini API OK: Model '{config.GEMINI_MODEL}' responded successfully.")
        except Exception as e:
            print(f"  ❌ Gemini API Error: {e}")
            print("     (Please verify your API key is correct and Generative Language API is enabled.)")
            success = False

    # 3. Check X/Twitter Ingest Status
    print("Checking X/Twitter Ingest Status...")
    try:
        x_client = scraper.XScraper()
        if x_client.mock_mode:
            if config.X_USERNAME:
                print("  ⚠️  X Scraper Warn: Live login failed. X ingestion disabled (no tweets produced).")
            else:
                print("  ℹ️  X Scraper Status: X ingestion disabled (no credentials, no tweets produced).")
        else:
            print("  ✅ X Scraper OK: Successfully authenticated using cookies.json.")
    except Exception as e:
        print(f"  ⚠️  X Scraper Warn: Could not verify status: {e}")

    print("=" * 60)
    return success


if __name__ == "__main__":
    import sys
    
    # 1. Run Pre-flight Connectivity Checks
    if not run_preflight_checks():
        print("\n❌ CRITICAL: Pre-flight checks failed. Please resolve the errors above before running the bot.")
        sys.exit(1)
        
    print("\n🚀 All critical pre-flight checks passed! Initializing bot services...")
    
    # 2. Initialize DB tables on startup
    database.init_db()
    
    # 3. Launch the FAST scheduler loop (RSS/web/X/Google News) in a separate thread
    scheduler_t = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_t.start()

    # 3a. Launch the SLOW scheduler loop (Cloudflare/DrissionPage sources)
    protected_t = threading.Thread(target=protected_scheduler_loop, daemon=True)
    protected_t.start()

    # 3b. Launch the isolated TikTok monitor loop in its own thread
    tiktok_t = threading.Thread(target=tiktok_monitor.tiktok_loop, args=(bot,), daemon=True)
    tiktok_t.start()

    # 4. Start Telegram Long Polling
    logger.info("Bot starting Telegram Long-Polling...")
    bot.infinity_polling()
