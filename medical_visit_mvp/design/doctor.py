"""Doctor screens: visit workflow, patient context, doctor plan, electronic-signature approval, exports."""
import uuid
import streamlit as st
from backend import diarization, export
from backend.repository import SIGNATURE_MEANING
from backend.schemas import (CATEGORY_LABELS, ROLE_LABELS, STATUS_LABELS, ClinicianDraft, PatientContext, PatientItem,
                             PatientSummary, Transcript)
from backend.summarizer import render_clinician
from design.common import act, safe_text, stamp, utc
from design.i18n import lang, t, tf
from design.theme import chip, info_row, page_title, section
from design.editors import (FACT_FIELDS, MED_FIELDS, ROLE_SPEAKERS, cell, edited_draft, edited_transcript, fact_rows, ids,
                            item_rows, labels, segment_rows)

SPOKEN_LANGUAGES = {"he": "עברית", "ar": "ערבית", "en": "אנגלית", "auto": "זיהוי אוטומטי / שיחה מעורבת"}
VISIT_LANGUAGES = {"he": "עברית", "ar": "ערבית", "en": "אנגלית", "mixed": "שיחה מעורבת"}
SUMMARY_LANGUAGES = {"Hebrew": "עברית", "Arabic": "ערבית", "English": "אנגלית"}
UI_TO_SUMMARY = {"he": "Hebrew", "ar": "Arabic", "en": "English"}
INPUT_SOURCES = {"mic": "מיקרופון המחשב", "upload": "העלאת הקלטה", "paste": "הדבקת תמלול"}
STATE_LABELS = {"received": "התקבל", "transcribing": "בתמלול", "source_review": "בדיקת תמלול", "extracting": "בחילוץ",
                "draft": "טיוטה", "approved": "מאושר", "failed": "נכשל"}
ORIGIN_LABELS = {"auto": "תמלול אוטומטי", "edited": "נערך", "manual": "הודבק ידנית", "redacted": "לאחר השחרה"}
REVOKE_LABELS = {"transcript_changed": "התמלול נערך", "clinician_summary_changed": "טיוטת הרופא השתנתה",
                 "patient_summary_changed": "סיכום המטופל השתנה", "superseded": "אושרה גרסה חדשה יותר",
                 "plan_changed": "התוכנית השתנתה", "patient_context_changed": "נתוני המטופל השתנו"}


# ---- visit selection ------------------------------------------------------------------------------------------
def new_patient_form(repo, user, expanded):
    with st.sidebar.expander("➕ " + t("מטופל/ת חדש/ה"), expanded=expanded):
        with st.form("new_patient", clear_on_submit=True):
            display = st.text_input(t("שם מלא"))
            username = st.text_input(t("שם משתמש (באנגלית)"))
            password = st.text_input(t("סיסמה זמנית"), type="password")
            st.caption(t("המטופל/ת ישויך/תשויך אליך ויחליף/תחליף את הסיסמה בכניסה הראשונה. מסרו אותה באופן אישי."))
            if st.form_submit_button(t("הוספת מטופל/ת"), type="primary", use_container_width=True):
                act(repo.create_patient, user, username, display, password, ok=t("המטופל/ת נוסף/ה ושויך/ה אליך."))


