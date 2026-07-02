"""TikTok Media Monitor & Downloader (standalone module).

Watches monitored TikTok creators and, when they post a new video, downloads it with
yt-dlp and posts it to Telegram as a native (autoplay) video plus the original link.

Fully isolated from the news pipeline: no Gemini, its own scheduler loop, its own tables,
and every step is wrapped in try/except so a TikTok failure never affects the news bot.
It never fabricates data — on any fetch/download failure it simply skips.
"""
import os
import time
import shutil
import logging
import tempfile

import config
import database

logger = logging.getLogger("tiktok")


def normalize_handle(text: str) -> str:
    """Extracts a clean TikTok handle from a full URL, an @handle, or a bare handle.
    Strips the scheme/host, a leading '@', any query string (?_r=...&_t=...) or path/slash,
    and lowercases. e.g. 'https://www.tiktok.com/@vago.int?_r=1&_t=...' -> 'vago.int'."""
    if not text:
        return ""
    h = text.strip()
    if '/@' in h:                 # full URL form: .../@handle...
        h = h.split('/@', 1)[1]
    h = h.lstrip('@')
    h = h.split('?', 1)[0]        # drop query string
    h = h.split('/', 1)[0]        # drop any trailing path
    return h.strip().lower()


# --- Fetch layer (the single pluggable point; swap for a paid API if needed) ---
def fetch_latest_videos(handle: str, limit: int = None) -> list[dict]:
    """Returns the latest videos for a creator as [{video_id, url, title}], newest first.
    Uses yt-dlp flat extraction (no download). Returns [] on any failure."""
    limit = limit or config.TIKTOK_FETCH_LIMIT
    handle = handle.lstrip('@')
    url = f"https://www.tiktok.com/@{handle}"
    try:
        import yt_dlp
        opts = {
            'quiet': True, 'no_warnings': True,
            'extract_flat': True, 'skip_download': True,
            'playlistend': limit,
        }
        if config.PROXY_URL:
            opts['proxy'] = config.PROXY_URL
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = (info.get('entries') or [])[:limit]
        results = []
        for e in entries:
            vid = e.get('id')
            if not vid:
                continue
            results.append({
                'video_id': str(vid),
                'url': e.get('url') or e.get('webpage_url') or f"{url}/video/{vid}",
                'title': (e.get('title') or '').strip(),
            })
        return results
    except Exception as e:
        logger.error(f"Failed to list TikTok videos for @{handle}: {e}")
        return []


def download_video(url: str, dest_dir: str) -> tuple[str | None, dict]:
    """Downloads a single TikTok video as MP4 into dest_dir, capped near the Telegram
    upload limit. Returns (path | None, meta) where meta has width/height/duration."""
    try:
        import yt_dlp
        max_bytes = config.TIKTOK_MAX_VIDEO_MB * 1024 * 1024
        opts = {
            'quiet': True, 'no_warnings': True,
            'outtmpl': os.path.join(dest_dir, '%(id)s.%(ext)s'),
            # Prefer an MP4 that fits the size cap; fall back to best available.
            'format': (f'best[ext=mp4][filesize<{max_bytes}]/'
                       f'bestvideo[ext=mp4][filesize<{max_bytes}]+bestaudio/'
                       f'best[ext=mp4]/best'),
            'merge_output_format': 'mp4',
        }
        if config.PROXY_URL:
            opts['proxy'] = config.PROXY_URL
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        vid = info.get('id')
        path = os.path.join(dest_dir, f"{vid}.mp4")
        if not os.path.exists(path):
            # Find whatever file was produced
            files = [f for f in os.listdir(dest_dir) if f.startswith(str(vid))]
            path = os.path.join(dest_dir, files[0]) if files else None
        meta = {
            'width': info.get('width'),
            'height': info.get('height'),
            'duration': info.get('duration'),
            'description': (info.get('description') or '').strip(),
        }
        return path, meta
    except Exception as e:
        logger.error(f"Failed to download TikTok video {url}: {e}")
        return None, {}


# --- Metadata layer (caption + top comments; NO AI) ---
def fetch_top_comments(video_id: str, video_url: str, limit: int = 3) -> list[dict]:
    """Returns the top `limit` most-liked comments for a TikTok video as
    [{text, likes}], sorted by like count. Uses TikTok's public comment endpoint
    with browser headers. Robust: returns [] on any failure."""
    try:
        import requests
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': video_url,
        }
        api = (f"https://www.tiktok.com/api/comment/list/?aweme_id={video_id}"
               f"&count=30&cursor=0&app_language=en&aid=1988")
        proxies = {'http': config.PROXY_URL, 'https': config.PROXY_URL} if config.PROXY_URL else None
        r = requests.get(api, headers=headers, timeout=15, proxies=proxies)
        comments = (r.json().get('comments') or [])
        top = sorted(comments, key=lambda c: (c.get('digg_count') or 0), reverse=True)[:limit]
        out = []
        for c in top:
            text = (c.get('text') or '').strip()
            if text:
                out.append({'text': text, 'likes': c.get('digg_count')})
        return out
    except Exception as e:
        logger.warning(f"Could not fetch comments for {video_id}: {e}")
        return []


def build_metadata_block(handle: str, url: str, caption: str, comments: list[dict]) -> str:
    """Builds the raw text block posted with each video: original caption + top comments.
    No AI/summarization — verbatim content only."""
    lines = [f"🎵 @{handle}"]
    if caption:
        lines += ["", caption]
    if comments:
        lines += ["", "💬 Top comments:"]
        for i, c in enumerate(comments, 1):
            likes = c.get('likes')
            like_str = f" (♥{likes:,})" if isinstance(likes, int) else ""
            lines.append(f"{i}.{like_str} {c['text']}")
    lines += ["", url]
    return "\n".join(lines)


