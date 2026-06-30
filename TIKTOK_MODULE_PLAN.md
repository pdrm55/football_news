# Implementation Plan — TikTok Media Monitor & Downloader (Module)

A standalone module that watches specific TikTok creators and, when they post a new
video, downloads it and posts it to Telegram as a **native (autoplay) video** with a
link. **No AI / no Gemini.** Fully isolated from the news pipeline so it can never
destabilize it.

---

## 1. Scope (from client "Robin")
- **Monitor only:** alert when a watched creator posts a new video.
- **No AI:** no caption/comment summarization. Completely separate from Gemini.
- **Download:** keep the raw video file for repurposing on X.
- **Telegram autoplay:** upload the actual file natively (not a link) so it autoplays.
- **Management in-bot:** list / add / remove monitored TikTok accounts.

## 2. Design principles
- **Isolated module** (`tiktok_monitor.py`) with its **own scheduler thread** wrapped in
  try/except, its **own tables**, and its **own admin-panel section**. A TikTok failure
  must never affect the news bot.
- **Pluggable fetch layer:** the "get latest videos for a handle" function is a single
  abstraction. Start with `yt-dlp` (free); if TikTok blocks it too often, swap in a paid
  API (Apify/TikAPI/EnsembleData) without touching the rest.

## 3. Dependencies
- **`yt-dlp`** — add to `requirements.txt`. Used for BOTH listing a creator's recent
  videos and downloading them (single tool, no extra browser).
- **`ffmpeg`** — system binary, already installed on the host (verified). Needed by
  yt-dlp for muxing/recoding. Add to README prerequisites.
- Reuses existing `PROXY_URL` from config when set.

## 4. Configuration additions (`config.py` + `.env.template`)
| Variable / constant | Default | Purpose |
|---|---|---|
| `TIKTOK_CHAT_ID` | falls back to `TELEGRAM_CHAT_ID` | Where TikTok videos are posted |
| `TIKTOK_THREAD_ID` | empty | Optional forum topic for TikTok |
| `TIKTOK_CYCLE_SECONDS` | `600` | Polling interval for the TikTok loop |
| `TIKTOK_MAX_VIDEO_MB` | `49` | Max size to upload natively (Telegram bot limit is 50MB) |
| `TIKTOK_FETCH_LIMIT` | `5` | How many latest videos to check per creator per cycle |

## 5. Data model (`database.py` — new tables, created in `init_db`)
```sql
CREATE TABLE IF NOT EXISTS tiktok_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT UNIQUE NOT NULL,            -- stored without '@'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tiktok_seen_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle TEXT NOT NULL,
    video_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(handle, video_id)               -- dedup: never alert twice
);
CREATE INDEX IF NOT EXISTS idx_tt_seen ON tiktok_seen_videos(handle, video_id);
```
New DB helpers: `add_tiktok_account`, `remove_tiktok_account`, `get_tiktok_accounts`,
`is_tiktok_video_seen`, `mark_tiktok_video_seen`, `prune_tiktok_seen(days)`.

**Baseline-on-add (important UX):** when a creator is added, immediately record their
current latest video IDs as *seen* (without posting them), so the bot only alerts on
videos posted **after** they were added — no backlog dump.

## 6. Core module (`tiktok_monitor.py`)
- `fetch_latest_videos(handle, limit) -> list[{video_id, url, title}]`
  Uses `yt-dlp` flat extraction of `https://www.tiktok.com/@{handle}` (no download).
  This is the single pluggable point. Returns `[]` on failure (never fabricates).
- `download_video(url) -> path | None`
  yt-dlp download to a temp dir, MP4/H.264, format selected to stay under
  `TIKTOK_MAX_VIDEO_MB`. Returns local path or None.
- `extract_meta(path) -> {width, height, duration, thumbnail}` (from yt-dlp info).
- `run_tiktok_cycle(bot)` — for each account: fetch latest → filter unseen →
  download → send native video → mark seen → delete temp file. Per-account and
  per-video try/except so one failure doesn't stop the rest.
- `tiktok_loop(bot)` — `while True:` calls `run_tiktok_cycle`, sleeps
  `TIKTOK_CYCLE_SECONDS`, all wrapped in try/except.