def choose_visit(repo, user):
    patients = {p["id"]: p["display"] for p in repo.users(user, "patient")}
    new_patient_form(repo, user, expanded=not patients)
    if not patients:
        st.sidebar.info(t("אין עדיין מטופלים משויכים אליך. הוסיפו מטופל/ת חדש/ה למעלה."))
    else:
        with st.sidebar.form("new_visit", clear_on_submit=True):
            st.markdown(f'<div class="mv-side-title">{t("ביקור חדש")}</div>', unsafe_allow_html=True)
            patient = st.selectbox(t("מטופל/ת"), list(patients), format_func=lambda i: f"{patients[i]} ({i})")
            language = st.selectbox(t("שפת השיחה"), list(VISIT_LANGUAGES), format_func=tf(VISIT_LANGUAGES))
            consent = st.checkbox(t("המטופל/ת הסכים/ה להקלטה ולעיבוד השיחה"), key="consent")
            if st.form_submit_button(t("יצירת ביקור"), type="primary", use_container_width=True):
                act(lambda: st.session_state.update(visit_id=repo.create_visit(user, patient, language, consent)), ok=t("הביקור נוצר."))
    visits = {v["id"]: v for v in repo.visits(user)}
    if not visits:
        return None
    current = st.session_state.get("visit_id")
    options = list(visits)
    st.sidebar.markdown(f'<div class="mv-side-title">{t("הביקורים שלי")}</div>', unsafe_allow_html=True)
    states = {k: t(v) for k, v in STATE_LABELS.items()}  # translated now, not inside the format function
    chosen = st.sidebar.radio(t("בחירת ביקור"), label_visibility="collapsed", options=options,
                              index=options.index(current) if current in visits else 0,
                              format_func=lambda i: f"{stamp(visits[i]['created_at'])} · {patients.get(visits[i]['patient_id'], visits[i]['patient_id'])} · "
                                                    f"{states[visits[i]['state']]}")
    if chosen != current:
        for key in [k for k in st.session_state if str(k).startswith(("assistant_", "findings_"))]:
            del st.session_state[key]  # switching patients clears any assistant/redaction UI state
    st.session_state.visit_id = chosen
    return visits[chosen]


# ---- step 1: input -------------------------------------------------------------------------------------------
def input_section(pipe, user, visit, has_transcript):
    vid = visit["id"]
    with st.expander(t("1. קלט: הקלטה, העלאה או הדבקת תמלול"), expanded=not has_transcript):
        if has_transcript:
            st.warning(t("קלט חדש יוצר גרסת תמלול חדשה ומבטל טיוטות ואישור קיימים."))
        source = st.radio(t("מקור השיחה"), list(INPUT_SOURCES), format_func=tf(INPUT_SOURCES), key="input_mode", horizontal=True)
        if source == "paste":
            text = st.text_area(t('תמלול, עם "רופא:" או "מטופל:" בתחילת כל תור דיבור'), key=f"manual_{vid}", height=200)
            if st.button(t("שמירת התמלול המודבק"), type="primary", disabled=not text.strip()):
                act(pipe.manual_transcript, user, vid, text, ok=t("התמלול נשמר. בדקו אותו בשלב 2."))
            return
        st.caption(t("התמלול מתבצע במחשב זה בלבד (faster-whisper). האודיו אינו נשמר במסד הנתונים ואינו נשלח לשירות חיצוני."))
        spoken = list(SPOKEN_LANGUAGES)
        default = spoken.index(visit["language"]) if visit["language"] in spoken else spoken.index("auto")
        choice = st.selectbox(t("שפת הדיבור"), spoken, index=default, format_func=tf(SPOKEN_LANGUAGES), key=f"spoken_{vid}")
        language = None if choice == "auto" else choice
        model = st.selectbox(t("מודל מקומי"), ["small", "turbo", "large-v3"], key="asr_model",
                             help=t("small קל יותר למחשב נייד. ההורדה הראשונה דורשת אינטרנט; זמן ודיוק משתנים."))
        diarization_ok, diarization_help = diarization.ready()
        diarize = st.checkbox(t("הפרדת דוברים אוטומטית"), value=diarization_ok, disabled=not diarization_ok, key="use_diarization")
        if not diarization_ok:
            st.caption(t("הפרדת דוברים אוטומטית אינה מותקנת במחשב זה, ולכן תשייכו את הדוברים בטבלה בשלב 2."), help=diarization_help)
        epoch_key = f"epoch_{vid}"
        epoch = st.session_state.get(epoch_key, 0)
        if source == "mic":
            audio = st.audio_input(t("התחילו הקלטה ועצרו כשסיימתם"), sample_rate=16000, key=f"mic_{vid}_{epoch}")
            st.caption(t("אשרו הרשאת מיקרופון בדפדפן. עד עשר דקות בכל הקלטה."))
        else:
            audio = st.file_uploader(t("קובץ אודיו עד 24MB ועד עשר דקות"), type=["wav", "mp3", "m4a", "ogg", "flac", "webm"],
                                     key=f"upload_{vid}_{epoch}")
        if audio is None:
            return
        data = audio.getvalue()
        st.audio(data)
        if st.button(t("תמלול ההקלטה"), type="primary"):
            with st.spinner(t("מתמללים… טעינת מודל מקומי עשויה לקחת זמן בפעם הראשונה.")):
                act(pipe.transcribe, user, vid, data, getattr(audio, "name", None) or "recording.wav", language, model,
                    source="microphone" if source == "mic" else "upload", use_diarization=diarize,
                    ok=t("התמלול נשמר. בדקו אותו בשלב 2."), then=lambda: st.session_state.update({epoch_key: epoch + 1}))


