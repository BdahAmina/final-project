"""Admin console: accounts, roles, care assignments and the audit trail. No clinical content is reachable
from here (the repository refuses it for the admin role)."""
import pandas as pd
import streamlit as st
from design.common import act, stamp
from design.i18n import t, tf
from design.theme import page_title

ROLES = {"admin": "מנהל/ת מערכת", "doctor": "רופא/ה", "patient": "מטופל/ת"}
OUTCOMES = {"": "הכל", "success": "הצלחה", "denied": "נדחה", "failure": "כישלון"}
ACTIVE = {"active": "פעיל", "inactive": "מושבת"}


def users_tab(repo, admin):
    users = repo.all_users(admin)
    st.dataframe(pd.DataFrame([{t("שם משתמש"): u["id"], t("שם"): u["display"], t("תפקיד"): t(ROLES[u["role"]]), t("פעיל"): u["active"],
                                t("נעול"): u["locked"], t("כניסות כושלות"): u["failed_logins"],
                                t("חייב/ת להחליף סיסמה"): u["must_change_password"], t("נוצר"): stamp(u["created_at"])} for u in users]),
                 hide_index=True, width="stretch")
    with st.form("admin_new_user", clear_on_submit=True):
        st.markdown("**" + t("משתמש/ת חדש/ה") + "** — " + t("התפקיד נקבע כאן בלבד; אין הרשמה עצמית."))
        cols = st.columns(4)
        role = cols[0].selectbox(t("תפקיד"), list(ROLES), index=2, format_func=tf(ROLES))
        display = cols[1].text_input(t("שם מלא"))
        username = cols[2].text_input(t("שם משתמש (באנגלית)"))
        password = cols[3].text_input(t("סיסמה זמנית"), type="password")
        st.caption(t("המשתמש/ת יחויב/ו להחליף את הסיסמה בכניסה הראשונה. מסרו אותה באופן אישי. ודאו את זהות האדם לפני יצירת החשבון."))
        if st.form_submit_button(t("יצירת חשבון"), type="primary"):
            act(repo.create_user, admin, username, role, display, password, ok=t("החשבון נוצר."))
    ids = [u["id"] for u in users]
    with st.form("admin_change_user"):
        st.markdown("**" + t("שינוי משתמש/ת קיים/ת") + "**")
        cols = st.columns(4)
        target = cols[0].selectbox(t("משתמש/ת"), ids)
        new_role = cols[1].selectbox(t("תפקיד חדש"), list(ROLES), format_func=tf(ROLES))
        active = cols[2].selectbox(t("סטטוס"), list(ACTIVE), format_func=tf(ACTIVE))
        reset = cols[3].text_input(t("סיסמה זמנית חדשה (לאיפוס)"), type="password")
        c1, c2, c3 = st.columns(3)
        if c1.form_submit_button(t("עדכון תפקיד")):
            act(repo.set_role, admin, target, new_role, ok=t("התפקיד עודכן; כל ההתחברויות של המשתמש/ת הסתיימו."))
        if c2.form_submit_button(t("עדכון סטטוס")):
            act(repo.set_active, admin, target, active == "active", ok=t("הסטטוס עודכן."))
        if c3.form_submit_button(t("איפוס סיסמה ושחרור נעילה")):
            act(repo.reset_password, admin, target, reset, ok=t("הסיסמה אופסה; נדרשת החלפה בכניסה הבאה."))


def assignments_tab(repo, admin):
    st.caption(t("רופא/ה ניגש/ת רק לביקורים של מטופלים המשויכים אליו/ה. שיוך עם סיבה הוא גם נוהל הגישה בחירום; כל שינוי נרשם ביומן."))
    rows = repo.assignments(admin)
    st.dataframe(pd.DataFrame([{"#": a["id"], t("רופא/ה"): a["doctor_id"], t("מטופל/ת"): a["patient_id"], t("סיבה"): a["reason"],
                                t("שויך על ידי"): a["assigned_by"] or t("הסבת נתונים"), t("שויך"): stamp(a["assigned_at"]),
                                t("בוטל"): stamp(a["revoked_at"]) if a["revoked_at"] else ""} for a in rows]), hide_index=True, width="stretch")
    users = repo.all_users(admin)
    doctors = [u["id"] for u in users if u["role"] == "doctor" and u["active"]]
    patients = [u["id"] for u in users if u["role"] == "patient" and u["active"]]
    with st.form("admin_assign", clear_on_submit=True):
        cols = st.columns(3)
        d = cols[0].selectbox(t("רופא/ה"), doctors)
        p = cols[1].selectbox(t("מטופל/ת"), patients)
        reason = cols[2].text_input(t("סיבה (חובה)"))
        if st.form_submit_button(t("שיוך"), type="primary"):
            act(repo.assign, admin, d, p, reason, ok=t("השיוך נוסף."))
    live = [a["id"] for a in rows if not a["revoked_at"]]
    if live:
        with st.form("admin_revoke"):
            target = st.selectbox(t("ביטול שיוך"), live)
            if st.form_submit_button(t("ביטול")):
                act(repo.revoke_assignment, admin, target, ok=t("השיוך בוטל."))


