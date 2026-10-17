"""The doctor-only panel "עוזר קליני לרופא": a floating button opens a side drawer where the doctor picks a patient and
one of that patient's visits, then asks questions or runs quick actions.

Answers are produced in the interface language (Hebrew, Arabic or English) and each answer is shown in its own
direction, so an Arabic or Hebrew answer reads right-to-left and an English one left-to-right, whatever the page.
Every answer is a draft for doctor review, kept apart from the recorded visit facts; suggestions enter the doctor's
plan only by an explicit accept/edit decision (Repository.decide_suggestion, audited). Conversations are stored per
visit by the backend, and switching patient or visit clears this panel's UI state, so nothing carries over.
"""
import html
import streamlit as st
from backend.assistant_actions import ACTIONS
from backend.i18n import direction, translate
from backend.repository import Conflict, NotFound
from design.common import safe_text, stamp
from design.i18n import lang, t
from design.theme import initials

KINDS = {"next_step": "צעד הבא לשקילה", "treatment_option": "אפשרות טיפול לשקילה", "medication_option": "אפשרות תרופתית לשקילה",
         "patient_instruction": "טיוטת הנחיה למטופל/ת"}
SAFETY = {"blocked": "⛔ חסום", "withheld_missing_info": "⏸ נמנע — חסר מידע", "needs_review": "⚠️ דורש בדיקה",
          "no_flags_in_checked_sources": "✓ לא נמצאו התראות במקורות שנבדקו (אינו אישור בטיחות)"}
DECISIONS = {"accepted": "✓ התקבלה לתוכנית", "edited": "✓ נערכה והתקבלה לתוכנית", "rejected": "✗ נדחתה"}
MISSING_LABELS = {"age_years": "גיל", "allergy_status": "אלרגיות", "medication_status": "תרופות קבועות", "pregnancy": "הריון",
                  "lactation": "הנקה", "renal": "תפקוד כליות", "hepatic": "תפקוד כבד"}
STATUS_TONE = {"rejected_unrelated": ("danger", "⛔"), "clarify": ("info", "❓"), "blocked": ("warn", "⚠️"), "abstained": ("neutral", "ℹ️"),
               "answered": ("ok", "💬")}
ACTION_ICONS = {"summary": ":material/summarize:", "missing": ":material/help:", "followup": ":material/quiz:",
                "contradictions": ":material/rule:", "compare": ":material/compare_arrows:", "references": ":material/menu_book:",
                "instructions": ":material/description:"}
# (response key, heading, icon, css tone)
SECTIONS = [("facts", "עובדות ידועות", "📋", "facts"), ("missing", "מידע חסר", "❔", "missing"),
            ("questions", "שאלות והצעות", "💡", "questions"), ("uncertain", "אי־ודאויות", "⚠️", "uncertain")]


def esc(text) -> str:
    return html.escape(str(text or ""))


def section_card(title: str, icon: str, tone: str, items: list[str], raw=False) -> str:
    """One colored section card (HTML). Items are escaped unless already-escaped HTML is passed with raw=True."""
    if not items:
        return ""
    lis = "".join(f"<li>{x if raw else esc(x)}</li>" for x in items)
    return (f'<div class="mv-sec mv-sec-{tone}"><div class="mv-sec-head"><span>{icon}</span>{esc(title)}'
            f'<span class="mv-sec-count">{len(items)}</span></div><ul>{lis}</ul></div>')


def citation_line(c, L) -> str:
    when = c.get("updated_at") or c.get("effective_date") or "—"
    doc = f"{c['document_label']} · " if c.get("document_label") else ""
    return (f"{doc}{c['title'].split(' — ')[0]} · {c.get('organization') or c['publisher']} · "
            f"{translate('עודכן {date}', L, date=when)} · {translate('נשלף {date}', L, date=str(c.get('retrieved_at') or '')[:10])} · {c['jurisdiction']}")