# ---- step 2: transcript review and redaction ---------------------------------------------------------------------
def review_section(pipe, user, vid, row, tr: Transcript):
    section(t("בדיקת התמלול ושיוך הדוברים"), "2")
    st.caption(t("גרסה {version} · {origin} · נשמרה {date}", version=row["version"], origin=t(ORIGIN_LABELS[row["origin"]]),
                 date=stamp(row["created_at"])))
    for notice in tr.notices:
        st.warning(t(notice))
    redaction_pending = row["origin"] == "redacted" and not tr.redaction_reviewed
    speakers = sorted({s.speaker for s in tr.segments} - {"UNKNOWN"})
    options = sorted(set(speakers) | set(ROLE_SPEAKERS)) + ["UNKNOWN"]
    with st.form(f"review_{row['id']}"):
        edited = st.data_editor(segment_rows(tr, lang()), key=f"segments_{row['id']}", hide_index=True, num_rows="fixed", width="stretch",
                                disabled=["id", "start", "end", "role", "flags"], column_config={
                                    "id": t("מקטע"), "start": st.column_config.NumberColumn(t("התחלה"), format="%.1f"),
                                    "end": st.column_config.NumberColumn(t("סוף"), format="%.1f"),
                                    "speaker": st.column_config.SelectboxColumn(t("דובר"), options=options, required=True),
                                    "role": t("תפקיד"), "text": st.column_config.TextColumn(t("טקסט"), width="large"), "flags": t("סימונים")})
        st.caption(t("מחיקת כל הטקסט של מקטע מסירה אותו. SPEAKER_DOCTOR / SPEAKER_PATIENT משויכים אוטומטית לרופא / למטופל."))
        roles = {}
        if speakers:
            st.markdown("**" + t("שיוך דוברים לתפקידים") + "**")
            for column, label in zip(st.columns(min(len(speakers), 4)) * len(speakers), speakers):
                current = tr.roles.get(label) or ROLE_SPEAKERS.get(label, "unknown")
                roles[label] = column.selectbox(label, list(ROLE_LABELS), index=list(ROLE_LABELS).index(current),
                                                format_func=tf(ROLE_LABELS), key=f"role_{row['id']}_{label}")
        confirmed = st.checkbox(t("בדקתי את הטקסט ואת שיוך הדוברים לרופא ולמטופל"), value=tr.roles_confirmed, key=f"confirmed_{row['id']}")
        if redaction_pending:
            st.info(t("בדקו שכל הפרטים המזהים הוחלפו בממלאי מקום (PATIENT_1, PHONE_1…). שמות שנאמרו ללא הקשר אינם מזוהים אוטומטית; "
                      "החליפו אותם ידנית. כללי ההשחרה ירוצו שוב על כל עריכה."))
            submitted = st.form_submit_button(t("אישור בדיקת ההשחרה"))
        else:
            if row["origin"] == "redacted":
                st.caption(t("שמירת שינויים תחייב הרצת השחרה ובדיקה מחדש."))
            submitted = st.form_submit_button(t("שמירת התמלול"))
    if submitted:
        try:
            updated = edited_transcript(tr, edited, roles, confirmed)
        except ValueError as exc:
            st.error(t(str(exc)))
        else:
            if redaction_pending:
                act(pipe.confirm_redaction, user, vid, updated, row["id"], ok=t("בדיקת ההשחרה אושרה."))
            elif updated.model_dump() == tr.model_dump():
                st.info(t("לא בוצעו שינויים."))
            else:
                act(pipe.save_review, user, vid, updated, row["id"], ok=t("נשמרה גרסת תמלול חדשה. טיוטות ואישור קודמים בוטלו."))
    findings_key = f"findings_{vid}"
    if redaction_pending and st.session_state.get(findings_key) is not None:
        counts = {}
        for f in st.session_state[findings_key]:
            counts[f["type"]] = counts.get(f["type"], 0) + 1
        st.caption(t("הוחלפו: {items}", items=", ".join(f"{k} × {v}" for k, v in counts.items())) if counts else
                   t("לא נמצאו פרטים מזהים בכללים האוטומטיים. בדקו ידנית."))
    if tr.roles_confirmed and row["origin"] != "redacted":
        if st.button(t("הרצת השחרת פרטים מזהים")):
            act(lambda: st.session_state.update({findings_key: pipe.redact(user, vid)[1]}), ok=t("ההשחרה הורצה. בדקו את הטקסט ואשרו בטופס."))
    problems = pipe.ready_for_extraction(tr, row["origin"])
    if problems:
        st.warning(t("לפני חילוץ הממצאים:") + " " + " ".join(t(p) for p in problems))
    return problems


