"""Deterministic checks on model output against the reviewed transcript.

An exact quote match does not prove that a statement follows from the quote;
these checks only catch technical and evidence errors. Human review remains
required before approval.
"""
import re
import unicodedata
from backend.schemas import (CATEGORY_LABELS, ClinicianDraft, Fact, Issue, ModelExtraction,
                             ModelPatientSummary, PatientItem, PatientSummary, Transcript)

DOCTOR_ONLY = {"findings", "assessment", "follow_up"}
NEGATION = re.compile(r"(^|[\s,.;:!?\"'(])(לא|אין|אינו|אינה|בלי|ללא|לא\s+ניתן|لا|لم|لن|ما|ليس|مش|بدون|no|not|denies|without|never)(?=$|[\s,.;:!?\"')])", re.I)
UNCERTAIN = re.compile(r"(אולי|ייתכן|יתכן|כנראה|לא\s+בטוח|ممكن|يمكن|ربما|مش\s+متأكد|maybe|possibly|probably|not\s+sure|might)", re.I)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
HEBREW_PREFIX = "ובהלמשכ"
# Letters allowed in a statement: Hebrew, Arabic and Latin (drug names). Anything else (Cyrillic, Greek, CJK...) is model drift.
ALLOWED_LETTERS = re.compile(r"[֐-׿؀-ۿݐ-ݿࢠ-ࣿיִ-﷿ﹰ-﻿A-Za-zÀ-ɏ]")


def foreign_words(statement: str, quote: str) -> list[str]:
    """Words of the statement that contain letters of another alphabet and are not copied from the quote."""
    quote_norm = normalize(quote)
    return [w for w in statement.split()
            if any(ch.isalpha() and not ALLOWED_LETTERS.match(ch) for ch in w) and normalize(w).strip(".,;:!?\"'()") not in quote_norm]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[֑-ׇً-ٰٟ]", "", text)  # Hebrew/Arabic diacritics
    text = text.replace("״", '"').replace("׳", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in NUMBER.findall(normalize(text))}


def contains_term(haystack: str, term: str) -> bool:
    """Substring match tolerant of whitespace, diacritics and attached Hebrew prefixes."""
    h, t = normalize(haystack), normalize(term)
    return bool(t) and (t in h or any(t.startswith(p) and t[1:] in h for p in HEBREW_PREFIX))


def _issue(code, message, blocking=True):
    return Issue(code=code, message=message, blocking=blocking)


def check_fact(fact: Fact, transcript: Transcript) -> list[Issue]:
    issues = []
    segments = transcript.by_id()
    cited = [segments[i] for i in fact.segment_ids if i in segments]
    if not fact.statement.strip():
        issues.append(_issue("empty_statement", "חסר טקסט לממצא."))
    if foreign := foreign_words(fact.statement, fact.quote):
        issues.append(_issue("foreign_script", f"הממצא כולל מילים בכתב זר שאינן בציטוט ({', '.join(foreign[:3])}); נסחו אותו מחדש בשפת הסיכום."))
    if not fact.segment_ids or len(cited) != len(fact.segment_ids):
        issues.append(_issue("bad_reference", "הממצא מפנה למקטע שאינו קיים בתמלול שנבדק."))
    source = " ".join(s.text for s in cited)
    if not fact.quote.strip() or normalize(fact.quote) not in normalize(source):
        issues.append(_issue("quote_not_found", "הציטוט אינו מופיע במקטעים שצוינו."))
    roles = {transcript.role_of(s) for s in cited}
    if "unknown" in roles or "other" in roles:
        issues.append(_issue("role_unconfirmed", "המקור כולל דובר שתפקידו לא אושר או דובר נוסף.", blocking=fact.category in DOCTOR_ONLY | {"treatment"}))
    if fact.category in DOCTOR_ONLY and roles and roles != {"doctor"}:
        issues.append(_issue("role_mismatch", f"'{CATEGORY_LABELS[fact.category]}' חייב להיאמר על ידי הרופא."))
    if fact.category == "treatment":
        if fact.treatment_status == "not_stated":
            issues.append(_issue("status_missing", "יש לסמן אם הטיפול קודם, ניתן בביקור, הומלץ או מתוכנן."))
        elif fact.treatment_status != "previous" and roles != {"doctor"}:
            issues.append(_issue("role_mismatch", "טיפול נוכחי או מומלץ חייב להיאמר על ידי הרופא; דברי המטופל אינם הוראת טיפול."))
    if fact.category == "complaints" and roles == {"doctor"}:
        issues.append(_issue("role_check", "תלונה שמקורה בדברי הרופא; ודאו שהיא משקפת את דברי המטופל.", blocking=False))
    if missing := sorted(numbers(fact.statement) - numbers(fact.quote)):
        issues.append(_issue("unsupported_number", f"מספרים שאינם מופיעים בציטוט: {', '.join(missing)}."))
    if fact.medication:
        for field, value in fact.medication.model_dump().items():
            if value and not contains_term(fact.quote, value):
                issues.append(_issue("unsupported_medication", f"פרט התרופה '{field}' ({value}) אינו מופיע בציטוט. אין להשלים פרטים שלא נאמרו."))
    if NEGATION.search(normalize(fact.quote)) and not fact.negated:
        issues.append(_issue("negation_check", "הציטוט כולל שלילה; ודאו שהממצא משקף אותה.", blocking=False))
    if UNCERTAIN.search(normalize(fact.quote)) and not fact.uncertain:
        issues.append(_issue("uncertainty_check", "הציטוט כולל אי־ודאות; ודאו שהממצא משקף אותה.", blocking=False))
    if any("overlap" in s.flags or "third_speaker" in s.flags for s in cited):
        issues.append(_issue("ambiguous_source", "המקור כולל דיבור חופף או דובר שלישי; בדקו מול ההקלטה.", blocking=False))
    return issues


