# Football News Bot — User Guide

A complete guide to everything your bot can do and how to use it. No coding required —
everything is controlled from inside Telegram.

---

## 1. What the bot does

The bot automatically collects football news for **Arsenal**, **Liverpool**, and
**Inter Milan** from many sources — news websites, journalists' pages, RSS feeds,
X (Twitter) accounts, Google News — and posts clean, short updates to your Telegram
channel, each in the right club's tab. It also monitors TikTok creators and can find
new X accounts for you.

It runs 24/7 on your server and restarts itself automatically.

---

## 2. Your channel & tabs (topics)

Your group uses **Topics**, one per category:

| Tab | What goes here |
|-----|----------------|
| **Arsenal** | Arsenal news and Arsenal-related tweets |
| **Liverpool** | Liverpool news and Liverpool-related tweets |
| **Inter** | Inter Milan news and tweets |
| **General** | Tweets from mixed accounts (e.g. Fabrizio Romano) that are **not** about any of the 3 clubs — keeps the club tabs clean |
| **TikTok** | Downloaded TikTok videos with their caption + top comments |

---

## 3. How the posts look

**News articles** are summarised by AI into a few short talking points. Each post shows:

```
BREAKING UPDATE
====================
<the update>
====================
Source URL: <link to the original article>
```

- **📋 Copy block** — every post has a "📋 Copy:" block underneath. Tap its copy icon to
  copy the **full** post text to your clipboard for reposting (e.g. on X) — no length
  limit, nothing left out. (Short posts also get a quick one-tap Copy button.)
- **Tweets** are posted as-is (not AI-summarised), so they stay in the author's words.
- **Translation** — if a tweet is not in English (e.g. an Arabic account), it is
  auto-translated to English and marked with a **🌐 Auto-translated from …** flag at the
  top, so you know it's a machine translation. (The Copy button copies the clean text
  without the flag.)

Only news from the **last 24 hours** is posted, and every item is **de-duplicated**, so
you never get the same story twice.

---

## 4. Opening the admin panel

Open a **private** chat with the bot and send **/start** or **/settings**. You'll see the
admin menu. (The panel only works in a private chat, not in the group.)

The menu buttons:

- 📁 Sources Manager
- 🔍 Filter Keywords
- ⚡ Run Scraper Now
- 🔑 Update X Cookies
- 👤 Switch X Account
- 🧪 Test a Source URL
- 🎵 TikTok Monitor
- 🔎 X Lead Finder

> **Tip:** In lists (like Sources), the action buttons appear at the **bottom** of the
> list, so you don't have to scroll up to add or remove something.

---

## 5. Sources Manager 📁

Add or remove where the bot gets news from.

- **➕ Add Source** → choose the type → choose the club → send the value:
  - **Web Link** — a team/section/journalist page, or any news article URL.
  - **RSS Feed** — a direct feed URL.
  - **X (Twitter) Account** — an `@handle`.
  The bot auto-detects the best setup. New sources are used on the next cycle.
- **❌ Remove Source** — tap a source to remove it.

> **Always test a new website source first** with **🧪 Test a Source URL** (see §11).

---

## 6. Filter Keywords 🔍

Add words that should make the bot **ignore** an article (e.g. a topic you never want).
Articles containing a filter keyword are skipped during AI summarisation.

---

## 7. X (Twitter) accounts

You can add any public X account as a source (Sources Manager → Add Source → X Account).
Three things make X sources powerful:

1. **Retweets are included.** Many accounts (especially the Arabic ones) mostly retweet
   rather than post their own tweets. The bot now posts the **full original tweet** they
   retweeted, and avoids duplicates (if two accounts retweet the same post, or you also
   follow the original, it's posted once).
2. **Auto-translation.** Non-English tweets are translated to English with the
   **🌐 Auto-translated** flag.
3. **Smart routing to the General tab.** For a mixed account like Fabrizio Romano, tweets
   that **are** about your club go to that club's tab; everything else goes to **General**.
   For a single-club account (e.g. an Arabic Arsenal account), all its tweets stay in the
   Arsenal tab. This keeps your club tabs focused.

> If you want an account's *unrelated* posts to stop cluttering a club tab, you don't need
> to do anything special — the General routing handles it automatically.

---

## 8. Keeping X connected 🔑 👤

The bot reads X using a logged-in session (cookies). If X posts stop and you get an alert
that the session expired:

- **🔑 Update X Cookies** — paste fresh `auth_token` and `ct0` cookies (from a browser
  logged in to x.com → DevTools → Application → Cookies).
- **👤 Switch X Account** — change to a different X account entirely (username / password /
  email, then cookies).

The bot **never posts fake data** when X is down — it just waits until the session is
restored.

---

## 9. TikTok Monitor 🎵

Watches TikTok creators and posts their new videos to the TikTok tab as **native,
autoplaying** clips, each with the **original caption + the top 3 most-liked comments**
(raw text, no AI).

From **🎵 TikTok Monitor**:

- **➕ Add Account** — send a handle or profile URL (e.g. `khaby.lame`,
  `@khaby.lame`, or the full link). The bot **immediately posts the 3 most recent videos**
  (with caption + comments), then keeps watching for new ones.
- **❌ Remove Account** — stop watching a creator.
- **🧪 Test an Account** — see which recent videos the bot can find (nothing is posted).

> Telegram bots can upload videos up to **50 MB**; anything larger is posted as a link
> instead.

---

## 10. X Lead Finder 🔎

Finds new football X accounts for you — great for spotting accounts to follow or buy.

1. Tap **🔎 X Lead Finder**, then choose:
   - **▶️ Run Scan (≥ 8,500)** — quick scan with the default threshold, or
   - **🔢 Custom Minimum…** — type your own follower filter: a single minimum (e.g.
     `20000`) **or a range** like `7000-20000` (also `7000 to 20000`) to only get accounts
     within that follower band.
2. A **live progress bar** shows the scan working (`▰▰▰▱▱▱ 4/12`, accounts scanned,
   qualified count). **You can leave this screen** — the bot works in the background.
3. When it finishes (a few minutes), the bot sends you an **Excel (.xlsx) file** with:
   **Account Name, Handle, Follower Count, Profile Bio, Profile Link** — only accounts
   above your follower threshold in the football niche, sorted by follower count.

You can tap and go — the file arrives automatically when it's ready.

---

## 11. Run Scraper Now ⚡ & Test a Source URL 🧪

- **⚡ Run Scraper Now** — trigger an immediate collect + post cycle instead of waiting for
  the next automatic one.
- **🧪 Test a Source URL** — a **dry run** for a website/RSS URL: it shows how the page is
  read, which articles it finds, and whether they'd be posted or skipped (and why).
  Nothing is saved or posted. **Use this before adding any new website source.**

---

## 12. Good to know

- **Timing:** news appears within a few minutes; the heavier protected sites are checked
  on a slightly slower cycle.
- **Translation quality:** Google Translate is free and good, but not perfect — that's why
  translated posts carry the 🌐 flag.
- **No duplicates, no fakes:** every item is de-duplicated, and the bot never invents
  content — if a source fails, it's simply skipped.