def audit_tab(repo, admin):
    st.caption(t("יומן הוספה בלבד: לרופאים ולמטופלים אין גישה אליו, ואין במערכת פעולה לעריכה או מחיקה של אירועים. "
                 "כל אירוע משורשר בגיבוב לקודמו; בדיקת השרשרת מזהה עריכות. קובץ SQLite רגיל אינו חסין מפני מי שיש לו גישת כתיבה לקובץ."))
    with st.form("audit_filters"):
        cols = st.columns(6)
        actor = cols[0].text_input(t("משתמש/ת"))
        action = cols[1].text_input(t("פעולה (לדוגמה login)"))
        record = cols[2].text_input(t("מזהה רשומה"))
        outcome = cols[3].selectbox(t("תוצאה"), list(OUTCOMES), format_func=tf(OUTCOMES))
        since = cols[4].text_input(t("מ־(UTC, 2026-10-17)"))
        until = cols[5].text_input(t("עד (UTC)"))
        cols = st.columns(3)
        page_size = cols[0].selectbox(t("בעמוד"), [25, 50, 100], index=1)
        page = cols[1].number_input(t("עמוד"), 1, 100000, 1)
        st.form_submit_button(t("סינון"))
    filters = {"actor_id": actor.strip() or None, "action": action.strip() or None, "record_id": record.strip() or None,
               "outcome": outcome or None, "since": since.strip() or None, "until": (until.strip() + "~") if until.strip() else None}
    result = repo.audit_events(admin, page=int(page), page_size=page_size, **filters)
    st.caption(t("{total} אירועים · עמוד {page} מתוך {pages}", total=result["total"], page=result["page"],
                 pages=max(1, -(-result["total"] // result["page_size"]))))
    st.dataframe(pd.DataFrame([{k: e[k] for k in ("seq", "ts_utc", "actor_id", "actor_role", "action", "record_type", "record_id",
                                                 "record_version", "outcome", "correlation_id")} | {"details": str(e["details"])}
                               for e in result["events"]]), hide_index=True, width="stretch")
    cols = st.columns(3)
    for col, fmt in zip(cols, ("csv", "json")):
        if col.button(t("הכנת ייצוא {fmt}", fmt=fmt.upper()), key=f"audit_prep_{fmt}"):
            st.session_state[f"audit_{fmt}"] = repo.audit_export(admin, fmt, **{k: v for k, v in filters.items() if k in ("actor_id", "action", "record_id", "outcome") and v})
        if st.session_state.get(f"audit_{fmt}"):
            col.download_button(t("הורדת {fmt}", fmt=fmt.upper()), st.session_state[f"audit_{fmt}"], f"audit_events.{fmt}", key=f"audit_dl_{fmt}")
    if cols[2].button(t("בדיקת שלמות שרשרת היומן")):
        r = repo.verify_audit(admin)
        head = t("תקינה") if r["ok"] else t("נמצאה חריגה באירוע {seq}", seq=r["first_bad_seq"])
        (st.success if r["ok"] else st.error)(head + " · " + t("נבדקו {n} אירועים", n=r["checked"])
                                               + (f" · {r['problem']}" if r.get("problem") else ""))


def admin_view(repo, admin):
    page_title(t("ניהול מערכת"), t("משתמשים, הרשאות, שיוכי רופא–מטופל ויומן ביקורת · ללא גישה לתוכן קליני"))
    users, assignments, audit_log = st.tabs([t("משתמשים ותפקידים"), t("שיוכי רופא–מטופל"), t("יומן ביקורת")])
    with users:
        users_tab(repo, admin)
    with assignments:
        assignments_tab(repo, admin)
    with audit_log:
        audit_tab(repo, admin)
