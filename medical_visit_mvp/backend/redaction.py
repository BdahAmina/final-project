"""Rule-based pseudonymization of identifiers in visit transcripts.

Replaces identifiers with consistent placeholders (PATIENT_1, PHONE_1, ...).
The reverse mapping is returned separately so the caller can store it apart
from transcripts and delete it under the retention policy. This is
pseudonymization, not anonymization: voices and context can still identify
people. Hebrew/Arabic name detection relies on context phrases and an optional
lexicon; names said without such context are NOT detected and must be caught
in manual review.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from backend.schemas import Transcript

LEXICON = Path(__file__).resolve().parents[1] / "data" / "name_lexicon.txt"

HE_MONTHS = "ינואר|פברואר|מרץ|מרס|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר"
AR_MONTHS = "يناير|كانون الثاني|فبراير|شباط|مارس|آذار|أبريل|ابريل|نيسان|مايو|أيار|يونيو|حزيران|يوليو|تموز|أغسطس|آب|سبتمبر|أيلول|أكتوبر|تشرين الأول|نوفمبر|تشرين الثاني|ديسمبر|كانون الأول"
EN_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
NAME_WORD = r"[א-ת][א-ת'\"׳״-]{1,}|[ء-ي]{2,}|[A-Z][a-z]{1,}"
NAME_STOP = {"לא", "כן", "אני", "גם", "רק", "עם", "של", "מאוד", "היום", "פה", "كمان", "انا", "أنا", "هون", "مش", "the", "and", "is", "not"}

ID_CONTEXT = r"(?:ת\.?\s?ז\.?|תעודת\s+זהות|מספר\s+זהות|رقم\s+(?:ال)?هوي[ةه]|هوي[ةه]|بطاق[ةه]|ID(?:\s+number)?)\s*(?:שלי|שלו|שלה|היא|هي|is|:)?\s*"

PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("ID", re.compile(ID_CONTEXT + r"(\d[\d\s-]{4,10}\d)", re.I)),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?972[\s-]?|0)(?:5\d|[23489]|7\d)[\s-]?\d{3}[\s-]?\d{4}(?!\d)")),
    ("ID", re.compile(r"(?<!\d)\d{9}(?!\d)")),
    ("ID", re.compile(r"(?<![\d.,])\d{7,8}(?![\d.,])")),
    ("DATE", re.compile(r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:19|20)?\d{2}(?!\d)")),
    ("DATE", re.compile(rf"(?<!\d)\d{{1,2}}\s+(?:ב|ل)?(?:{HE_MONTHS}|{AR_MONTHS}|{EN_MONTHS})\s+(?:19|20)\d{{2}}", re.I)),
    ("DATE", re.compile(rf"(?:נולדתי|נולדת|נולד|נולדה|תאריך\s+לידה|ولدت|مواليد|تاريخ\s+الميلاد|born|date\s+of\s+birth)(?![א-תء-ي])\s*(?:ב|في|on|:)?[\s-]*"
                        rf"(\d{{1,2}}[./-]\d{{1,2}}(?:[./-]\d{{2,4}})?|\d{{1,2}}\s+(?:ב|ل)?(?:{HE_MONTHS}|{AR_MONTHS}|{EN_MONTHS})(?:\s+\d{{4}})?|(?:19|20)\d{{2}})", re.I)),
    ("ADDRESS", re.compile(r"(?:רחוב|רח['׳]|שדרות|שד['׳]|שכונת|شارع|حي|street|st\.)\s+(?:[^\s,.\d]+\s*){1,3}\d{0,4}", re.I)),
    ("ADDRESS", re.compile(r"(?:אני\s+גר(?:ה)?\s+ב|גר(?:ה)?\s+ב|ساكن(?:ة)?\s+في|أسكن\s+في|بسكن\s+ب|I\s+live\s+in\s+)([^\s,.]+(?:\s+[^\s,.]+)?)", re.I)),
]
NAME_PATTERNS = [
    ("PATIENT", re.compile(rf"(?:שמי|קוראים\s+לי|השם\s+שלי(?:\s+הוא|\s+היא)?|اسمي|انا\s+اسمي|إسمي|my\s+name\s+is|I\s+am)\s+((?:{NAME_WORD})(?:\s+(?:{NAME_WORD}))?)")),
    ("DOCTOR", re.compile(rf"(?:ד[\"״]ר|דוקטור|דוקטורית|הרופא(?:ה)?|د\.|دكتور(?:ة)?|الدكتور(?:ة)?|Dr\.?|Doctor)\s+((?:{NAME_WORD})(?:\s+(?:{NAME_WORD}))?)")),
    ("PERSON", re.compile(rf"(?:מר|גב['׳]|גברת|السيد(?:ة)?|ابني|بنتي|הבן\s+שלי|הבת\s+שלי|אשתי|בעלי|زوجي|زوجتي|Mr\.?|Mrs\.?|Ms\.?)\s+((?:{NAME_WORD}))")),
]


@dataclass
class Pseudonymizer:
    mapping: dict = field(default_factory=dict)   # placeholder -> original
    reverse: dict = field(default_factory=dict)   # normalized original -> placeholder
    counts: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)

    def placeholder(self, kind, original):
        key = re.sub(r"\s+", " ", original.strip()).lower()
        if key not in self.reverse:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.reverse[key] = f"{kind}_{self.counts[kind]}"
            self.mapping[self.reverse[key]] = original.strip()
            if kind in {"PATIENT", "DOCTOR", "PERSON"}:
                for part in key.split():  # later mentions of just the first or last name
                    if len(part) > 1 and part not in NAME_STOP:
                        self.reverse.setdefault(part, self.reverse[key])
        return self.reverse[key]

    def spans(self, text):
        found = []
        for kind, pattern in PATTERNS + NAME_PATTERNS:
            for m in pattern.finditer(text):
                start, end = m.span(1) if m.groups() else m.span()
                value = text[start:end].strip()
                if kind in {"PATIENT", "DOCTOR", "PERSON"}:
                    words = [w for w in value.split() if w not in NAME_STOP]
                    if not words:
                        continue
                    value = " ".join(words)
                    end = start + text[start:].find(words[-1]) + len(words[-1])
                    start = start + text[start:].find(words[0])
                if value:
                    found.append((start, end, kind))
        known = list(self.reverse.items()) + [(n.lower(), None) for n in lexicon()]
        for name, ph in known:
            if ph is None or ph.split("_")[0] in {"PATIENT", "DOCTOR", "PERSON"}:
                for m in re.finditer(rf"(?<![\w֐-ۿ]){re.escape(name)}(?![\w֐-ۿ])", text, re.I):
                    found.append((m.start(), m.end(), ph.rsplit("_", 1)[0] if ph else "PERSON"))
        chosen, last = [], -1
        for start, end, kind in sorted(found, key=lambda s: (s[0], -(s[1] - s[0]))):
            if start >= last:
                chosen.append((start, end, kind)); last = end
        return chosen

    def redact(self, text, segment_id=None):
        out, pos = [], 0
        for start, end, kind in self.spans(text):
            ph = self.placeholder(kind, text[start:end])
            out += [text[pos:start], ph]; pos = end
            self.findings.append({"segment_id": segment_id, "placeholder": ph, "type": kind})
        return "".join(out) + text[pos:]


def lexicon():
    if not LEXICON.exists():
        return []
    return [l.strip() for l in LEXICON.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


def redact_transcript(transcript: Transcript):
    """Return (redacted transcript, placeholder->original mapping, findings)."""
    p = Pseudonymizer()
    # Two passes so a name learned late (e.g. "my name is X") is also replaced earlier.
    for s in transcript.segments:
        p.redact(s.text)
    p.findings.clear()
    segments = []
    for s in transcript.segments:
        text = p.redact(s.text, s.id)
        segments.append(s.model_copy(update={"text": text, "words": s.words if text == s.text else []}))
    return transcript.model_copy(update={"segments": segments}), dict(p.mapping), p.findings
