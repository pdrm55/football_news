import os
import time
import logging
import re
import threading
import html
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import config
import database
import scraper

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
    return None

# Message Formatting
def format_broadcast(summary: str, url: str) -> str:
    """Formats the news post using a clean, plain text layout without markdown or emojis."""
    # Check if the unique identifier is a valid URL, otherwise omit the Source URL line
    if not url or not (url.startswith('http://') or url.startswith('https://')):
        return (
            "BREAKING UPDATE\n"
            "====================\n"
            f"{summary}\n"
            "===================="
        )
        
    return (
        "BREAKING UPDATE\n"
        "====================\n"
        f"{summary}\n"
        "====================\n"
        f"Source URL: {url}"
    )

# Broadcasting Layer
def send_telegram_broadcast(summary: str, url: str, media_url: str | None, thread_id: int | None, art_id: int, team_tag: str | None) -> bool:
    """Delivers the post to Telegram as plain text. If thread_id fails (e.g. topic not found),
    it automatically falls back to sending directly to the main chat.
    """
    formatted_msg = format_broadcast(summary, url)
    
    # Try photo first if media_url is provided
    if media_url:
        try:
            bot.send_photo(
                chat_id=config.TELEGRAM_CHAT_ID,
                photo=media_url,
                caption=formatted_msg,
                message_thread_id=thread_id
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
                        message_thread_id=None
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
            message_thread_id=thread_id
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
                    message_thread_id=None
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
        
        # 1. Skip Gemini for X/Twitter posts since they are already short/summarized
        if source_type == 'x_account':
            logger.info(f"Article {art_id} is from X/Twitter. Bypassing Gemini API and using raw text.")
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
    x_scraper = scraper.XScraper()
    
    while True:
        try:
            logger.info("Running scheduled cycle...")
            
            # Check X cookies status and alert admin if expired
            if config.X_USERNAME:
                if x_scraper.mock_mode:
                    if not cookie_alert_sent:
                        logger.warning("X Scraper fell back to Mock mode. Sending cookie alert to admin...")
                        try:
                            bot.send_message(
                                chat_id=config.ADMIN_USER_ID,
                                text=(
                                    "⚠️ **هشدار امنیتی X (Twitter):**\n"
                                    "نشست کوکی‌های X منقضی شده یا با شکست مواجه شده است. خزش زنده متوقف گردید.\n"
                                    "لطفاً از طریق دکمه `Update X Cookies` در منوی `/settings` ربات، کوکی‌های جدید را ثبت کنید."
                                ),
                                parse_mode='Markdown'
                            )
                            cookie_alert_sent = True
                        except Exception as alert_err:
                            logger.error(f"Failed to send admin cookie alert: {alert_err}")
                else:
                    cookie_alert_sent = False

            # 1. Ingest new articles
            scraper.run_scraper_ingestion(x_scraper=x_scraper)
            # 2. Process and Broadcast in real-time
            process_and_broadcast_pipeline()
            # 3. Clean up database records older than retention period
            try:
                database.delete_old_articles(days=config.DB_RETENTION_DAYS)
            except Exception as prune_err:
                logger.error(f"Error pruning database: {prune_err}")
        except Exception as e:
            logger.error(f"Error in background scheduler: {e}")
        
        logger.info(f"Scheduled cycle completed. Sleeping for {config.SCHEDULER_CYCLE_SECONDS} seconds.")
        time.sleep(config.SCHEDULER_CYCLE_SECONDS)

# Telegram Command Handlers
@bot.message_handler(commands=['start'])
def handle_start(message):
    bot.reply_to(
        message,
        "⚽ *Football News Aggregator & Summarizer Bot*\n\n"
        "This bot runs background ingestion and broadcasts compiled news to "
        "the designated group topics. Admin configurations can be opened "
        "in private using `/settings`.",
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['settings'])
def handle_settings(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Access Denied: You are not authorized as the Administrator.")
        return
    
    if message.chat.type != "private":
        bot.reply_to(message, "⚠️ The settings panel is only available in private chat.")
        return

    send_main_menu(message.chat.id)

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
        
    elif data == "update_x_cookies":
        prompt_auth_token(chat_id)
        
    elif data == "switch_x_account":
        prompt_new_username(chat_id)
        
    elif data == "add_src_type":
        show_add_source_types(chat_id, message_id)
        
    elif data.startswith("add_src_t_"):
        # Selected source type
        stype = data.replace("add_src_t_", "")
        show_add_source_teams(chat_id, message_id, stype)
        
    elif data.startswith("add_src_p_"):
        # Format: add_src_p_{type}_{team}
        parts = data.replace("add_src_p_", "").split("_", 1)
        stype = parts[0]
        team = parts[1]
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

# Manual Execution Function
def run_manual_cycle(chat_id):
    try:
        scraper.run_scraper_ingestion()
        process_and_broadcast_pipeline()
        bot.send_message(chat_id, "✅ Scraper ingestion and AI pipeline run completed.")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Manual scraper run failed: {e}")

# Sources Views
def show_sources_menu(chat_id, message_id):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("➕ Add Source", callback_data="add_src_type"),
        InlineKeyboardButton("❌ Remove Source", callback_data="del_src_list")
    )
    markup.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu"))
    
    # List active sources
    sources = database.get_sources()
    src_text = ""
    if not sources:
        src_text = "\n<i>No sources registered yet.</i>"
    else:
        for s in sources:
            src_text += f"\n• [{s['team_tag']}] <b>{s['type'].upper()}</b>: <code>{html.escape(s['value'])}</code>"
            
    bot.edit_message_text(
        f"📁 <b>Sources Manager</b>\n{src_text}",
        chat_id,
        message_id,
        reply_markup=markup,
        parse_mode='HTML'
    )

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
        "👤 **تنظیم اکانت جدید X (توییتر)**\n\n"
        "لطفاً **نام‌کاربری جدید X** را بدون کاراکتر @ ارسال کنید:\n"
        "(مثال: `trendia_x`)\n\n"
        "یا بنویسید /cancel برای انصراف."
    )
    bot.register_next_step_handler(msg, save_new_username)

