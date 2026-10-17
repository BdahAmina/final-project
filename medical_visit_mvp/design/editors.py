"""Conversions between the Streamlit table editors and the stored versions (pure functions, unit-tested)."""
import pandas as pd
from backend.i18n import translate
from backend.schemas import CATEGORY_LABELS, ROLE_LABELS, STATUS_LABELS, ClinicianDraft, Fact, Medication, PatientSummary, Transcript

ROLE_SPEAKERS = {"SPEAKER_DOCTOR": "doctor", "SPEAKER_PATIENT": "patient"}  # same labels as parse_manual
MED_FIELDS = {"name": "תרופה", "dose": "מינון", "unit": "יחידה", "frequency": "תדירות", "route": "דרך מתן", "duration": "משך"}
FACT_FIELDS = {"id", "category", "statement", "segment_ids", "quote", "negated", "uncertain", "treatment_status", "medication"}


def cell(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def checked(value) -> bool:
    try:
        return bool(value) and not pd.isna(value)
    except (TypeError, ValueError):
        return False


def ids(value) -> list[str]:
    return [i.strip() for i in cell(value).replace("،", ",").split(",") if i.strip()]


def labels(mapping: dict, lang: str = "he") -> dict:
    return {k: translate(v, lang) for k, v in mapping.items()}


def issue_text(issues, lang: str = "he") -> str:
    """Hebrew keeps the detailed message; other languages get the catalog text by issue code."""
    def text(i):
        return i.message if lang == "he" else translate(ISSUE_MESSAGES.get(i.code, i.message), lang)
    return " | ".join(("⛔ " if i.blocking else "⚠️ ") + text(i) for i in issues)


# Generic wording per validator code (the Hebrew messages in backend/validators.py may include values).
ISSUE_MESSAGES = {
    "empty_statement": "חסר טקסט לממצא.", "bad_reference": "הממצא מפנה למקטע שאינו קיים בתמלול שנבדק.",
    "quote_not_found": "הציטוט אינו מופיע במקטעים שצוינו.", "role_unconfirmed": "המקור כולל דובר שתפקידו לא אושר או דובר נוסף.",
    "role_mismatch": "הקטגוריה אינה מתאימה לדובר (רופא/מטופל).", "status_missing": "יש לסמן אם הטיפול קודם, ניתן בביקור, הומלץ או מתוכנן.",
    "role_check": "תלונה שמקורה בדברי הרופא; ודאו שהיא משקפת את דברי המטופל.", "unsupported_number": "מספרים שאינם מופיעים בציטוט.",
    "unsupported_medication": "פרט תרופה שאינו מופיע בציטוט.", "negation_check": "הציטוט כולל שלילה; ודאו שהממצא משקף אותה.",
    "uncertainty_check": "הציטוט כולל אי־ודאות; ודאו שהממצא משקף אותה.",
    "ambiguous_source": "המקור כולל דיבור חופף או דובר שלישי; בדקו מול ההקלטה.",
    "foreign_script": "הממצא כולל מילים בכתב זר; נסחו אותו מחדש בשפת הסיכום.",
    "scope": "סיכום המטופל מוגבל להערכה, לטיפול ולהנחיות שנאמרו.", "previous_treatment": "המשפט מבוסס על טיפול קודם ואינו הוראה חדשה.",
    "source_blocked": "המשפט מבוסס על ממצא שעדיין מסומן כבעייתי בטיוטת הרופא.",
}


def segment_rows(t: Transcript, lang: str = "he"):
    return pd.DataFrame([{"id": s.id, "start": s.start, "end": s.end, "speaker": s.speaker, "role": translate(ROLE_LABELS[t.role_of(s)], lang),
                          "text": s.text, "flags": ", ".join(s.flags)} for s in t.segments])


def edited_transcript(t: Transcript, table: pd.DataFrame, roles: dict, confirmed: bool) -> Transcript:
    old = t.by_id()
    segments = []
    for rec in table.to_dict("records"):
        prev, text = old[rec["id"]], cell(rec["text"])
        if not text:
            continue  # clearing a segment's text removes it
        speaker = cell(rec["speaker"]) or "UNKNOWN"
        flags = [f for f in prev.flags if f != "unassigned"] + (["unassigned"] if speaker == "UNKNOWN" else [])
        segments.append(prev.model_copy(update={"text": text, "speaker": speaker, "flags": flags,
                                                "words": prev.words if text == prev.text else []}))
    if not segments:
        raise ValueError("התמלול ריק.")
    used = {s.speaker for s in segments} - {"UNKNOWN"}
    new_roles = {label: roles.get(label) or t.roles.get(label) or ROLE_SPEAKERS.get(label, "unknown") for label in used}
    return t.model_copy(update={"segments": segments, "roles": new_roles, "roles_confirmed": confirmed})


def fact_rows(draft: ClinicianDraft, lang: str = "he"):
    rows = []
    cats, stats = labels(CATEGORY_LABELS, lang), labels(STATUS_LABELS, lang)
    for f in draft.facts:
        med = f.medication or Medication()
        rows.append({"id": f.id, "category": cats[f.category], "statement": f.statement,
                     "segment_ids": ", ".join(f.segment_ids), "quote": f.quote, "negated": f.negated, "uncertain": f.uncertain,
                     "treatment_status": stats[f.treatment_status],
                     **{k: getattr(med, k) or "" for k in MED_FIELDS},
                     "source": f"{translate(ROLE_LABELS[f.speaker_role], lang)} {f.start:.1f}–{f.end:.1f}", "issues": issue_text(f.issues, lang)})
    return pd.DataFrame(rows, columns=["id", "category", "statement", "segment_ids", "quote", "negated", "uncertain",
                                       "treatment_status", *MED_FIELDS, "source", "issues"])


def edited_draft(draft: ClinicianDraft, table: pd.DataFrame, lang: str = "he") -> ClinicianDraft:
    categories = {v: k for k, v in labels(CATEGORY_LABELS, lang).items()}
    statuses = {v: k for k, v in labels(STATUS_LABELS, lang).items()}
    records = table.to_dict("records")
    taken = {cell(r["id"]) for r in records}
    n = max((int(i[1:]) for i in taken if i[1:].isdigit()), default=0)
    facts = []
    for rec in records:
        if not cell(rec["statement"]) and not cell(rec["quote"]):
            continue  # empty or cleared row
        if rec["category"] not in categories:
            raise ValueError("בחרו קטגוריה לכל ממצא.")
        fid = cell(rec["id"])
        if not fid:
            n += 1
            fid = f"F{n}"
        med = {k: cell(rec[k]) or None for k in MED_FIELDS}
        facts.append(Fact(id=fid, category=categories[rec["category"]], statement=cell(rec["statement"]),
                          segment_ids=ids(rec["segment_ids"]), quote=cell(rec["quote"]), negated=checked(rec["negated"]),
                          uncertain=checked(rec["uncertain"]), treatment_status=statuses.get(rec["treatment_status"], "not_stated"),
                          medication=Medication(**med) if any(med.values()) else None))
    return draft.model_copy(update={"facts": facts})


def item_rows(summary: PatientSummary, lang: str = "he"):
    return pd.DataFrame([{"text": i.text, "fact_ids": ", ".join(i.fact_ids),
                          "category": translate(CATEGORY_LABELS.get(i.category, ""), lang) if i.category else "",
                          "issues": issue_text(i.issues, lang)} for i in summary.items],
                        columns=["text", "fact_ids", "category", "issues"])
