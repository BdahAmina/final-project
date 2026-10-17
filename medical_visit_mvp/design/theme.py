"""Visual frame shared by every screen: fixed top bar (centered system name, language switch), centered page titles,
chips, footer. All dynamic text is HTML-escaped before it is placed in markup. The page direction follows the
interface language: Hebrew and Arabic right-to-left, English left-to-right."""
import html
from datetime import datetime
from pathlib import Path
import streamlit as st
from design.i18n import dir_, lang, language_switch, t

SYSTEM_NAME = "מערכת סיכום ביקורים רפואיים"
SYSTEM_TAGLINE = "פורטל קליני לרופאים ולמטופלים"
ROLE_NAMES = {"admin": "מנהל/ת מערכת", "doctor": "רופא/ה", "patient": "מטופל/ת"}
MONTHS = {
    "he": ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"],
    "ar": ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
}
LOGO = ('<svg viewBox="0 0 40 40" width="34" height="34" aria-hidden="true"><circle cx="20" cy="20" r="19" fill="#ffffff" opacity=".14"/>'
        '<path d="M16 8h8v8h8v8h-8v8h-8v-8H8v-8h8z" fill="#ffffff"/><circle cx="20" cy="20" r="3.2" fill="#0f766e"/></svg>')
DIRECTION_CSS = {
    "rtl": """
.stApp, [data-testid="stDialog"] [role="dialog"] { direction: rtl; }
[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"],
[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] { transform: scaleX(-1); }
""",
    "ltr": """
.stApp, [data-testid="stDialog"] [role="dialog"] { direction: ltr; }
""",
}


def inject_css():
    css = (Path(__file__).resolve().parent / "styles.css").read_text(encoding="utf-8")
    st.markdown("<style>" + css + DIRECTION_CSS[dir_()] + "</style>", unsafe_allow_html=True)


def initials(name: str) -> str:
    parts = [p for p in (name or "").replace("״", "").replace('"', "").split() if p not in ("ד", "דר", "ד׳ר", "פרופ׳")]
    return "".join(p[0] for p in parts[:2]) or "?"


def topbar(user=None):
    now = datetime.now()
    date = f"{now.day} {MONTHS[lang()][now.month - 1]} {now.year}"
    who = ""
    if user:
        who = (f'<div class="mv-user"><span class="mv-avatar">{html.escape(initials(user["display"]))}</span>'
               f'<span class="mv-user-text"><b>{html.escape(user["display"])}</b>'
               f'<small>{html.escape(t(ROLE_NAMES.get(user["role"], "")))}</small></span></div>')
    st.markdown(
        f'<div class="mv-topbar"><div class="mv-topbar-side mv-start">{who}</div>'
        f'<div class="mv-brand">{LOGO}<div><div class="mv-brand-name">{html.escape(t(SYSTEM_NAME))}</div>'
        f'<div class="mv-brand-tag">{html.escape(t(SYSTEM_TAGLINE))}</div></div></div>'
        f'<div class="mv-topbar-side mv-end"><span class="mv-date">{date}</span></div></div>',
        unsafe_allow_html=True)
    language_switch()


def page_title(title: str, subtitle: str = ""):
    """Title and subtitle are already translated by the caller."""
    sub = f'<p class="mv-page-sub">{html.escape(subtitle)}</p>' if subtitle else ""
    st.markdown(f'<div class="mv-page-title"><h1>{html.escape(title)}</h1>{sub}</div>', unsafe_allow_html=True)


def chip(text: str, tone: str = "neutral") -> str:
    """Inline status chip (tone: neutral, ok, warn, danger, info)."""
    return f'<span class="mv-chip mv-chip-{tone}">{html.escape(str(text))}</span>'


def info_row(items: list[tuple[str, str]]):
    """A row of label/value tiles, e.g. the visit header."""
    cells = "".join(f'<div class="mv-tile"><small>{html.escape(k)}</small><div>{v}</div></div>' for k, v in items)
    st.markdown(f'<div class="mv-tiles">{cells}</div>', unsafe_allow_html=True)


def section(title: str, step: str = "", note: str = ""):
    badge = f'<span class="mv-step">{html.escape(step)}</span>' if step else ""
    sub = f'<div class="mv-section-note{" mv-indent" if step else ""}">{html.escape(note)}</div>' if note else ""
    st.markdown(f'<div class="mv-section">{badge}<h2>{html.escape(title)}</h2></div>{sub}', unsafe_allow_html=True)


def footer():
    st.markdown(f'<div class="mv-footer">{html.escape(t("אב־טיפוס לימודי שנבחן עם מטופלים בדויים · אינו מכשיר רפואי מאושר · כל העיבוד מקומי במחשב זה"))}</div>',
                unsafe_allow_html=True)