# --- Posting layer ---
def _format_caption(handle: str, url: str) -> str:
    return f"🎵 New TikTok from @{handle}\n{url}"


_CAPTION_LIMIT = 1024  # Telegram media caption hard limit

def _send_native_video(bot, path: str, meta: dict, handle: str, url: str, text_block: str) -> bool:
    """Uploads the video natively (autoplay) with the metadata text block. If the block
    fits Telegram's 1024-char caption limit it becomes the caption; otherwise the video
    is sent with a short caption and the full block follows as a separate message."""
    try:
        if len(text_block) <= _CAPTION_LIMIT:
            caption, followup = text_block, None
        else:
            caption, followup = _format_caption(handle, url), text_block
        with open(path, 'rb') as f:
            bot.send_video(
                chat_id=config.TIKTOK_CHAT_ID,
                video=f,
                caption=caption,
                supports_streaming=True,
                width=meta.get('width'),
                height=meta.get('height'),
                duration=meta.get('duration'),
                message_thread_id=config.TIKTOK_THREAD_ID,
            )
        if followup:
            bot.send_message(chat_id=config.TIKTOK_CHAT_ID, text=followup[:4096],
                             message_thread_id=config.TIKTOK_THREAD_ID)
        logger.info(f"Posted native TikTok video for @{handle}: {url}")
        return True
    except Exception as e:
        logger.warning(f"Native video upload failed for @{handle} ({url}): {e}")
        # Fallback: at least send the metadata text so the alert is not lost.
        try:
            bot.send_message(
                chat_id=config.TIKTOK_CHAT_ID,
                text=(text_block or _format_caption(handle, url))[:4096] + "\n(video failed to upload)",
                message_thread_id=config.TIKTOK_THREAD_ID,
            )
            return True
        except Exception as e2:
            logger.error(f"Fallback link message also failed for @{handle}: {e2}")
            return False


# --- Per-video processing (shared by the live cycle and the initial fetch) ---
def process_and_post_video(bot, handle: str, video: dict) -> bool:
    """Downloads one video, gathers its caption + top-3 liked comments, posts it to
    Telegram (video + metadata text block, NO AI), and marks it seen. Returns True if
    a message was sent."""
    vid, url = video['video_id'], video['url']
    logger.info(f"Processing TikTok video from @{handle}: {url}")
    tmp = tempfile.mkdtemp(prefix="tt_")
    try:
        path, meta = download_video(url, tmp)
        caption = meta.get('description') or (video.get('title') or '')
        comments = fetch_top_comments(vid, url, limit=3)
        block = build_metadata_block(handle, url, caption, comments)
        if path and os.path.exists(path):
            sent = _send_native_video(bot, path, meta, handle, url, block)
        else:
            # Could not download the file; still post the metadata + link.
            bot.send_message(chat_id=config.TIKTOK_CHAT_ID,
                             text=block[:4096] + "\n(video download failed)",
                             message_thread_id=config.TIKTOK_THREAD_ID)
            sent = True
        database.mark_tiktok_video_seen(handle, vid)  # mark seen either way (no retry loops)
        return sent
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- Account onboarding: post the N most recent videos immediately, then monitor ---
def post_recent_videos(bot, handle: str, limit: int = 3) -> int:
    """Called when a new account is registered: downloads and posts the `limit` most
    recent videos (with caption + top comments), marks their IDs seen, then the regular
    monitor loop takes over for future posts. Returns how many were posted."""
    handle = normalize_handle(handle)
    vids = fetch_latest_videos(handle, limit)[:limit]
    posted = 0
    for v in reversed(vids):  # oldest-first so newest ends up on top
        if database.is_tiktok_video_seen(handle, v['video_id']):
            continue
        try:
            if process_and_post_video(bot, handle, v):
                posted += 1
            time.sleep(2)
        except Exception as e:
            logger.error(f"Error posting recent video for @{handle}: {e}")
    logger.info(f"Initial fetch for @{handle}: posted {posted} recent video(s).")
    return posted


# --- Cycle / loop ---
def run_tiktok_cycle(bot):
    """One monitoring pass over all accounts: detect new videos, download, post."""
    accounts = database.get_tiktok_accounts()
    if not accounts:
        return
    logger.info(f"TikTok cycle: checking {len(accounts)} creator(s)...")

    for acc in accounts:
        handle = acc['handle']
        try:
            videos = fetch_latest_videos(handle)
            # Oldest-first so posts arrive in chronological order.
            for v in reversed(videos):
                if database.is_tiktok_video_seen(handle, v['video_id']):
                    continue
                logger.info(f"New TikTok detected from @{handle}: {v['url']}")
                try:
                    process_and_post_video(bot, handle, v)
                except Exception as e:
                    logger.error(f"Error posting video {v['url']} for @{handle}: {e}")
                time.sleep(2)  # gentle pacing between uploads
        except Exception as e:
            logger.error(f"Error processing TikTok creator @{handle}: {e}")

    try:
        database.prune_tiktok_seen(config.TIKTOK_SEEN_RETENTION_DAYS)
    except Exception as e:
        logger.error(f"Error pruning tiktok_seen_videos: {e}")


def tiktok_loop(bot):
    """Background loop. Isolated from the news scheduler."""
    logger.info("Starting TikTok monitor loop...")
    while True:
        try:
            run_tiktok_cycle(bot)
        except Exception as e:
            logger.error(f"Error in TikTok monitor loop: {e}")
        logger.info(f"TikTok cycle complete. Sleeping {config.TIKTOK_CYCLE_SECONDS}s.")
        time.sleep(config.TIKTOK_CYCLE_SECONDS)