# ---- step 3: drafts (recorded visit facts) ---------------------------------------------------------------------------
def drafts_section(pipe, user, vid, problems, make_client):
    repo = pipe.repo
    section(t("טיוטות — עובדות שנאמרו בביקור"), "3", t("הסיכום כולל רק את מה שנאמר בביקור; הצעות העוזר נשמרות בנפרד בתוכנית הרופא."))
    options = list(SUMMARY_LANGUAGES)
    language = st.selectbox(t("שפת הסיכום"), options, index=options.index(UI_TO_SUMMARY[lang()]), format_func=tf(SUMMARY_LANGUAGES),
                            key=f"summary_language_{lang()}")
    st.caption(t("החילוץ והסיכום מתבצעים במודל מקומי (Ollama) במחשב זה."))
    if st.button(t("חילוץ ממצאים מהתמלול"), type="primary", disabled=bool(problems)):
        with st.spinner(t("המודל המקומי מחלץ ממצאים מהתמלול…")):
            act(pipe.extract, user, vid, make_client(), language, ok=t("טיוטת הרופא נוצרה. בדקו כל ממצא מול הציטוט."))
    clinician = repo.current_summary(user, vid, "clinician")
    if not clinician:
        if repo.latest_summary(user, vid, "clinician"):
            st.info(t("הטיוטה הקודמת נוצרה מגרסת תמלול ישנה ולכן אינה בתוקף. צרו אותה מחדש."))
        return None
    draft = ClinicianDraft(**clinician["content"])
    cats, stats = labels(CATEGORY_LABELS, lang()), labels(STATUS_LABELS, lang())
    st.subheader(t("טיוטה לרופא"))
    st.caption(t("גרסה {version} · {origin}", version=clinician["version"], origin=t("מהמודל") if clinician["origin"] == "model" else t("נערכה"))
               + (f" · {draft.model}" if draft.model else ""))
    if draft.missing:
        st.caption(t("לא נמצא בתמלול:") + " " + ", ".join(cats[c] for c in draft.missing))
    for c in draft.contradictions:
        st.warning(t("סתירה:") + " " + c)
    with st.form(f"clinician_{clinician['id']}"):
        edited = st.data_editor(fact_rows(draft, lang()), key=f"facts_{clinician['id']}_{lang()}", hide_index=True, num_rows="dynamic",
                                width="stretch", disabled=["id", "source", "issues"], column_config={
                                    "id": t("מזהה"),
                                    "category": st.column_config.SelectboxColumn(t("קטגוריה"), options=list(cats.values()), required=True),
                                    "statement": st.column_config.TextColumn(t("ממצא"), width="large"),
                                    "segment_ids": t("מקטעים"), "quote": st.column_config.TextColumn(t("ציטוט מדויק"), width="large"),
                                    "negated": st.column_config.CheckboxColumn(t("שלילה")), "uncertain": st.column_config.CheckboxColumn(t("אי־ודאות")),
                                    "treatment_status": st.column_config.SelectboxColumn(t("סטטוס טיפול"), options=list(stats.values())),
                                    **{k: t(v) for k, v in MED_FIELDS.items()}, "source": t("מקור"),
                                    "issues": st.column_config.TextColumn(t("בעיות"), width="large")})
        save = st.form_submit_button(t("שמירת טיוטת הרופא"))
    if save:
        try:
            updated = edited_draft(draft, edited, lang())
        except ValueError as exc:
            st.error(t("לא ניתן לשמור: {error}", error=t(str(exc))))
        else:
            if [f.model_dump(include=FACT_FIELDS) for f in updated.facts] == [f.model_dump(include=FACT_FIELDS) for f in draft.facts]:
                st.info(t("לא בוצעו שינויים."))
            else:
                act(pipe.save_clinician, user, vid, updated, ok=t("טיוטת הרופא נשמרה ונבדקה מחדש. האישור הקודם, אם היה, בוטל."))
    with st.expander(t("תצוגת טיוטת הרופא")):
        st.markdown(render_clinician(draft, lang()))
    if clinician["blocking"]:
        st.error(t("יש בטיוטה בעיות חוסמות (⛔). תקנו אותן לפני יצירת סיכום למטופל."))

    st.subheader(t("סיכום למטופל"))
    if st.button(t("יצירת סיכום למטופל"), type="primary", disabled=clinician["blocking"]):
        with st.spinner(t("יוצרים סיכום בשפה פשוטה…")):
            act(pipe.generate_patient, user, vid, make_client(), language, ok=t("סיכום המטופל נוצר."))
    patient = repo.current_summary(user, vid, "patient")
    if not patient:
        if repo.latest_summary(user, vid, "patient"):
            st.info(t("סיכום המטופל הקודם נוצר מגרסה ישנה של טיוטת הרופא. צרו אותו מחדש."))
        return clinician
    summary = PatientSummary(**patient["content"])
    with st.form(f"patient_{patient['id']}"):
        edited = st.data_editor(item_rows(summary, lang()), key=f"items_{patient['id']}_{lang()}", hide_index=True, num_rows="dynamic",
                                width="stretch", disabled=["category", "issues"], column_config={
                                    "text": st.column_config.TextColumn(t("משפט למטופל"), width="large"), "fact_ids": t("ממצאים (F1, F2…)"),
                                    "category": t("קטגוריה"), "issues": st.column_config.TextColumn(t("בעיות"), width="large")})
        save = st.form_submit_button(t("שמירת סיכום המטופל"))
    if save:
        items = [PatientItem(text=cell(r["text"]), fact_ids=ids(r["fact_ids"])) for r in edited.to_dict("records") if cell(r["text"])]
        if [(i.text, i.fact_ids) for i in items] == [(i.text, i.fact_ids) for i in summary.items]:
            st.info(t("לא בוצעו שינויים."))
        else:
            act(pipe.save_patient, user, vid, PatientSummary(items=items, model=summary.model),
                ok=t("סיכום המטופל נשמר ונבדק מחדש. האישור הקודם, אם היה, בוטל."))
    if patient["blocking"]:
        st.error(t("יש בסיכום המטופל בעיות חוסמות (⛔)."))
    return clinician


