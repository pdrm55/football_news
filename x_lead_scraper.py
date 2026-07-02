#!/usr/bin/env python3
"""
X (Twitter) Lead Scraper — Football niche account finder.

Standalone tool (separate from the news bot) that discovers X accounts in the football
niche, filters by follower count and football keywords, and exports the results to
Excel/CSV for lead generation.

It reuses the project's existing authenticated X session (cookies.json) via twikit, so
no Selenium/Playwright is needed. Every network call is paced with human-like delays and
wrapped to handle rate limits gracefully.

USAGE
-----
    python x_lead_scraper.py                         # uses the config defaults below
    python x_lead_scraper.py --min-followers 10000   # override threshold
    python x_lead_scraper.py --max-pages 5 --output leads.csv
    python x_lead_scraper.py --queries "football news,transfer news,arsenal"

Requires: twikit (installed), a valid cookies.json in this folder, openpyxl (for .xlsx).
"""
import os
import csv
import time
import random
import asyncio
import logging
import argparse

# ---------------------------------------------------------------------------
# CONFIGURATION  (edit here, or override via command-line flags / env vars)
# ---------------------------------------------------------------------------

# Search queries sent to X user-search to DISCOVER candidate accounts.
SEARCH_QUERIES = [
    "football", "football news", "football tactics", "transfer news",
    "transfer updates", "premier league", "champions league",
    "arsenal", "liverpool", "inter milan", "manchester united", "chelsea",
]

# An account passes the niche filter if its name/bio contains ANY of these (lowercased).
FOOTBALL_KEYWORDS = [
    "football", "futbol", "soccer", "tactics", "transfer", "premier league",
    "champions league", "la liga", "serie a", "bundesliga", "fpl",
    "arsenal", "liverpool", "chelsea", "man utd", "man city", "tottenham",
    "inter", "milan", "juventus", "barcelona", "real madrid", "psg",
]

MIN_FOLLOWERS = 8500          # only keep accounts with >= this many followers
MAX_PAGES_PER_QUERY = 3       # how many pages (~20 users each) to fetch per query
DELAY_MIN_SECONDS = 4.0       # human-like delay range between requests
DELAY_MAX_SECONDS = 9.0
COOKIES_PATH = "cookies.json"
OUTPUT_PATH = "x_football_leads.xlsx"   # .xlsx or .csv
PROXY_URL = os.getenv("PROXY_URL")      # optional; reuses the project's proxy env var

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("x_lead_scraper")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def _sleep():
    """Human-like pause between requests."""
    time.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))


def build_client():
    """Creates a twikit client authenticated with the existing cookies.json session."""
    from twikit import Client
    if not os.path.exists(COOKIES_PATH):
        raise FileNotFoundError(
            f"'{COOKIES_PATH}' not found. Provide a valid X session cookie file "
            "(auth_token + ct0) in this folder."
        )
    client = Client("en-US", proxy=PROXY_URL) if PROXY_URL else Client("en-US")
    client.load_cookies(COOKIES_PATH)
    return client


def matches_football(name: str, bio: str, keywords: list[str]) -> bool:
    """True if the account name or bio contains any football keyword."""
    text = f"{name or ''} {bio or ''}".lower()
    return any(kw in text for kw in keywords)


async def _search_page(client, query, cursor_result):
    """Returns one page of user results, handling rate limits gracefully."""
    from twikit.errors import TooManyRequests
    for attempt in range(2):
        try:
            if cursor_result is None:
                return await client.search_user(query)
            return await cursor_result.next()
        except TooManyRequests as e:
            wait = getattr(e, "rate_limit_reset", None)
            cooldown = 60
            logger.warning(f"Rate limited on '{query}'. Cooling down {cooldown}s...")
            time.sleep(cooldown)
        except Exception as e:
            logger.error(f"Search error on '{query}': {e}")
            return None
    return None