## 7. Telegram autoplay + 50MB handling (in the send step)
- Send with `bot.send_video(chat_id=TIKTOK_CHAT_ID, video=<file>, supports_streaming=True,
  width=..., height=..., duration=..., caption=<creator + original link>,
  message_thread_id=TIKTOK_THREAD_ID)` → enables inline autoplay.
- **If the file > `TIKTOK_MAX_VIDEO_MB`:** try a lower-quality yt-dlp format; if still too
  big, **fall back to posting the link** (with thumbnail) and a note — never silently drop.
- Optional future upgrade: self-hosted **Telegram Local Bot API server** raises the limit
  to 2 GB (documented as a note, not built now).
- Always `os.remove()` the temp file in a `finally` block.

## 8. Admin panel additions (`bot.py`)
- New main-menu button **🎵 TikTok Monitor** in `get_main_menu_markup()`.
- New callbacks in `handle_callbacks`:
  - `tt_menu` → submenu (List / Add / Remove / Test).
  - `tt_add` → prompt for handle → `register_next_step_handler(save_tiktok_account)`.
  - `tt_del_list` / `tt_del_do_{id}` → remove (mirrors `show_remove_source_list`).
  - `tt_test` → prompt for a handle → dry-run `fetch_latest_videos` and report what it
    finds (no download/post) — same spirit as the existing "Test a Source URL".
- The list view reuses `_send_chunked_lines` (avoids the MESSAGE_TOO_LONG issue).
- **Reuse `menu_button_interrupt(message)`** at the top of `save_tiktok_account` (and the
  test prompt handler) so tapping the Settings button mid-flow can't create a bogus
  account — consistent with the existing flows.

## 9. Scheduler integration (`bot.py __main__`)
- Start a second daemon thread next to the existing one:
  ```python
  threading.Thread(target=tiktok_monitor.tiktok_loop, args=(bot,), daemon=True).start()
  ```
- The news `scheduler_loop` is untouched.

## 10. Pre-flight (optional, `run_preflight_checks`)
- Add a light check that `yt-dlp` imports and `ffmpeg` is on PATH; warn (don't hard-fail)
  if missing, so the news bot still starts.

## 11. Files touched / added
- **New:** `tiktok_monitor.py`
- **Changed:** `database.py` (tables + helpers), `config.py` (+ `.env.template`),
  `bot.py` (menu button, callbacks, next-step handlers, thread start, optional pre-flight),
  `requirements.txt` (`yt-dlp`), `README.md` (TikTok section + ffmpeg prerequisite).

## 12. Risks / honest notes (carry into delivery)
- **TikTok anti-bot is the main risk.** yt-dlp may need cookies/proxy and can break when
  TikTok changes; budget for occasional maintenance or a paid API fallback (the fetch
  layer is isolated for exactly this).
- **50MB native-upload limit** handled via format selection + link fallback.
- **Disk/bandwidth:** temp files are always cleaned up; `tiktok_seen_videos` is pruned.
- **Legal/ToS:** downloading + reposting TikTok content is the client's business decision;
  noted, not a technical blocker.

## 13. Testing
- Unit: `fetch_latest_videos` against 1–2 public handles (live), `download_video` size cap.
- The in-bot **Test** button gives the client a safe dry-run before adding an account.
- Verify a real post end-to-end: add a test creator → confirm baseline (no backlog) →
  post a new video → confirm native autoplay in Telegram + temp file cleaned.

## 14. Rollout
1. Add deps + config + DB tables (no behavior change yet).
2. Build `tiktok_monitor.py` (fetch/download/send) + isolated tests.
3. Wire the admin panel + the second scheduler thread.
4. Test end-to-end on the running bot; then commit on a feature branch + PR.

## 15. Decisions
1. **Where to post — DECIDED:** a new **"TikTok" topic in the existing forum group**.
   `TIKTOK_CHAT_ID` = current `TELEGRAM_CHAT_ID`; `TIKTOK_THREAD_ID` = the new topic's
   thread ID (client creates the topic and provides the ID, same as the club topics).
2. **Free (yt-dlp) vs paid API** to start — recommend yt-dlp first, paid only if blocking
   becomes frequent. *(pending)*
3. **Caption content** — creator handle + original TikTok link; anything else? *(pending)*