# ---- patient context (doctor-entered) ------------------------------------------------------------------------------
CONTEXT_OPTIONS = {
    "sex": {"unknown": "לא ידוע", "female": "נקבה", "male": "זכר", "other": "אחר"},
    "pregnancy": {"unknown": "לא ידוע", "not_pregnant": "לא בהריון (אושר)", "pregnant": "בהריון", "not_applicable": "לא רלוונטי"},
    "lactation": {"unknown": "לא ידוע", "no": "לא מניקה (אושר)", "yes": "מניקה", "not_applicable": "לא רלוונטי"},
    "allergy_status": {"unknown": "לא ידוע", "none_known": "אין אלרגיות ידועות (אושר)", "listed": "יש אלרגיות (פרטו)"},
    "medication_status": {"unknown": "לא ידוע", "none": "אין תרופות קבועות (אושר)", "listed": "נוטל/ת תרופות (פרטו)"},
    "renal": {"unknown": "לא ידוע", "normal": "תקין (אושר)", "impaired": "פגוע"},
    "hepatic": {"unknown": "לא ידוע", "normal": "תקין (אושר)", "impaired": "פגוע"},
}
CONTEXT_LABELS = {"sex": "מין", "pregnancy": "הריון", "lactation": "הנקה", "allergy_status": "אלרגיות", "medication_status": "תרופות קבועות",
                  "renal": "תפקוד כליות", "hepatic": "תפקוד כבד"}