def render_sources(sources, L, limit=6):
    for c in sources[:limit]:
        with st.container(border=True):
            st.markdown(f"**{safe_text(c['section'] or '—')}**")
            st.caption(citation_line(c, L) + f" · [{translate('קישור למקור', L)}]({c['url']})"
                       + (f" · [{translate('רשומת המאגר', L)}]({c['registry_url']})" if c.get("registry_url") else ""))
            if c.get("quote"):
                st.markdown(f"> {safe_text(c['quote'])}")
            else:
                st.caption(c["text"][:360] + ("…" if len(c["text"]) > 360 else ""))
            for note in c.get("applicability") or []:
                st.caption("⚠️ " + note)


def render_answer(repo, user, vid, msg, on_change):
    r = msg["content"]
    L = r.get("language", "he")
    T = lambda text, **kw: translate(text, L, **kw)  # noqa: E731  (the answer's own language, not the page's)
    status = msg["status"]
    tone, icon = STATUS_TONE.get(status, ("neutral", "ℹ️"))
    with st.container(key=f"assistant_msg_{msg['id']}_lang_{L}"):
        lead = []
        if r.get("answer"):
            lead.append(f'<div class="mv-answer">{esc(r["answer"])}</div>')
        if r.get("message"):
            lead.append(f'<div class="mv-note mv-note-{tone}"><span>{icon}</span>{esc(r["message"])}</div>')
        known = list(dict.fromkeys(f'{esc(f["text"])}<small>{esc(f.get("source", ""))}</small>' for f in r.get("known_facts", [])))
        if not known and status in ("abstained", "answered") and not r.get("action"):
            summary = r.get("record_summary") or {}
            known = [esc(f"{f['category']}: {f['text']}") for f in summary.get("facts", [])] + [esc(x) for x in summary.get("context_known", [])]
        cards = [section_card(T(SECTIONS[0][1]), SECTIONS[0][2], "facts", known[:12], raw=True),
                 section_card(T(SECTIONS[1][1]), SECTIONS[1][2], "missing", r.get("missing_information", [])),
                 section_card(T(SECTIONS[2][1]), SECTIONS[2][2], "questions", r.get("clarification_questions", []) + r.get("examples", [])),
                 section_card(T(SECTIONS[3][1]), SECTIONS[3][2], "uncertain", r.get("uncertainties", []))]
        for check in r.get("named_medication_checks", []):
            notes = [i["result"] for i in check["blocked"] + check["flags"]]
            if check["missing"]:
                notes.append(T("חסר לבדיקה:") + " " + ", ".join(T(MISSING_LABELS.get(m, m)) for m in check["missing"]))
            cards.append(section_card(T("בדיקת תווית — {drug}", drug=check.get("display_name") or check["medication"])
                                      + " · " + T(SAFETY.get(check["status"], check["status"])), "💊", "drug", notes or [T("אין הערות")]))
        if r.get("named_medication_checks") or r.get("medication_checks"):
            cards.append(f'<div class="mv-muted">{esc(T("בדיקת אינטראקציות בין תרופות: לא זמינה — אין במערכת מאגר אינטראקציות מאומת. מוצג רק אם התווית מזכירה את התרופה הקבועה."))}</div>')
        for cmp in r.get("comparison", []):
            items = ([f"🆕 {esc(x)}" for x in cmp["only_now"]] + [f"⏮ {esc(x)}" for x in cmp["only_before"]]
                     + [f"＝ {esc(x)}" for x in cmp["both"]] + [f"✎ {esc(x)}" for x in cmp["context_changes"]])
            cards.append(section_card(T("השוואה לביקור מ־{date}", date=stamp(cmp["created_at"])), "🔁", "compare",
                                      items or [esc(cmp["note"] or T("לא נמצאו הבדלים בעובדות המתועדות."))], raw=True))
        html_out = "".join(lead) + "".join(c for c in cards if c)
        if html_out:
            st.markdown(f'<div class="mv-msg" dir="{direction(L)}" lang="{L}">{html_out}</div>', unsafe_allow_html=True)
        suggestions(repo, user, vid, msg, L, on_change)
        sources = r.get("sources") if r.get("action") == "references" else [c for s in msg.get("suggestions", []) for c in s["citations"]]
        sources = sources or r.get("evidence_highlights", [])
        footer = st.columns(2)
        if sources:
            with footer[0].popover(f"📚 {T('מקורות')} ({len(sources)})", use_container_width=True):
                render_sources(sources, L)
        if r.get("limitations") or r.get("removed"):
            with footer[1].popover(f"ⓘ {T('מגבלות ופרטים')}", use_container_width=True):
                for lim in r.get("limitations", []):
                    st.caption("• " + lim)
                if r.get("removed"):
                    st.caption(T("הוסרו בבדיקה האוטומטית:") + " " + ", ".join(f"{x['kind']}: {x['reason']}" for x in r["removed"]))
                ctx = r.get("context", {})
                st.caption(T("תמלול גרסה {t} · טיוטת רופא גרסה {c} · נתוני מטופל גרסה {p} · מודל {m}",
                             t=ctx.get("transcript_version") or "—", c=(ctx.get("clinician_summary") or {}).get("version", "—"),
                             p=(ctx.get("patient_context") or {}).get("version", T("לא הוזנו")), m=msg.get("model") or T("ללא")))
        if msg["stale"]:
            st.warning(T("התשובה אינה עדכנית: נתוני הביקור או המטופל השתנו מאז. בקשו הערכה מחדש."), icon="⚠️")
        st.markdown(f'<div class="mv-draft-tag" dir="{direction(L)}">{esc(T("טיוטה לבדיקת הרופא · אינה אבחנה או מרשם"))}</div>',
                    unsafe_allow_html=True)


