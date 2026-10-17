"""Streamlit entry: login, session handling and role routing.

Everything shown is reloaded from the database on each rerun. The browser session holds only a
login token; the server checks it on every rerun (idle and absolute expiry in backend/security.py).
Authorization is enforced in backend/repository.py, so hiding a button here is never the control.
All processing is local: faster-whisper for transcription, Ollama for drafts and the assistant.
"""
import os
from pathlib import Path
import streamlit as st
from dotenv import load_dotenv
from backend import audit, db
from backend.assistant import ClinicalAssistant
from backend.llm_client import OllamaClient
from backend.pipeline import Pipeline
from backend.repository import PseudonymStore, Repository
from design.common import act
from design.i18n import clear_session, t
from design.theme import footer, inject_css, page_title, topbar
from design.editors import edited_draft, edited_transcript, fact_rows, item_rows  # noqa: F401 (re-exported for tests)

ROOT = Path(__file__).resolve().parents[1]


@st.cache_resource
def get_pipeline(data_dir: str = "") -> Pipeline:
    """One pipeline per process. VISIT_DATA_DIR (optional) moves the database and temporary audio together."""
    if data_dir:
        d = Path(data_dir)
        return Pipeline(Repository(d / "visits.sqlite"), PseudonymStore(d / "pseudonyms.sqlite"), d / "tmp_audio")
    pipe = Pipeline()
    pipe.cleanup_stale()
    return pipe


def make_client():
    """Local Ollama only (extraction and patient summaries)."""
    return OllamaClient()


def make_assistant(repo):
    """The assistant picks its own fast local model (qwen3:4b when installed) and short-answer settings."""
    return ClinicalAssistant(repo)


@st.cache_resource
def warm_assistant_model():
    """Load the assistant model in the background once per server process, so the first question is not slowed by loading."""
    import threading
    from backend.assistant import assistant_model
    threading.Thread(target=OllamaClient(model=assistant_model()).warm, daemon=True).start()
    return True


def login_screen(repo):
    page_title(t("כניסה למערכת"), t("גישה מאובטחת לצוות הרפואי ולמטופלים"))
    if not repo.has_users():
        st.warning(t("אין עדיין משתמשים. מנהל/ת המערכת הראשון/ה נוצר/ת משורת הפקודה:"))
        st.code(r"..\venv\Scripts\python.exe -m backend.manage create-admin --username <name> --display ""<full name>""", language=None)
        return
    if not repo.has_admin():
        st.info(t("עדיין אין מנהל/ת מערכת. משתמשים קיימים יכולים להיכנס; ניהול משתמשים ושיוכים דורש מנהל/ת "
                  "(python -m backend.manage create-admin)."))
    with st.form("login"):
        st.markdown('<div class="mv-login-head"><div class="mv-login-icon">'
                    '<svg viewBox="0 0 40 40" width="34" height="34"><path d="M16 8h8v8h8v8h-8v8h-8v-8H8v-8h8z" fill="#fff"/></svg>'
                    f'</div><h3>{t("התחברות לחשבון")}</h3><p>{t("הזינו את שם המשתמש והסיסמה שקיבלתם ממנהל/ת המערכת")}</p></div>',
                    unsafe_allow_html=True)
        username = st.text_input(t("שם משתמש"), key="login_username")
        password = st.text_input(t("סיסמה"), type="password", key="login_password")
        if st.form_submit_button(t("כניסה"), type="primary"):
            user = repo.authenticate(username, password)
            if not user:
                st.error(t("שם משתמש או סיסמה שגויים, או שהחשבון נעול זמנית."))
            else:
                clear_session()
                st.session_state.token = repo.create_session(user)
                st.rerun()
    st.markdown('<div class="mv-login-note">' + t("החשבונות נוצרים על ידי מנהל/ת המערכת בלבד · ההתחברות מסתיימת אחרי 15 דקות ללא פעילות "
                                                  "· חמש סיסמאות שגויות נועלות את החשבון זמנית") + '</div>', unsafe_allow_html=True)


def password_form(repo, user, forced=False):
    if forced:
        page_title(t("החלפת סיסמה זמנית"), t("לפני הכניסה הראשונה יש לבחור סיסמה אישית"))
    with st.form("change_password", clear_on_submit=True):
        if forced:
            st.warning(t("יש להחליף את הסיסמה הזמנית לפני השימוש במערכת."))
        old = st.text_input(t("סיסמה נוכחית"), type="password")
        new = st.text_input(t("סיסמה חדשה"), type="password")
        confirm = st.text_input(t("אימות סיסמה חדשה"), type="password")
        if st.form_submit_button(t("שינוי סיסמה"), type="primary" if forced else "secondary"):
            if new != confirm:
                st.error(t("הסיסמאות החדשות אינן זהות."))
            else:
                def change():
                    repo.change_password(user, old, new)  # ends every session, including this one
                    clear_session()
                act(change, ok=t("הסיסמה שונתה. היכנסו שוב עם הסיסמה החדשה."))


def account_sidebar(repo, user):
    st.sidebar.markdown(f'<div class="mv-side-title">{t("החשבון שלי")}</div>', unsafe_allow_html=True)
    if st.sidebar.button(t("יציאה"), use_container_width=True, key="logout_sidebar"):
        logout(repo, user)


def logout(repo, user):
    repo.end_session(st.session_state.get("token"), user)
    clear_session()
    st.rerun()


def logout_bar(repo, user):
    """Always-visible logout on the page itself, so it never depends on the sidebar being open (narrow or zoomed screens)."""
    with st.container(key="mv_logout_bar"):
        if st.button(t("יציאה מהמערכת"), key="logout_main", icon=":material/logout:"):
            logout(repo, user)
    with st.sidebar.expander(t("שינוי סיסמה")):
        password_form(repo, user)


def run():
    load_dotenv(ROOT / ".env", override=False)
    st.set_page_config(page_title=t("מערכת סיכום ביקורים רפואיים"), page_icon="🩺", layout="wide")
    inject_css()
    audit.new_correlation_id()  # one correlation id per rerun = per user action
    pipe = get_pipeline(os.getenv("VISIT_DATA_DIR", ""))
    token = st.session_state.get("token")
    user = pipe.repo.session_user(token) if token else None
    topbar(user)
    if flash := st.session_state.pop("flash", ""):
        st.success(flash)
    if token and not user:
        clear_session()
        st.warning(t("ההתחברות הסתיימה (חוסר פעילות או תום תוקף). היכנסו שוב."))
    if not user:
        login_screen(pipe.repo)
        footer()
        return
    if user.get("must_change_password"):
        password_form(pipe.repo, user, forced=True)
        footer()
        return
    account_sidebar(pipe.repo, user)
    logout_bar(pipe.repo, user)
    if user["role"] == "doctor" and not os.getenv("VISIT_DATA_DIR"):
        warm_assistant_model()
    if user["role"] == "admin":
        from design.admin import admin_view
        admin_view(pipe.repo, user)
    elif user["role"] == "doctor":
        from design.doctor import doctor_view
        doctor_view(pipe, user, make_client, make_assistant)
    else:
        from design.patient import patient_view
        patient_view(pipe.repo, user)
    footer()