def context_section(repo, user, vid):
    row = repo.patient_context(user, vid)
    ctx = PatientContext(**row["content"]) if row else PatientContext()
    section(t("נתוני מטופל לבדיקות בטיחות"), "", t("מוזנים ומאושרים על ידי הרופא"))
    st.caption((t("גרסה {version} · עודכן {date}", version=row["version"], date=stamp(row["created_at"])) if row
                else t("לא הוזנו נתונים: כל השדות 'לא ידוע'."))
               + " · " + t("'לא ידוע' אינו זהה ל'אין'. שינוי כאן מסמן הצעות קודמות של העוזר כלא עדכניות."))
    with st.form(f"context_{vid}_{row['id'] if row else 0}"):
        cols = st.columns(4)
        age_known = cols[0].checkbox(t("גיל ידוע"), value=ctx.age_years is not None)
        age = cols[0].number_input(t("גיל (שנים)"), 0, 120, ctx.age_years or 0, disabled=False)
        values = {}
        for n, (field, options) in enumerate(CONTEXT_OPTIONS.items()):
            col = cols[(n + 1) % 4]
            values[field] = col.selectbox(t(CONTEXT_LABELS[field]), list(options), index=list(options).index(getattr(ctx, field)),
                                          format_func=tf(options), key=f"ctx_{field}_{vid}_{row['id'] if row else 0}")
        allergies = st.text_input(t("אלרגיות (מופרדות בפסיקים)"), ", ".join(ctx.allergies))
        medications = st.text_input(t("תרופות קבועות (מופרדות בפסיקים)"), ", ".join(ctx.medications))
        conditions = st.text_input(t("מחלות רקע ידועות (מופרדות בפסיקים)"), ", ".join(ctx.conditions))
        if st.form_submit_button(t("שמירת נתוני המטופל")):
            def save():
                data = values | {"age_years": int(age) if age_known else None, "conditions": ids(conditions),
                                 "allergies": ids(allergies) if values["allergy_status"] == "listed" else [],
                                 "medications": ids(medications) if values["medication_status"] == "listed" else []}
                new = PatientContext(**data)
                if new == ctx and row:
                    raise ValueError("לא בוצעו שינויים.")
                repo.add_patient_context(user, vid, new.model_dump())
            act(save, ok=t("נתוני המטופל נשמרו כגרסה חדשה."))


# ---- doctor plan (after the visit) -------------------------------------------------------------------------------------
def plan_section(repo, user, vid):
    section(t("תוכנית הרופא לאחר הביקור"))
    st.caption(t("נפרדת מהעובדות שנאמרו בביקור. פריטים מהעוזר הקליני נכנסים לכאן רק לאחר קבלה או עריכה שלך, עם תיעוד המקור."))
    plan = repo.current_plan(user, vid)
    items = plan["content"]["items"] if plan else []
    stale = set(plan["stale_items"]) if plan else set()
    if stale:
        st.warning(t("{n} פריטים נוצרו לפני שינוי בנתוני הביקור או המטופל. בדקו אותם מחדש ואשרו את ההערכה לפני החתימה.", n=len(stale)))
    for item in items:
        with st.container(border=True):
            st.markdown(("⚠️ **" + t("לא עדכני") + "** · " if item["id"] in stale else "") + safe_text(item["text"]))
            if item.get("source") == "assistant_suggestion":
                st.caption(t("מקור: הצעת העוזר #{id}", id=item["suggestion_id"])
                           + (" · " + t("נערכה על ידי הרופא") if item.get("edited") else " · " + t("התקבלה כלשונה"))
                           + (" · " + t("נוסח מקורי: {text}", text=item["original_text"]) if item.get("edited") else "")
                           + " · " + t("מקורות:") + " " + ", ".join(c["passage_id"] for c in item.get("citations", [])))
            else:
                st.caption(t("נכתב על ידי הרופא"))
            if st.button(t("הסרה מהתוכנית"), key=f"plan_remove_{plan['id']}_{item['id']}"):
                act(repo.save_plan, user, vid, [i for i in items if i["id"] != item["id"]], plan["id"], ok=t("הפריט הוסר (גרסת תוכנית חדשה)."))
    with st.form(f"plan_add_{plan['id'] if plan else 0}", clear_on_submit=True):
        text = st.text_input(t("הוספת פריט משלך לתוכנית"))
        if st.form_submit_button(t("הוספה")):
            new = {"id": f"P{uuid.uuid4().hex[:8]}", "text": text.strip(), "source": "doctor", "citations": []}
            if text.strip():
                act(repo.save_plan, user, vid, items + [new], plan["id"] if plan else None, ok=t("הפריט נוסף."))
    if stale and st.button(t("בדקתי מחדש את כל פריטי התוכנית מול הנתונים העדכניים")):
        act(repo.save_plan, user, vid, items, plan["id"], reason="reassessed", ok=t("ההערכה מחדש נרשמה (גרסת תוכנית חדשה)."))


