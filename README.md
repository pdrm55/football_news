# Football News Bot

رباتی برای جمع‌آوری، خلاصه‌سازی (با Google Gemini) و انتشار خودکار اخبار سه باشگاه
**Arsenal**، **Liverpool** و **Inter** در یک کانال/گروه تلگرام. منابع از طریق RSS، صفحات
وب (شامل سایت‌های محافظت‌شده با Cloudflare از طریق DrissionPage)، اکانت‌های X (Twitter) و
Google News تأمین می‌شوند. یک پنل ادمین تلگرامی هم برای مدیریت منابع و فیلترها وجود دارد.

## ساختار

| فایل | نقش |
|------|-----|
| `bot.py` | ربات تلگرام، پنل ادمین، حلقهٔ زمان‌بند پس‌زمینه و انتشار |
| `scraper.py` | جمع‌آوری از RSS/وب/X/Google News، خلاصه‌سازی با Gemini |
| `database.py` | لایهٔ SQLite (منابع، فیلترها، مقالات) |
| `config.py` | بارگذاری متغیرهای محیطی و ثابت‌های قابل‌تنظیم |

## راه‌اندازی

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.template .env   # سپس مقادیر را پر کنید
python bot.py
```

## نکتهٔ مهم دربارهٔ وابستگی X/Twitter (twifork ↔ twikit)

کد در `scraper.py` و `bot.py` از `from twikit import Client` استفاده می‌کند، اما در
`requirements.txt` بستهٔ نصب‌شده **`twifork`** است (یک fork از twikit):

```
twifork @ git+https://github.com/PawiX25/twifork.git@1dfb33ea...
```

این بسته با همان نام پکیج `twikit` نصب می‌شود؛ به همین دلیل ایمپورت `twikit` کار می‌کند.
**این جفت‌شدگی عمدی است** — اگر بستهٔ رسمی `twikit` را جداگانه نصب کنید ممکن است با
`twifork` تداخل پیدا کند. برای نصب فقط از `requirements.txt` استفاده کنید.

## متغیرهای محیطی

به `.env.template` مراجعه کنید. موارد اجباری: `TELEGRAM_BOT_TOKEN`،
`TELEGRAM_CHAT_ID`، `ADMIN_USER_ID`، `GEMINI_API_KEY`. موارد X/Twitter و پروکسی اختیاری‌اند.

## ثابت‌های قابل‌تنظیم (`config.py`)

- `SCHEDULER_CYCLE_SECONDS` — فاصلهٔ هر سیکل جمع‌آوری (پیش‌فرض ۶۰۰ ثانیه)
- `MAX_BATCH_SIZE` — حداکثر مقالات پردازش‌شده در هر سیکل
- `MAX_BACKLOG` — حداکثر بک‌لاگ پردازش‌شده هنگام شروع
- `DB_RETENTION_DAYS` — مدت نگه‌داری رکوردهای ارسال‌شده/skip‌شده در پایگاه‌داده
