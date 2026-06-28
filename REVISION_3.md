# رویژن ۳ — محدودسازی استخراج لینک به کانتینر فید مقالات

## مشکل

تابع `extract_articles_from_author_page` در `scraper.py` که برای صفحات نویسنده/موضوع
سایت‌های بزرگ ورزشی (skysports، mirror، liverpoolecho و ...) استفاده می‌شود، با
`soup.find_all('a')` **کل صفحه** را اسکن می‌کرد. در این پورتال‌های بزرگ، هدر، فوتر، نوار
ناوبری و ویجت‌های «most-read / trending / related» پر از لینک به مقالات نامرتبط در سراسر
سایت هستند. در نتیجه crawler به‌جای فقط مقالات فهرست‌شده در URL خاصِ نویسنده/موضوع که کلاینت
داده، انبوهی از URLهای نامرتبط را وارد صف اسکرپ می‌کرد.

## راه‌حل اعمال‌شده (`scraper.py`)

استخراج لینک اکنون **به کانتینرِ فیدِ مقالات محدود** شده است. سه بخش اضافه/بازنویسی شد:

### ۱. نقشهٔ سلکتورهای کانتینر فید به‌ازای هر دامنه — `_FEED_CONTAINER_SELECTORS`
برای هر دامنه، فهرستی از سلکتورهای CSS که به کانتینر نگه‌دارندهٔ فهرست مقالات اشاره می‌کنند.
سلکتورها به‌ترتیب امتحان می‌شوند و اولین تطبیق برنده است؛ `main` به‌عنوان عمومی‌ترین گزینهٔ
پیش از fallback نهایی. برای نمونه:
- `skysports.com` → `.news-list`, `.sdc-site-tiles`, `.page__main`, `main`
- `mirror.co.uk` / `liverpoolecho.co.uk` → `.publication-body`, `[data-component="ArticleList"]`, `main`

### ۲. حذف «chrome» صفحه — `_decompose_noise`
قبل از انتخاب کانتینر، عناصر غیرفیدی به‌صورت in-place از سند حذف می‌شوند:
- تگ‌های `header`, `footer`, `nav`, `aside`
- هر عنصری که در `class`/`id` آن کلیدواژه‌های نویزی باشد: `nav`, `menu`, `related`,
  `trending`, `most-read`, `popular`, `promo`, `sidebar`, `social`, `share`, `advert`,
  `banner` و ... .

این کار حتی در مسیر fallback هم اسکن را داخل محتوای واقعی نگه می‌دارد.

### ۳. تفکیک منطق فیلتر لینک — `_is_article_url`
منطق تشخیص «آیا این path یک مقالهٔ مشخص است یا یک هاب/تگ/صفحهٔ نویسنده» از داخل حلقه به یک
تابع کمکی مستقل منتقل شد. قواعد per-domain دقیقاً همان قبل است (Athletic/NYTimes، Times،
Telegraph/Guardian، Independent، Standard، DailyMail، The Sun، Sky Sports و fallback
مخصوص Reach plc برای mirror/liverpoolecho)، اما حالا خواناتر و قابل‌تست است.

### جریان جدید تابع اصلی
```
soup → _resolve_feed_containers(حذف نویز + انتخاب کانتینر فید)
     → فقط داخل کانتینرها: find_all('a')
     → فیلتر: همان‌دامنه بودن + رد صفحهٔ نویسنده + _is_article_url
     → حداکثر ۵ لینک یکتا
```

امضای تابع تغییر نکرده، پس فراخوان آن در `run_scraper_ingestion` (بلوک DrissionPage) دست
نخورده باقی ماند.

## اعتبارسنجی

`py_compile` بدون خطا، و یک تست عملکردی روی یک صفحهٔ شبیه‌سازی‌شدهٔ Liverpool Echo اجرا شد
که شامل: کانتینر فید واقعی + لینک‌های هدر/nav/فوتر + ویجت «most-read» + ویجت «trending» +
لینک‌های هاب (`/authors/`, `/all-about/`).

```
$ .venv/bin/python -m py_compile scraper.py   →  OK
نتیجهٔ استخراج:
  https://www.liverpoolecho.co.uk/sport/football/liverpool-fc/salah-new-deal-1001
  https://www.liverpoolecho.co.uk/sport/football/liverpool-fc/slot-presser-1002
PASS: فقط ۲ مقالهٔ واقعی فید استخراج شد؛ تمام لینک‌های chrome/widget/hub حذف شدند.
```

## نتیجه

- ✅ استخراج لینک از سراسر پورتال متوقف شد و به کانتینر فید محدود گردید.
- ✅ هدر، فوتر، nav و ویجت‌های trending/most-read/related دیگر وارد صف نمی‌شوند.
- ✅ منطق فیلتر تمیزتر و قابل‌نگه‌داری‌تر شد (تابع `_is_article_url`).

**نکته برای نگه‌داری آینده:** اگر یکی از این سایت‌ها ساختار HTML خود را عوض کند، فقط کافی است
سلکتور مربوطه در `_FEED_CONTAINER_SELECTORS` به‌روزرسانی شود؛ سیستم در نبودِ تطبیق، به `main`
و سپس کل سندِ پاک‌سازی‌شده fallback می‌کند تا چیزی از قلم نیفتد.