def save_new_username(message):
    val = message.text.strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    if not val:
        bot.send_message(message.chat.id, "❌ مقدار نامعتبر است. انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(message.chat.id, "لطفاً **رمز عبور جدید X** را ارسال کنید:")
    bot.register_next_step_handler(msg, save_new_password, val)

def save_new_password(message, username):
    val = message.text.strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    if not val:
        bot.send_message(message.chat.id, "❌ مقدار نامعتبر است. انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(message.chat.id, "لطفاً **ایمیل جدید مرتبط با اکانت X** را ارسال کنید:")
    bot.register_next_step_handler(msg, save_new_email, username, val)

def save_new_email(message, username, password):
    val = message.text.strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    if not val:
        bot.send_message(message.chat.id, "❌ مقدار نامعتبر است. انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(
        message.chat.id, 
        "مشخصات اکانت دریافت شد. حالا لطفاً کوکی جدید **`auth_token`** را برای این اکانت جدید ارسال کنید:",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_new_auth_token, username, password, val)

def save_new_auth_token(message, username, password, email):
    val = message.text.strip()
    if val.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    if not val:
        bot.send_message(message.chat.id, "❌ مقدار نامعتبر است. انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(
        message.chat.id, 
        "مقدار `auth_token` دریافت شد. حالا لطفاً کوکی جدید **`ct0`** را ارسال کنید:",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_new_ct0, username, password, email, val)

def save_new_ct0(message, username, password, email, auth_token):
    ct0 = message.text.strip()
    if ct0.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ انصراف داده شد.")
        send_main_menu(message.chat.id)
        return
        
    if not ct0:
        bot.send_message(message.chat.id, "❌ مقدار نامعتبر است. انصراف داده شد.")
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
            "💾 اکانت جدید و کوکی‌ها با موفقیت روی سرور ذخیره شدند.\n"
            "در حال تست اتصال با مشخصات جدید..."
        )
        
        test_client = scraper.XScraper()
        if test_client.mock_mode:
            bot.send_message(
                message.chat.id, 
                "❌ خطای احراز هویت: توییتر اتصال اکانت جدید را مسدود کرد.\n"
                "ربات به حالت شبیه‌ساز (Mock) بازگشت. لطفاً مشخصات اکانت و صحت کوکی‌ها را مجدداً بررسی کنید."
            )
        else:
            bot.send_message(message.chat.id, "✅ موفقیت: اتصال لایو با اکانت جدید برقرار شد!")
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطا در اعمال تنظیمات جدید: {e}")
        
    send_main_menu(message.chat.id)

# Cookie Updating Next-Step Flow
def prompt_auth_token(chat_id):
    msg = bot.send_message(
        chat_id, 
        "لطفاً مقدار جدید **`auth_token`** را برای توییتر ارسال کنید:\n"
        "(مثال: `4a800d70d277f3da...`)\n\n"
        "ارسال کنید یا بنویسید /cancel برای انصراف.",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_auth_token)

def save_auth_token(message):
    token = message.text.strip()
    if token.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ فرآیند بروزرسانی لغو شد.")
        send_main_menu(message.chat.id)
        return
        
    if not token:
        bot.send_message(message.chat.id, "❌ مقدار نامعتبر است. عملیات لغو شد.")
        send_main_menu(message.chat.id)
        return
        
    msg = bot.send_message(
        message.chat.id, 
        "مقدار `auth_token` دریافت شد.\n"
        "حالا لطفاً مقدار جدید **`ct0`** را ارسال کنید:\n"
        "(مثال: `92c20e279c...`)\n\n"
        "ارسال کنید یا بنویسید /cancel برای انصراف.",
        parse_mode='Markdown'
    )
    bot.register_next_step_handler(msg, save_ct0, token)

def save_ct0(message, auth_token):
    ct0 = message.text.strip()
    if ct0.lower() == '/cancel':
        bot.send_message(message.chat.id, "❌ فرآیند بروزرسانی لغو شد.")
        send_main_menu(message.chat.id)
        return
        
    if not ct0:
        bot.send_message(message.chat.id, "❌ مقدار نامعتبر است. عملیات لغو شد.")
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
            
        bot.send_message(message.chat.id, "💾 فایل `cookies.json` با موفقیت روی سرور بروزرسانی شد.\nدر حال تست اتصال زنده...")
        
        # Test the connection immediately
        test_client = scraper.XScraper()
        if test_client.mock_mode:
            bot.send_message(
                message.chat.id, 
                "❌ خطای احراز هویت: توییتر اتصال با این کوکی‌ها را مسدود کرد.\n"
                "ربات همچنان در حالت شبیه‌ساز (Mock) خواهد بود. لطفاً کوکی‌ها را بررسی و مجدداً تلاش کنید."
            )
        else:
            bot.send_message(message.chat.id, "✅ موفقیت: لاگین زنده برقرار شد! نشست X به درستی کار می‌کند.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ خطا در نوشتن فایل کوکی‌ها: {e}")
        
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
                print("  ⚠️  X Scraper Warn: Live login failed. Running in Simulator Mode.")
            else:
                print("  ℹ️  X Scraper Status: Running in Simulator Mode (no credentials).")
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
    
    # 3. Launch Background Scheduler Loop in a separate thread
    scheduler_t = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_t.start()
    
    # 4. Start Telegram Long Polling
    logger.info("Bot starting Telegram Long-Polling...")
    bot.infinity_polling()
