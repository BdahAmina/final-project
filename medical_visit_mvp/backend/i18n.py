"""Interface languages: Hebrew (source text), Arabic and English.

Every user-facing string is written in Hebrew in the code and translated through `translate`. The catalog
(backend/locales.json) maps the exact Hebrew text, including {placeholders}, to its Arabic and English versions.
A missing entry falls back to Hebrew; tests/test_i18n.py checks that every literal passed to the translators is in
the catalog. Clinical content from the record (transcripts, facts, quotes, source passages) is never machine-
translated: it is shown as recorded.
"""
import json
from functools import lru_cache
from pathlib import Path

LANGS = {"he": "עברית", "ar": "العربية", "en": "English"}
RTL = {"he", "ar"}
MODEL_LANGUAGE = {"he": "Hebrew", "ar": "Arabic", "en": "English"}
CATALOG_PATH = Path(__file__).resolve().parent / "locales.json"


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8")) if CATALOG_PATH.exists() else {}


def translate(text: str, lang: str = "he", **values) -> str:
    out = text if lang == "he" else (catalog().get(text) or {}).get(lang) or text
    return out.format(**values) if values else out


def localize(text, lang: str = "he"):
    """Translate a system-built row such as "גיל: לא ידוע" or "רקע רפואי שנמסר: לא נאמר בביקור": the whole text if it is in
    the catalog, else label and value separately around the first ": ". Anything not in the catalog (record content,
    values typed by the doctor, model text) is returned unchanged."""
    if lang == "he" or not isinstance(text, str):
        return text
    whole = (catalog().get(text) or {}).get(lang)
    if whole:
        return whole
    if ": " in text:
        head, tail = text.split(": ", 1)
        head_t = (catalog().get(head + ":") or {}).get(lang)
        head_t = head_t[:-1] if head_t and head_t.endswith(":") else (head_t or (catalog().get(head) or {}).get(lang))
        tail_t = localize(tail, lang)
        if head_t or tail_t != tail:
            return f"{head_t or head}: {tail_t}"
    return text


SCRIPTS = {"he": r"[֐-׿]", "ar": r"[؀-ۿݐ-ݿ]", "en": r"[A-Za-z]"}


def in_language(text: str, lang: str, share: float = 0.6) -> bool:
    """True if most letters of `text` are in the alphabet of `lang` (drug names in Latin letters inside Hebrew or
    Arabic text are fine up to 40%). Used to keep a model's free text out of the answer when it ignores the language."""
    import re
    letters = re.findall(r"[^\W\d_]", text or "")
    if not letters:
        return True
    return sum(1 for ch in letters if re.match(SCRIPTS[lang], ch)) / len(letters) >= share


def direction(lang: str) -> str:
    return "rtl" if lang in RTL else "ltr"


def normalize_lang(lang) -> str:
    return lang if lang in LANGS else "he"
