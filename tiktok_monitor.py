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
        }
        return path, meta
    except Exception as e:
        logger.error(f"Failed to download TikTok video {url}: {e}")
        return None, {}


# --- Posting layer ---
def _format_caption(handle: str, url: str) -> str:
    return f"🎵 New TikTok from @{handle}\n{url}"


def _send_native_video(bot, path: str, meta: dict, handle: str, url: str) -> bool:
    """Uploads the video natively so Telegram autoplays it. Returns True on success."""
    try:
        with open(path, 'rb') as f:
            bot.send_video(
                chat_id=config.TIKTOK_CHAT_ID,
                video=f,
                caption=_format_caption(handle, url),
                supports_streaming=True,
                width=meta.get('width'),
                height=meta.get('height'),
                duration=meta.get('duration'),
                message_thread_id=config.TIKTOK_THREAD_ID,
            )
        logger.info(f"Posted native TikTok video for @{handle}: {url}")
        return True
    except Exception as e:
        logger.warning(f"Native video upload failed for @{handle} ({url}): {e}")
        # Fallback: at least send the link so the alert is not lost.
        try:
            bot.send_message(
                chat_id=config.TIKTOK_CHAT_ID,
                text=f"🎵 New TikTok from @{handle} (video too large/failed to upload):\n{url}",
                message_thread_id=config.TIKTOK_THREAD_ID,
            )
            return True
        except Exception as e2:
            logger.error(f"Fallback link message also failed for @{handle}: {e2}")
            return False


# --- Account onboarding ---
def seed_account_baseline(handle: str) -> int:
    """Marks a newly added creator's current videos as 'seen' so the bot only alerts on
    videos posted AFTER the account was added (no backlog dump). Returns count seeded."""
    handle = handle.lstrip('@').lower()
    vids = fetch_latest_videos(handle)
    for v in vids:
        database.mark_tiktok_video_seen(handle, v['video_id'])
    logger.info(f"Seeded {len(vids)} baseline videos as seen for @{handle}.")
    return len(vids)


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
                vid, url = v['video_id'], v['url']
                if database.is_tiktok_video_seen(handle, vid):
                    continue

                logger.info(f"New TikTok detected from @{handle}: {url}")
                tmp = tempfile.mkdtemp(prefix="tt_")
                try:
                    path, meta = download_video(url, tmp)
                    if path and os.path.exists(path):
                        _send_native_video(bot, path, meta, handle, url)
                    else:
                        # Could not download; still alert with the link.
                        bot.send_message(
                            chat_id=config.TIKTOK_CHAT_ID,
                            text=f"🎵 New TikTok from @{handle} (download failed):\n{url}",
                            message_thread_id=config.TIKTOK_THREAD_ID,
                        )
                    # Mark seen regardless, so we don't retry forever on a bad video.
                    database.mark_tiktok_video_seen(handle, vid)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
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