# ---- step 4: electronic signature and export ------------------------------------------------------------------------
def downloads(repo, user, approved, include_clinician, key):
    name = f"visit_{approved['visit_id']}"
    cols = st.columns(3)
    if cols[0].download_button(t("הורדה כטקסט"), export.to_text(approved, include_clinician, lang()), f"{name}.txt", "text/plain", key=f"txt_{key}"):
        repo.log_export(user, approved["visit_id"], approved["approval_id"], "txt")
    try:
        pdf = export.to_pdf(approved, include_clinician, lang())
    except (ValueError, ImportError) as exc:
        cols[1].caption(t("ייצוא PDF אינו זמין: {error}", error=t(str(exc))))
    else:
        if cols[1].download_button(t("הורדה כ־PDF"), pdf, f"{name}.pdf", "application/pdf", key=f"pdf_{key}"):
            repo.log_export(user, approved["visit_id"], approved["approval_id"], "pdf")
    if cols[2].button(t("הכנת JSON מלא (כל הגרסאות)"), key=f"json_prep_{key}"):
        st.session_state[f"json_{key}"] = export.to_json(repo.record_export(user, approved["visit_id"]))
    if st.session_state.get(f"json_{key}"):
        cols[2].download_button(t("הורדת JSON"), st.session_state[f"json_{key}"], f"{name}.json", "application/json", key=f"json_dl_{key}")


def approval_section(pipe, user, vid):
    repo = pipe.repo
    section(t("חתימה אלקטרונית והורדה"), "4")
    approved = repo.approved(user, vid)
    if approved:
        sig = approved["signature"]
        st.success(t("נחתם על ידי {name} · {time} UTC · SHA-256 {hash}…", name=sig["signer_name"] or sig["signer_id"],
                     time=utc(sig["signed_at"]), hash=approved["content_sha256"][:16]))
        st.caption(t("משמעות: {meaning} · כל עריכה תבטל את האישור.",
                     meaning=t(sig["meaning"]) if sig["meaning"] else t("אישור מגרסה קודמת (ללא רכיבי חתימה)")))
        if sig.get("algorithm"):
            st.caption(t("חתימה קריפטוגרפית {alg} אומתה · מפתח {key}… · אימות דו־שלבי: {mfa} · מקור זמן: {source}",
                         alg=sig["algorithm"], key=sig["key_fingerprint"][:16], mfa=t("כן") if sig["mfa"] else t("לא"),
                         source=sig["time_source"] or "—"))
        downloads(repo, user, approved, True, f"doctor_{approved['approval_id']}")
        return
    history = repo.approvals(user, vid)
    if history and history[0]["revoked_at"]:
        reason = history[0]["revoked_reason"]
        st.warning(t("האישור מ־{date} בוטל: {reason}. נדרשת חתימה מחדש.", date=stamp(history[0]["approved_at"]),
                     reason=t(REVOKE_LABELS[reason]) if reason in REVOKE_LABELS else reason))
    package = pipe.review_package(user, vid)
    if not package:
        st.info(t("החתימה תתאפשר אחרי ששתי הטיוטות (רופא ומטופל) תואמות לגרסאות הנוכחיות."))
        return
    c, p, plan = package["clinician"], package["patient"], package["plan"]
    with st.expander(t("מה ייחתם"), expanded=True):
        st.markdown(render_clinician(ClinicianDraft(**c["content"]), lang()))
        st.markdown("### " + t("סיכום למטופל") + "\n" + "\n".join(export.patient_lines(PatientSummary(**p["content"]), lang())))
        if plan:
            st.markdown("### " + t("תוכנית הרופא לאחר הביקור") + "\n" + "\n".join("- " + safe_text(i["text"]) for i in plan["content"]["items"]))
        st.caption(t("גרסאות: סיכום רופא {c}, סיכום מטופל {p}", c=c["version"], p=p["version"])
                   + (", " + t("תוכנית {v}", v=plan["version"]) if plan else "") + f" · SHA-256 {package['sha256'][:16]}…")
    blocking = c["blocking"] or p["blocking"] or bool(plan and plan["stale_items"])
    if blocking:
        st.error(t("לא ניתן לחתום: יש בעיות חוסמות או פריטי תוכנית שלא הוערכו מחדש."))
    with st.form(f"sign_{c['id']}_{p['id']}_{plan['id'] if plan else 0}", clear_on_submit=True):
        st.markdown("**" + t("חתימה אלקטרונית") + "** · " + t("משמעות:") + " " + t(SIGNATURE_MEANING))
        st.caption(t("החתימה מחייבת הזנה מחדש של שם המשתמש, הסיסמה וקוד האימות שלך. היא נחתמת קריפטוגרפית (Ed25519) ונרשמת עם שמך המלא, "
                     "מועד UTC מאומת ותוכן הגרסאות החתומות."))
        reviewed = st.checkbox(t("בדקתי את הסיכומים והתוכנית מול התמלול"), key=f"approve_check_{c['id']}_{p['id']}")
        username = st.text_input(t("שם משתמש"), key=f"sign_user_{c['id']}_{p['id']}")
        password = st.text_input(t("סיסמה"), type="password", key=f"sign_pass_{c['id']}_{p['id']}")
        code = st.text_input(t("קוד אימות (6 ספרות מהאפליקציה)"), max_chars=6, key=f"sign_code_{c['id']}_{p['id']}")
        if st.form_submit_button(t("חתימה ואישור"), type="primary", disabled=blocking):
            if not reviewed:
                st.error(t("יש לסמן שבדקת את התוכן לפני החתימה."))
            else:
                act(pipe.approve, user, vid, c["id"], p["id"], package["sha256"], username, password, plan["id"] if plan else None,
                    code.strip() or None, ok=t("המסמך נחתם ואושר."))


