# استراتژی بهبود جمع‌آوری اخبار (Collection Strategy)

این سند پیشنهادهای بهبود **استراتژی و تکنولوژی** برای جمع‌آوری بهتر اخبار است، بر اساس
بررسی کامل پایپلاین فعلی:

```
scheduler_loop (هر ۱۰ دقیقه)
  → run_scraper_ingestion  (RSS + web + Cloudflare/DrissionPage + X/twikit + Google News)
  → SQLite (news_articles)
  → Gemini summarizer
  → انتشار در تاپیک‌های تلگرام (Arsenal / Liverpool / Inter)
```

---

## ضعف‌های فعلی لایهٔ جمع‌آوری

1. **سریال و کند:** `run_scraper_ingestion` منابع را پشت‌سرهم پردازش می‌کند؛ با sleepهای
   DrissionPage (۵ث)، استگر X (۸–۱۵ث) و استگر نویسنده‌ها (۱۵–۲۰ث) یک سیکل می‌تواند
   طولانی‌تر از فاصلهٔ ۱۰ دقیقه‌ای شود. تأخیر انتشار خبر فوری بالاست.
2. **Polling با فاصلهٔ ثابت:** منبع Tier-1 (باشگاه رسمی، Fabrizio Romano) همان‌قدر کند چک
   می‌شود که یک aggregator کم‌اهمیت.
3. **استخراج محتوای دست‌ساز:** `parse_article_html` و `extract_article_published_date`
   مبتنی بر سلکتورهای ثابت‌اند و در هر سایت می‌شکنند.
4. **خزش Cloudflare با Chromium کامل:** سنگین، پرمصرف RAM روی VPS، شکننده و قابل‌تشخیص.
5. **dedup فقط با URL دقیق:** یک خبر واحد (انتقال یک بازیکن) از Google News + RSS + صفحهٔ
   نویسنده، چند بار پست می‌شود — بزرگ‌ترین مشکل کیفیتی فید.
6. **Google News:** لینک‌های obfuscate‌شده که اغلب resolve نمی‌شوند → fallback به snippet
   کوتاه؛ تکراری و انگلیسی‌محور.
7. **X/twikit:** ریسک بن و انقضای کوکی؛ غیررسمی و شکننده.
8. **تشخیص تیم با شمارش کلیدواژه** (`detect_team_from_text`): با `allow_fallback=True`
   عملاً همیشه تگ پیش‌فرض را برمی‌گرداند → ریسک آلودگی بین‌باشگاهی.

---

## ۱) بُردهای سریع (ROI بالا، کم‌هزینه)

### الف. استخراج محتوا با `trafilatura`
به‌جای پارسرهای دست‌ساز:
```python
import trafilatura
result = trafilatura.bare_extraction(html, with_metadata=True)
# result['text'], result['date'], result['image'], result['title']
```
- یک کتابخانه برای همهٔ سایت‌ها؛ متن تمیز + تاریخ انتشار (`htmldate`) + تصویر اصلی با دقت
  بسیار بالاتر.
- نگه‌داری selectorهای per-domain و کل `extract_article_published_date` تقریباً حذف می‌شود.
- **محل اعمال:** جایگزین `parse_article_html` و `extract_article_published_date` در
  `scraper.py`.

### ب. حذف تکراری معنایی (near-duplicate) — مهم‌ترین برد کیفیتی
ستون `content_hash` و dedup فازی:
```python
from rapidfuzz import fuzz
# نرمال‌سازی عنوان (lowercase، حذف نام منبع/علائم)
# اگر شباهت > 90 با خبرهای ۶ ساعت اخیرِ همان تیم بود ⇒ skip
```
برای دقت بیشتر: **embedding** (همان `google-genai`، مدل `text-embedding-004`) + شباهت
کسینوسی با آستانهٔ ~0.9، یا SimHash/MinHash برای راه‌حل سبک. نتیجه: هر رویداد یک‌بار پست
می‌شود.
- **محل اعمال:** قبل از `database.save_article` در `run_scraper_ingestion`.

### ج. Conditional GET برای RSS
```python
feed = feedparser.parse(url, etag=saved_etag, modified=saved_modified)
if getattr(feed, 'status', None) == 304:
    continue   # چیزی عوض نشده
```
پهنای‌باند و زمان کم می‌شود → امکان poll مکررتر بدون هزینهٔ اضافه.
- **نیازمند:** ذخیرهٔ `etag`/`modified` per-source (دو ستون در جدول `sources`).

### د. موازی‌سازی واکشی
RSS/web بدون I/O مشترک‌اند؛ با `asyncio + aiohttp` یا
`concurrent.futures.ThreadPoolExecutor` ده‌ها منبع هم‌زمان واکشی می‌شوند. زمان سیکل از
چند دقیقه به چند ثانیه می‌رسد. (DrissionPage و X را جدا و throttled نگه‌دار.)