async def collect_accounts(client, queries, keywords, min_followers, max_pages):
    """Discovers accounts across all queries, filters them, and returns unique rows."""
    seen_handles = set()
    results = []
    stats = {"scanned": 0, "passed": 0, "low_followers": 0, "off_niche": 0}

    for query in queries:
        logger.info(f"=== Searching X users for: '{query}' ===")
        page = await _search_page(client, query, None)
        for page_num in range(max_pages):
            if not page:
                break
            users = list(page)
            if not users:
                break
            for u in users:
                handle = (getattr(u, "screen_name", "") or "").strip()
                if not handle or handle.lower() in seen_handles:
                    continue
                seen_handles.add(handle.lower())
                stats["scanned"] += 1

                name = getattr(u, "name", "") or ""
                bio = getattr(u, "description", "") or ""
                followers = getattr(u, "followers_count", 0) or 0

                if followers < min_followers:
                    logger.info(f"Scanning @{handle}... Skipped: low follower count ({followers:,})")
                    stats["low_followers"] += 1
                    continue
                if not matches_football(name, bio, keywords):
                    logger.info(f"Scanning @{handle}... Skipped: not in football niche")
                    stats["off_niche"] += 1
                    continue

                logger.info(f"Scanning @{handle}... Passed criteria ({followers:,} followers) ✅")
                stats["passed"] += 1
                results.append({
                    "Account Name": name,
                    "Handle": f"@{handle}",
                    "Follower Count": followers,
                    "Profile Bio": bio.replace("\n", " ").strip(),
                    "Profile Link": f"https://x.com/{handle}",
                })

            _sleep()  # pace between pages
            page = await _search_page(client, query, page)

    logger.info(
        f"Done. Scanned {stats['scanned']} | Passed {stats['passed']} | "
        f"Skipped low-followers {stats['low_followers']} | Skipped off-niche {stats['off_niche']}"
    )
    # Highest-follower leads first
    results.sort(key=lambda r: r["Follower Count"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
COLUMNS = ["Account Name", "Handle", "Follower Count", "Profile Bio", "Profile Link"]


def export_results(rows, path):
    if not rows:
        logger.warning("No accounts passed the criteria — nothing to export.")
        return
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    else:
        try:
            from openpyxl import Workbook
        except ImportError:
            logger.error("openpyxl not installed; falling back to CSV.")
            return export_results(rows, os.path.splitext(path)[0] + ".csv")
        wb = Workbook()
        ws = wb.active
        ws.title = "Football Leads"
        ws.append(COLUMNS)
        for r in rows:
            ws.append([r[c] for c in COLUMNS])
        wb.save(path)
    logger.info(f"Exported {len(rows)} leads to {path}")


# ---------------------------------------------------------------------------
# Programmatic entry point (used by the Telegram bot button)
# ---------------------------------------------------------------------------
def scan_to_file(path, min_followers=MIN_FOLLOWERS, max_pages=MAX_PAGES_PER_QUERY, queries=None):
    """Runs a full scan synchronously and writes the results to `path` (.xlsx or .csv).
    Returns the number of leads found (0 if none, in which case no file is written).
    Safe to call from a background thread — it creates its own event loop."""
    client = build_client()
    rows = asyncio.run(collect_accounts(
        client, queries or SEARCH_QUERIES, FOOTBALL_KEYWORDS, min_followers, max_pages))
    export_results(rows, path)
    return len(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="X (Twitter) football-niche lead scraper.")
    p.add_argument("--min-followers", type=int, default=MIN_FOLLOWERS)
    p.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_QUERY,
                   help="pages (~20 users each) per query")
    p.add_argument("--queries", type=str, default=None,
                   help="comma-separated search queries (overrides defaults)")
    p.add_argument("--output", type=str, default=OUTPUT_PATH, help=".xlsx or .csv")
    return p.parse_args()


async def _run(args):
    queries = [q.strip() for q in args.queries.split(",")] if args.queries else SEARCH_QUERIES
    logger.info(f"Config: min_followers={args.min_followers} | max_pages={args.max_pages} "
                f"| queries={len(queries)} | output={args.output}")
    client = build_client()
    rows = await collect_accounts(client, queries, FOOTBALL_KEYWORDS,
                                  args.min_followers, args.max_pages)
    export_results(rows, args.output)


def main():
    args = parse_args()
    try:
        asyncio.run(_run(args))
    except FileNotFoundError as e:
        logger.error(str(e))
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")


if __name__ == "__main__":
    main()
