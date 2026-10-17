"""Shared Streamlit helpers."""
import time
import streamlit as st
from backend.audit import utc_iso
from backend.repository import Conflict, NotFound
from design.i18n import t


def act(fn, *args, ok="", then=None, **kwargs):
    """Run an action. On error show a safe message and return; on success flash `ok` and reload from the database."""
    try:
        fn(*args, **kwargs)
    except NotFound:
        st.error(t("הרשומה לא נמצאה או שאין לך הרשאה אליה."))
        return
    except (Conflict, ValueError) as exc:  # includes LLM, audio and validation errors (messages contain no clinical text)
        st.error(t(str(exc)))  # static backend messages are in the catalog; others are shown as written
        return
    if then:
        then()
    st.session_state.flash = ok  # already translated by the caller
    st.rerun()


def stamp(t) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(t)) if t else "—"


def utc(t) -> str:
    """Readable UTC time, e.g. 16/10/2026 21:59 UTC."""
    if not t:
        return "—"
    iso = utc_iso(t)
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]} {iso[11:16]}"


def safe_text(text) -> str:
    """Escape Markdown so stored text is shown, never interpreted."""
    text = str(text or "")
    for ch in "\\`*_{}[]<>()#+-.!|":
        text = text.replace(ch, "\\" + ch)
    return text