def decide(repo, user, vid, sid, decision, text=None):
    try:
        repo.decide_suggestion(user, vid, sid, decision, text)
        st.session_state["assistant_notice"] = t({"accepted": "ההצעה נוספה לתוכנית הרופא.", "edited": "הנוסח הערוך נוסף לתוכנית.",
                                                  "rejected": "ההצעה נדחתה."}[decision])
    except (NotFound, Conflict, ValueError) as exc:
        st.session_state["assistant_notice"] = "⚠️ " + t(str(exc))


def suggestions(repo, user, vid, msg, L, on_change):
    T = lambda text, **kw: translate(text, L, **kw)  # noqa: E731
    for n, s in enumerate(msg.get("suggestions", []), 1):
        with st.container(border=True, key=f"assistant_sug_{s['id']}"):
            cite = s["citations"][0] if s["citations"] else None
            notes = "".join(f'<div class="mv-sug-warn">⚠️ {esc(x)}</div>' for x in (cite.get("applicability") or [] if cite else []))
            safety = (f'<div class="mv-sug-meta">{esc(T("בדיקות בטיחות ({drug}):", drug=s["safety"]["medication"]))} '
                      f'{esc(T(SAFETY.get(s["safety"]["status"], s["safety"]["status"])))}</div>') if s.get("safety") else ""
            st.markdown(
                f'<div class="mv-sug" dir="{direction(L)}"><div class="mv-sug-kind">{n} · {esc(T(KINDS.get(s["kind"], s["kind"])))}'
                f'<span class="mv-badge">{esc(T("טיוטה"))}</span></div><div class="mv-sug-text">{esc(s["text"])}</div>'
                + (f'<div class="mv-sug-meta">{esc(s["rationale"])}</div>' if s.get("rationale") else "")
                + (f'<div class="mv-sug-meta">📄 {esc(citation_line(cite, L))}</div>' if cite else "") + safety + notes + "</div>",
                unsafe_allow_html=True)
            if s["status"] != "pending":
                st.caption(T(DECISIONS[s["status"]]) + (f" · {stamp(s['decided_at'])}" if s["decided_at"] else ""))
            elif s["stale"]:
                st.caption("⚠️ " + T("לא עדכנית — נתוני המטופל השתנו; לא ניתן לקבל אותה. בקשו הערכה מחדש."))
            else:
                with st.container(horizontal=True, gap="small", key=f"assistant_sug_actions_{s['id']}"):
                    st.button(t("הוספה לתוכנית"), key=f"assistant_accept_{s['id']}", type="primary", icon=":material/check:",
                              on_click=decide, args=(repo, user, vid, s["id"], "accepted"))
                    st.button(t("דחייה"), key=f"assistant_reject_{s['id']}", icon=":material/close:",
                              on_click=decide, args=(repo, user, vid, s["id"], "rejected"))
                    with st.popover(t("עריכה"), icon=":material/edit:"):
                        with st.form(f"assistant_edit_{s['id']}", clear_on_submit=True, border=False):
                            text = st.text_area(t("עריכה וקבלה לתוכנית"), value=s["text"], key=f"assistant_edit_text_{s['id']}")
                            if st.form_submit_button(t("שמירת הנוסח הערוך"), type="primary"):
                                decide(repo, user, vid, s["id"], "edited", text)
                                on_change()