def session_section(pipe, user, vid):
    with st.expander(t("סיום הפעלה")):
        st.caption(t("מוחק אודיו זמני ואת מיפוי ממלאי המקום לפרטים המקוריים של ביקור זה. התמלול והטיוטות נשמרים. לא ניתן לבטל."))
        if st.button(t("מחיקת אודיו זמני ומיפוי פרטים מזהים")):
            act(pipe.end_session, user, vid, ok=t("האודיו הזמני ומיפוי הפרטים נמחקו."))


def doctor_view(pipe, user, make_client, make_assistant):
    from design.assistant_panel import assistant_launcher
    import html as _html
    page_title(t("סביבת עבודה קלינית"), t("תיעוד ביקור, בדיקת טיוטות, עוזר קליני וחתימה"))
    visit = choose_visit(pipe.repo, user)
    assistant_launcher(pipe.repo, user, make_assistant, visit["id"] if visit else None)
    if not visit:
        st.info(t("צרו ביקור חדש בסרגל הצד כדי להתחיל."))
        return
    vid = visit["id"]
    names = {p["id"]: p["display"] for p in pipe.repo.users(user, "patient")}
    tone = {"approved": "ok", "failed": "danger", "draft": "info", "source_review": "warn"}.get(visit["state"], "neutral")
    info_row([(t("מטופל/ת"), _html.escape(names.get(visit["patient_id"], visit["patient_id"]))),
              (t("נפתח"), stamp(visit["created_at"])),
              (t("מצב הביקור"), chip(t(STATE_LABELS[visit["state"]]) + (f" ({visit['error_category']})" if visit["error_category"] else ""), tone)),
              (t("מזהה ביקור"), f'<code>{vid}</code>')])
    workflow, context_tab, sign_tab = st.tabs([t("תמלול וטיוטות"), t("נתוני מטופל לבטיחות"), t("תוכנית, חתימה והורדה")])
    row, transcript = pipe.transcript(user, vid)
    with workflow:
        input_section(pipe, user, visit, bool(row))
        if row:
            problems = review_section(pipe, user, vid, row, transcript)
            drafts_section(pipe, user, vid, problems, make_client)
    with context_tab:
        context_section(pipe.repo, user, vid)
        st.caption(t("העוזר הקליני לרופא נפתח מהכפתור 'עוזר קליני' בפינת המסך, לכל מטופל/ת וביקור שלך."))
    with sign_tab:
        plan_section(pipe.repo, user, vid)
        if row:
            approval_section(pipe, user, vid)
    session_section(pipe, user, vid)
