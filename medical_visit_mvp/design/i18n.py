"""The interface language of this browser session (kept in the URL as ?lang=he|ar|en, so it survives login,
logout and refresh) and the `t` shortcut used by every screen."""
import streamlit as st
from backend.i18n import LANGS, direction, normalize_lang, translate


def lang() -> str:
    if "ui_lang" not in st.session_state:
        st.session_state["ui_lang"] = normalize_lang(st.query_params.get("lang"))
    return st.session_state["ui_lang"]


def set_lang(value: str):
    st.session_state["ui_lang"] = normalize_lang(value)
    st.query_params["lang"] = st.session_state["ui_lang"]


def t(text: str, **values) -> str:
    return translate(text, lang(), **values)


def tf(mapping: dict):
    """format_func for a selectbox over a {key: Hebrew label} mapping (language fixed when the widget is built)."""
    current = lang()
    return lambda key: translate(mapping[key], current) if mapping.get(key) else str(key)


def dir_() -> str:
    return direction(lang())


def clear_session():
    """st.session_state.clear() that keeps the interface language."""
    keep = lang()
    st.session_state.clear()
    st.session_state["ui_lang"] = keep


def language_switch():
    """Compact language selector shown in the top bar on every screen."""
    with st.container(key="mv_lang_switch"):
        choice = st.segmented_control("שפה / اللغة / Language", list(LANGS), format_func=LANGS.get, default=lang(),
                                      key="ui_lang_select", label_visibility="collapsed", selection_mode="single")
    if choice and choice != lang():
        set_lang(choice)
        st.rerun()


__all__ = ["LANGS", "lang", "set_lang", "t", "tf", "dir_", "clear_session", "language_switch"]
