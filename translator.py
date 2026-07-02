"""Lightweight, free machine translation for non-English posts (mainly X/Twitter).

Uses Google Translate via `deep-translator` (no API key) with `langdetect` for language
detection. Because free machine translation is not perfect, every translated post is
prefixed with a visible flag so readers know it is auto-translated.

Fully defensive: on any detection/translation failure it returns the original text
untouched (and without a flag) so the broadcast pipeline is never broken.
"""
import logging

logger = logging.getLogger("translator")

TRANSLATION_FLAG = "🌐 Auto-translated"   # shown at the top of translated posts

# ISO code -> human name, for the flag line (falls back to the raw code otherwise).
_LANG_NAMES = {
    "ar": "Arabic", "es": "Spanish", "fr": "French", "pt": "Portuguese",
    "de": "German", "it": "Italian", "tr": "Turkish", "fa": "Persian",
    "nl": "Dutch", "ru": "Russian", "id": "Indonesian", "ja": "Japanese",
    "ko": "Korean", "zh-cn": "Chinese", "zh-tw": "Chinese", "hi": "Hindi",
    "ur": "Urdu", "el": "Greek", "he": "Hebrew", "sv": "Swedish",
}

_MAX_CHARS = 4900  # Google Translate free endpoint limit is ~5000 chars


def _detect_language(text: str) -> str:
    """Best-effort language code. Non-Latin scripts are decided by their unicode block
    (very reliable); otherwise langdetect with a confidence gate. Returns 'en' when
    unsure, so we never translate (and possibly mangle) text we aren't confident about."""
    # Fast, reliable path for Arabic / Hebrew script (the main use case here).
    arabic = sum(1 for c in text if "؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ")
    if arabic >= 3:
        return "ar"
    if sum(1 for c in text if "֐" <= c <= "׿") >= 3:
        return "he"
    try:
        from langdetect import detect_langs
        langs = detect_langs(text)
        if langs and langs[0].prob >= 0.75:
            return langs[0].lang.lower()
    except Exception as e:
        logger.debug(f"Language detection failed: {e}")
    return "en"


def translate_to_english(text: str) -> tuple[str, str]:
    """Returns (text_in_english, source_lang). If the text is already English or
    translation fails, returns the original text with the detected/'en' language."""
    if not text or not text.strip():
        return text, "en"
    lang = _detect_language(text)
    if lang in ("en", "en-us", "en-gb"):
        return text, "en"
    try:
        from deep_translator import GoogleTranslator
        translated = GoogleTranslator(source="auto", target="en").translate(text[:_MAX_CHARS])
        if translated and translated.strip():
            return translated.strip(), lang
    except Exception as e:
        logger.warning(f"Translation failed ({lang} -> en): {e}")
    return text, lang  # fall back to the original on any failure


def translate_for_broadcast(text: str) -> str:
    """Translates non-English text to English and prefixes a visible flag so readers
    know it's an auto-translation. English text (or anything that couldn't be
    translated) is returned unchanged, with no flag."""
    translated, lang = translate_to_english(text)
    if lang == "en" or translated == text:
        return text
    name = _LANG_NAMES.get(lang, lang)
    return f"{TRANSLATION_FLAG} from {name}\n\n{translated}"