def choose_patient_visit(repo, user, default_visit):
    """Patient first, then one of that patient's visits (this doctor's authorized visits only)."""
    patients = {p["id"]: p["display"] for p in repo.users(user, "patient")}
    visits = repo.visits(user)
    if not patients or not visits:
        st.info(t("אין עדיין מטופלים או ביקורים משויכים אליך. צרו ביקור בסרגל הצד כדי להתחיל."))
        return None, None
    by_patient = {}
    for v in visits:
        by_patient.setdefault(v["patient_id"], []).append(v)
    ids = [p for p in patients if p in by_patient]
    if not ids:
        st.info(t("למטופלים שלך אין עדיין ביקורים. צרו ביקור בסרגל הצד כדי להתחיל."))
        return None, None
    default_patient = next((v["patient_id"] for v in visits if v["id"] == default_visit), ids[0])
    cols = st.columns(2)
    pid = cols[0].selectbox(t("מטופל/ת"), ids, index=ids.index(default_patient) if default_patient in ids else 0,
                            format_func=lambda i: f"{patients[i]} ({i})", key="assistant_patient")
    options = [v["id"] for v in by_patient[pid]]  # newest first
    vid = cols[1].selectbox(t("ביקור"), options, index=options.index(default_visit) if default_visit in options else 0,
                            format_func=lambda i: next(f"{stamp(v['created_at'])} · {i}" for v in by_patient[pid] if v["id"] == i),
                            key=f"assistant_visit_{pid}")
    if st.session_state.get("assistant_bound") != (pid, vid):  # switching patient/visit: drop all per-conversation UI state
        for key in [k for k in st.session_state if str(k).startswith(("assistant_accept_", "assistant_reject_", "assistant_edit_",
                                                                       "assistant_notice", "assistant_q_"))]:
            del st.session_state[key]
        st.session_state["assistant_bound"] = (pid, vid)
    return vid, patients[pid]


def rerun_panel():
    """Rerun only the drawer when this is a drawer rerun; otherwise the whole app (the drawer reopens from session state)."""
    from streamlit.errors import StreamlitAPIException
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()


