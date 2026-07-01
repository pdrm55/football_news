# Football News Bot

An automated Telegram bot that collects, summarizes (with Google Gemini), and
broadcasts news for three clubs — **Arsenal**, **Liverpool**, and **Inter Milan** —
to a Telegram channel/group. Sources include RSS feeds, web/journalist/team pages
(including Cloudflare-protected sites via a headless browser), X (Twitter) accounts,
and Google News. A Telegram admin panel lets you manage sources and filters without
touching the code.

---

## Features

- **Multi-source ingestion:** RSS, web/author/team pages, Cloudflare-protected sites
  (headless Chromium), X/Twitter accounts, and Google News.
- **AI summaries:** long articles are condensed into punchy posts with Google Gemini.
  (Tweets are posted as-is.)
- **Per-club routing:** each club can post to its own Telegram forum topic.
- **Strict 24-hour freshness:** only articles published in the last 24h are posted.
- **Club-relevance filter:** off-club / off-topic articles are dropped (no tennis news
  in the Arsenal channel).
- **Duplicate suppression:** every item is de-duplicated by its unique URL/ID.
- **No fake data:** the bot never fabricates/simulates content — if a source can't be
  fetched it is skipped.
- **In-bot admin panel:** add/remove sources, manage keyword filters, test a source
  URL (dry run), run the scraper on demand, and rotate X cookies/accounts.
- **TikTok Monitor (no AI):** watch TikTok creators and post their new videos to a
  Telegram topic as native, autoplaying clips — for repurposing on X.

---

## How it works

Ingestion runs in **two independent loops** so breaking news is not stuck behind the
heavy Cloudflare/headless-browser scraping:

```
FAST loop (~3 min): RSS + plain web + X + Google News
  ├─ per item: 24h freshness check → club-relevance check → save (dedup by URL)
  ├─ summarize new items with Gemini  (X posts skipped — already short)
  └─ broadcast to the club's Telegram topic

SLOW loop (~20 min): Cloudflare-protected sites (headless Chromium)
  └─ ingest only; the fast loop broadcasts what it adds, and prunes old rows

TikTok loop (~10 min): monitored creators  →  download  →  native autoplay video
```

---

## Prerequisites

- **Python 3.13** (3.11+ should work).
- **Google Chrome / Chromium** installed on the host — required for scraping
  Cloudflare-protected sites (Standard, Telegraph, Mirror, Sky Sports, etc.) via
  DrissionPage. Without Chrome, only RSS / plain web / X sources work.
- **ffmpeg** installed on the host — required by the TikTok Monitor (yt-dlp) to mux/recode
  downloaded videos. On Debian/Ubuntu: `sudo apt install ffmpeg`.
- A **Telegram bot token**, a **target chat/channel**, your **admin user ID**, and a
  **Google Gemini API key**. X/Twitter cookies are optional (needed only for X sources).

---

## Project structure

| File | Role |
|------|------|
| `bot.py` | Telegram bot, admin panel, fast/slow schedulers, broadcasting |
| `scraper.py` | Source ingestion (RSS/web/Cloudflare/X/Google News) + Gemini summarizer |
| `tiktok_monitor.py` | TikTok Monitor module (watch creators, download, post native video) |
| `database.py` | SQLite layer (sources, filters, articles, TikTok) — auto-created on first run |
| `config.py` | Loads environment variables and tunable constants |
| `team_keywords.json` | Per-club keyword lists used for relevance detection |
| `.env.template` | Template for required/optional environment variables |
| `requirements.txt` | Python dependencies |

---

## Setup

```bash
# 1. Create a virtual environment and install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Create your .env from the template and fill in the values (see below)
cp .env.template .env

# 3. Run
python bot.py
```

On startup the bot runs pre-flight checks (Telegram token, chat access, Gemini API,
X session) and prints the result. The SQLite database (`football_news.db`) is created
automatically.

---

## Getting the credentials