def build_draft(extraction: ModelExtraction, transcript: Transcript, model: str = "") -> ClinicianDraft:
    facts = [Fact(**item.model_dump(), id=f"F{n}") for n, item in enumerate(extraction.facts, 1)]
    draft = revalidate(ClinicianDraft(facts=facts, contradictions=extraction.contradictions, model=model), transcript)
    return revalidate(drop_duplicates(draft), transcript)


def drop_duplicates(draft: ClinicianDraft) -> ClinicianDraft:
    """The model sometimes files one sentence under several categories (e.g. a patient's complaint also as an
    examination finding). Keep one copy per statement+quote: the one with the fewest blocking issues, then the
    earliest. Facts are renumbered. Applied to model output only; a doctor's own edits are never merged."""
    best = {}
    for n, f in enumerate(draft.facts):
        key = (normalize(f.statement), normalize(f.quote), tuple(f.segment_ids))
        rank = (sum(i.blocking for i in f.issues), n)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, f)
    kept = sorted(best.values(), key=lambda x: x[0][1])
    draft.facts = [f.model_copy(update={"id": f"F{n}"}) for n, (_, f) in enumerate(kept, 1)]
    return draft


def revalidate(draft: ClinicianDraft, transcript: Transcript) -> ClinicianDraft:
    """Re-derive speaker, role and timestamps from the cited segments (never trusted from the
    model or an edit), then re-run every check."""
    segments = transcript.by_id()
    for fact in draft.facts:
        cited = [segments[i] for i in fact.segment_ids if i in segments]
        roles = sorted({transcript.role_of(s) for s in cited})
        fact.speaker = ",".join(sorted({s.speaker for s in cited})) or "UNKNOWN"
        fact.speaker_role = roles[0] if len(roles) == 1 else "unknown"
        fact.start = min((s.start for s in cited), default=0.0)
        fact.end = max((s.end for s in cited), default=0.0)
        fact.issues = check_fact(fact, transcript)
    present = {f.category for f in draft.facts}
    draft.missing = [c for c in CATEGORY_LABELS if c not in present]
    return draft


def check_patient_item(item: PatientItem, draft: ClinicianDraft) -> list:
    facts = {f.id: f for f in draft.facts}
    issues = []
    if not item.fact_ids or any(i not in facts for i in item.fact_ids):
        issues.append(_issue("bad_reference", "המשפט אינו מקושר לממצא קיים בטיוטת הרופא."))
        return issues
    cited = [facts[i] for i in item.fact_ids]
    if any(f.category not in {"assessment", "treatment", "follow_up"} for f in cited):
        issues.append(_issue("scope", "סיכום המטופל מוגבל להערכה, לטיפול ולהנחיות שנאמרו.", blocking=False))
    if any(f.category == "treatment" and f.treatment_status == "previous" for f in cited):
        issues.append(_issue("previous_treatment", "המשפט מבוסס על טיפול קודם ואינו הוראה חדשה."))
    if missing := sorted(numbers(item.text) - numbers(" ".join(f.quote for f in cited))):
        issues.append(_issue("unsupported_number", f"מספרים שאינם מופיעים במקור: {', '.join(missing)}."))
    if any(f.issues and any(i.blocking for i in f.issues) for f in cited):
        issues.append(_issue("source_blocked", "המשפט מבוסס על ממצא שעדיין מסומן כבעייתי בטיוטת הרופא."))
    return issues


def build_patient_summary(result: ModelPatientSummary, draft: ClinicianDraft, model: str = "") -> PatientSummary:
    summary = PatientSummary(items=[PatientItem(**i.model_dump()) for i in result.items], model=model)
    return revalidate_patient(summary, draft)


def revalidate_patient(summary: PatientSummary, draft: ClinicianDraft) -> PatientSummary:
    facts = {f.id: f for f in draft.facts}
    for item in summary.items:
        cited = [facts[i] for i in item.fact_ids if i in facts]
        item.category = cited[0].category if cited else None
        item.medications = [f.medication for f in cited if f.medication and not any(i.blocking for i in f.issues)]
        item.issues = check_patient_item(item, draft)
    return summary