### ه. قبل از مرورگر headless، fingerprint سبک
برای دامنه‌های Cloudflare اول `curl_cffi` (با `impersonate="chrome120"`) یا `cloudscraper`؛
فقط در صورت شکست سراغ مرورگر برو. اکثر پیلوال‌ها بدون Chromium رد می‌شوند → مصرف RAM و زمان
به‌شدت پایین می‌آید.
- **محل اعمال:** مسیر `protected_web_sources` در `run_scraper_ingestion`.

---

## ۲) سرمایه‌گذاری متوسط

### الف. Tiering منابع + Polling تطبیقی
ستون‌های `tier` و `poll_interval` به جدول `sources`:
- **Tier-1** (باشگاه رسمی، Romano، Ornstein): هر ۱–۲ دقیقه.
- **Tier-3** (aggregatorها): هر ۱۵–۳۰ دقیقه.
- تطبیقی: منبع پرفعالیت → فاصله کم‌تر؛ منبع مرده → backoff.

### ب. دروازهٔ relevance با LLM/Embedding
به‌جای `matches > 0`، یک طبقه‌بندی سبک: «این متن دربارهٔ
Arsenal/Liverpool/Inter است؟ کدام؟ یا هیچ‌کدام» با embedding ارزان یا prompt کوتاه.
آلودگی بین‌باشگاهی و clickbait حذف می‌شود.
- **محل اعمال:** جایگزین/مکمل `detect_team_from_text`.

### ج. سلامت per-source + هشدار
ستون‌های `last_success_at`, `last_item_count`, `consecutive_failures`. در `/settings`
نمایش بده و اگر منبعی N سیکل خبر صفر داشت به ادمین alert بده. الان منبع خراب بی‌صدا هیچ
نمی‌دهد.

### د. جایگزینی Google News
گزینه‌ها: **GDELT** (رایگان، خبرمحور، فیلتر دامنه/کلید)، **Bing News Search API**، یا
**NewsAPI.org**؛ یا تکیه بر مجموعهٔ RSS دستی‌چین با کیفیت بالاتر از Google News
obfuscated.

---

## ۳) سرمایه‌گذاری بزرگ‌تر (هنگام مقیاس بالا)

- **صف کار + جداسازی مراحل:** `arq`/`Celery`/`RQ` با سه worker مستقل
  *ingest → summarize → publish*. اسکجولر بلاک نمی‌شود؛ خلاصه‌سازی Gemini موازی و با
  backoff؛ مقیاس‌پذیری افقی.
- **Postgres به‌جای SQLite:** برای همزمانی نوشتن چند worker، full-text search و ایندکس
  روی `content_hash`. (SQLite فعلی با WAL برای تک‌پروسه کافی است.)
- **Push به‌جای Poll:** برای فیدهای دارای **WebSub/PubSubHubbub** یا سرویس‌هایی مثل
  Superfeedr/Inoreader API؛ تأخیر به ثانیه می‌رسد.
- **X پایدارتر:** اکانت burner + پروکسی residential (کد از `PROXY_URL` پشتیبانی می‌کند)،
  یا instanceهای Nitter، یا تیر پولی X API، یا چرخش چند اکانت برای کاهش ریسک بن.
- **خزش مدیریت‌شده:** Zyte API / ScrapingBee / Browserless برای دامنه‌های سخت، به‌جای
  نگه‌داری Chromium روی VPS.

---

## جدول اولویت

| اولویت | اقدام | اثر |
|--------|------|-----|
| 🔴 ۱ | dedup معنایی (hash/rapidfuzz/embedding) | حذف پست‌های تکراری — مهم‌ترین برد کیفیتی |
| 🔴 ۲ | `trafilatura` برای متن/تاریخ/تصویر | دقت بالاتر، حذف کد شکننده |
| 🔴 ۳ | موازی‌سازی + Conditional GET | تأخیر انتشار ↓، امکان poll مکرر |
| 🟠 ۴ | `curl_cffi`/`cloudscraper` قبل از headless | مصرف VPS ↓، پایداری ↑ |
| 🟠 ۵ | Tiering + polling تطبیقی | خبر فوری زودتر |
| 🟠 ۶ | relevance با LLM/embedding | حذف آلودگی بین‌باشگاهی/clickbait |
| 🟢 ۷ | سلامت per-source + alert | دیده‌بانی |
| 🟢 ۸ | صف کار + Postgres + push | مقیاس‌پذیری بلندمدت |

---

## نقطهٔ شروع پیشنهادی

با موارد **۱، ۲ و ۳** شروع کنید — این سه با کمترین تغییر، بیشترین اثر را روی «کیفیت + سرعت
جمع‌آوری» دارند و ریسک پایینی دارند:
1. **dedup معنایی** تا کاربر یک خبر را چند بار نبیند.
2. **trafilatura** تا کیفیت متن/تاریخ/تصویر بالا برود و کد شکننده حذف شود.
3. **موازی‌سازی + Conditional GET** تا سیکل کوتاه شود و بتوان مکررتر poll کرد.