**`TELEGRAM_BOT_TOKEN`** — In Telegram, talk to [@BotFather](https://t.me/BotFather),
send `/newbot`, follow the prompts, and copy the token.

**`TELEGRAM_CHAT_ID`** — Add your bot to the target group/channel as an **admin**. To
find the chat ID, add [@username_to_id_bot](https://t.me/username_to_id_bot) (or similar)
to the group, or forward a message from the group to [@JsonDumpBot](https://t.me/JsonDumpBot).
Supergroup/channel IDs look like `-100xxxxxxxxxx`.

**`ADMIN_USER_ID`** — Your own numeric Telegram user ID (e.g. from
[@userinfobot](https://t.me/userinfobot)). Multiple admins can be comma-separated.

**`GEMINI_API_KEY`** — Create a key at
[Google AI Studio](https://aistudio.google.com/app/apikey).

**X / Twitter cookies (optional)** — Needed only if you add X account sources. Log in to
x.com in a browser, open DevTools → Application → Cookies, and copy the `auth_token` and
`ct0` values. Register them through the bot: `/settings` → **🔑 Update X Cookies**.
(Alternatively set `X_USERNAME` / `X_PASSWORD` / `X_EMAIL` in `.env` to log in directly.)

---

## Environment variables (`.env`)

**Required**

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Target group/channel ID (e.g. `-100xxxxxxxxxx`) |
| `ADMIN_USER_ID` | Your numeric Telegram user ID (comma-separated for multiple admins) |
| `GEMINI_API_KEY` | Google Gemini API key |

**Optional**

| Variable | Description |
|----------|-------------|
| `GEMINI_MODEL` | Defaults to `gemini-2.0-flash`. Other options: `gemini-2.5-flash`, `gemini-2.5-pro` |
| `THREAD_ID_ARSENAL` / `THREAD_ID_LIVERPOOL` / `THREAD_ID_INTER` | Forum topic IDs (leave blank to post to the main chat) |
| `X_USERNAME` / `X_PASSWORD` / `X_EMAIL` | X/Twitter login (only if not using cookies) |
| `PROXY_URL` | HTTP/SOCKS5 proxy for X, e.g. `socks5://12.34.56.78:1080` |
| `TIKTOK_CHAT_ID` | Where TikTok videos are posted (blank = main `TELEGRAM_CHAT_ID`) |
| `TIKTOK_THREAD_ID` | Thread/topic ID of a "TikTok" forum topic in the group (optional) |

### Telegram forum topics (optional)

If your group has **Topics** enabled, create one topic per club and set the
`THREAD_ID_*` values so each club posts to its own topic. Leave them blank to broadcast
everything to the main chat. (If a topic ID is wrong, the bot automatically falls back
to the main chat.)

---

## Tunable constants (`config.py`)

| Constant | Default | Meaning |
|----------|---------|---------|
| `FAST_CYCLE_SECONDS` | `180` | Sleep between fast cycles (RSS/web/X/Google News) |
| `PROTECTED_CYCLE_SECONDS` | `1200` | Sleep between slow cycles (Cloudflare/DrissionPage) |
| `MAX_BATCH_SIZE` | `30` | Max pending articles processed per cycle |
| `MAX_BACKLOG` | `15` | Max backlog broadcast per cycle (anti-flood) |
| `DB_RETENTION_DAYS` | `7` | Delete sent/skipped rows older than this |
| `TIKTOK_CYCLE_SECONDS` | `600` | Polling interval for the TikTok monitor loop |
| `TIKTOK_FETCH_LIMIT` | `5` | Latest videos checked per creator per cycle |
| `TIKTOK_MAX_VIDEO_MB` | `49` | Max video size uploaded natively (Telegram limit is 50 MB) |

---

## Using the bot (admin panel)

Open a **private** chat with the bot and send `/start`, then tap the **⚙️ Settings**
button (or send `/settings`). The panel offers:

- **📁 Sources Manager** — add or remove sources.
  - **➕ Add Source** → choose type → choose club → send the value:
    - **Web Link** — a team/section/author page or any web article URL
    - **RSS Feed** — a direct feed URL
    - **X (Twitter) Account** — an `@handle`
    The bot auto-detects the best configuration. New sources are used on the next cycle.
- **🔍 Filter Keywords** — add/remove keywords (informational filtering).
- **⚡ Run Scraper Now** — run an ingestion + broadcast cycle immediately.
- **🧪 Test a Source URL** — *dry run* a URL (nothing is saved/posted). The report shows
  how the page is accessed, which article links are found, and whether sample articles
  would be saved or skipped (with reasons). **Always test a new source before adding it.**
- **🔑 Update X Cookies** — register fresh `auth_token` / `ct0` cookies.
- **👤 Switch X Account** — change the X username/password/email and cookies.
- **🎵 TikTok Monitor** — manage monitored TikTok creators (see below).

---

## TikTok Monitor

A standalone module (separate from the news pipeline, **no AI**) that watches TikTok
creators and, when they post a new video, downloads it and posts it to Telegram as a
**native, autoplaying** video plus the original link — handy for repurposing clips.

**Setup:** create a **"TikTok" topic** in your forum group and set `TIKTOK_THREAD_ID`
to its thread ID (leave `TIKTOK_CHAT_ID` blank to use the main chat). Requires `yt-dlp`
(installed via `requirements.txt`) and `ffmpeg` on the host.

**Manage it** from `/settings → 🎵 TikTok Monitor`:
- **➕ Add Account** — send a creator handle (e.g. `khaby.lame`). Only videos posted
  *after* you add it are alerted (the current backlog is silently marked as seen).
- **❌ Remove Account** / list monitored accounts.
- **🧪 Test an Account** — dry-run a handle to see which recent videos the bot can find
  (nothing is downloaded or posted).

**Notes:**
- Telegram bots can upload up to **50 MB**; larger videos fall back to posting the link.
  For unlimited size, run a self-hosted Telegram Local Bot API server (2 GB).
- TikTok actively blocks automated access; if listing/downloads start failing, the
  fetch layer in `tiktok_monitor.py` can be swapped for a paid TikTok API.

---

## Running in production

The bot is a long-running process. Keep it alive with `nohup`, `screen`/`tmux`, or a
**systemd** service.

Quick background run:

```bash
nohup .venv/bin/python bot.py > bot_run.out 2>&1 &
```

Example systemd unit (`/etc/systemd/system/football-news.service`):

```ini
[Unit]
Description=Football News Telegram Bot
After=network-online.target

[Service]
WorkingDirectory=/path/to/football_news
ExecStart=/path/to/football_news/.venv/bin/python bot.py
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now football-news
```

---

## X/Twitter dependency note (twifork ↔ twikit)

The code imports `from twikit import Client`, but `requirements.txt` installs
**`twifork`** (a maintained fork) which ships under the same `twikit` package name:

```
twifork @ git+https://github.com/PawiX25/twifork.git@1dfb33ea...
```

This coupling is intentional. Install **only** from `requirements.txt`; do not also
install the official `twikit` package, as the two can conflict.

---

## Troubleshooting

- **No X posts / "X live ingestion disabled":** the X session is missing or expired.
  Update cookies via `/settings → 🔑 Update X Cookies`. The admin also receives an alert
  when the session goes down. The bot never posts fake data when X is unavailable.
- **Cloudflare sites return nothing:** make sure Google Chrome/Chromium is installed and
  reachable on the host (DrissionPage launches it headless).
- **A source shows 0 articles in the Test tool:** the site may be JavaScript-heavy or its
  layout changed; the per-domain extraction rule may need tuning.
- **`database is locked`:** rare; the DB uses WAL mode. Avoid running two bot instances
  against the same database file.

---

## Notes

- The database (`football_news.db`), real `.env`, and `cookies.json` are **not** included
  in this package and must be provided/created at deploy time.
- All admin-panel messages are in English.