def run(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except NotFound:
        st.session_state["assistant_notice"] = "⚠️ " + t("הרשומה לא נמצאה או שאין לך הרשאה אליה.")
    except (Conflict, ValueError) as exc:
        st.session_state["assistant_notice"] = "⚠️ " + t(str(exc))


def assistant_body(repo, user, make_assistant, default_visit=None):
    """Panel content (shared by the side drawer and tests)."""
    vid, name = choose_patient_visit(repo, user, default_visit)
    if not vid:
        return
    L = lang()
    st.markdown(f'<div class="mv-agent-head"><span class="mv-agent-avatar">{esc(initials(name))}</span>'
                f'<div><b>{esc(name)}</b><small>{esc(t("ביקור"))} {esc(vid)} · {esc(t("התשובות בשפה:"))} {esc(t({"he": "עברית", "ar": "ערבית", "en": "אנגלית"}[L]))}</small></div>'
                f'<span class="mv-badge">{esc(t("לרופא בלבד"))}</span></div>', unsafe_allow_html=True)
    assistant = make_assistant(repo)
    if notice := st.session_state.pop("assistant_notice", ""):
        (st.warning if notice.startswith("⚠️") else st.success)(notice)
    with st.container(horizontal=True, gap="small", key="assistant_actions"):
        for action, label in ACTIONS.items():
            if st.button(t(label), key=f"assistant_action_{action}", icon=ACTION_ICONS[action]):
                with st.spinner(t("מכין…") if action not in ("followup", "instructions") else t("מכין במודל המקומי… (עד כחצי דקה)")):
                    run(assistant.run_action, user, vid, action, L)
                rerun_panel()
    history = repo.assistant_history(user, vid)
    thread = st.container(height=470, key=f"assistant_thread_{vid}", autoscroll=True)
    with thread:
        if not history:
            with st.chat_message("assistant"):
                st.markdown(t("שלום! בחרו פעולה מהירה או שאלו על המטופל/ת של הביקור שנבחר, למשל:") + "\n" + "\n".join(
                    "- " + t(x) for x in ("האם אקמול מתאים לה?", "מה כדאי לשאול אותה על הכאב?", "אילו אזהרות בתווית של איבופרופן רלוונטיות?")))
        for msg in history:
            if msg["author"] == "doctor":
                with st.chat_message("user"):
                    st.markdown(f'<div dir="auto">{esc(msg["content"]["question"])}</div>', unsafe_allow_html=True)
            else:
                with st.chat_message("assistant"):
                    render_answer(repo, user, vid, msg, rerun_panel)
    question = st.chat_input(t("שאלה על המטופל/ת של הביקור שנבחר…"), key=f"assistant_q_{vid}")
    if question and question.strip():
        with thread:
            with st.chat_message("user"):
                st.markdown(f'<div dir="auto">{esc(question)}</div>', unsafe_allow_html=True)
            with st.chat_message("assistant"):
                with st.spinner(t("חושב… (מודל מקומי, עד כחצי דקה)")):
                    run(assistant.ask, user, vid, question, language=L)
        rerun_panel()
    st.caption(t("לרופא בלבד. התשובות הן טיוטות לבדיקה: העוזר אינו מאבחן, אינו רושם, אינו קובע מינונים ואינו מזמין בדיקות, "
                 "ושום דבר ממנו אינו מגיע למטופל/ת ללא החלטה וחתימה שלך."))


def _closed():
    st.session_state["assistant_open"] = False


def _drawer(L):
    # The drawer opens on the side opposite the sidebar: left for right-to-left pages, right for left-to-right pages.
    @st.dialog(translate("עוזר קליני לרופא", L), width="large", position="left" if direction(L) == "rtl" else "right", on_dismiss=_closed)
    def drawer(repo, user, make_assistant, default_visit=None):
        assistant_body(repo, user, make_assistant, default_visit)
    return drawer


DRAWERS = {L: _drawer(L) for L in ("he", "ar", "en")}


def assistant_launcher(repo, user, make_assistant, default_visit=None):
    """Floating button on every doctor screen; the drawer stays open across the panel's own reruns."""
    with st.container(key="mv_agent_fab"):
        if st.button(t("עוזר קליני"), icon=":material/smart_toy:", key="assistant_launch", type="primary"):
            st.session_state["assistant_open"] = True
    if st.session_state.get("assistant_open"):
        DRAWERS[lang()](repo, user, make_assistant, default_visit)
