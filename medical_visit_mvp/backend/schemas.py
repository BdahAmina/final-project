"""Pydantic models shared by the pipeline, validators, API and UI.

The language model only returns `ModelFact` items that cite segment ids.
Speaker, role and timestamps are filled in by code from the reviewed
transcript, never taken from the model.
"""
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

Role = Literal["doctor", "patient", "other", "unknown"]
Category = Literal["complaints", "history", "findings", "assessment", "treatment", "follow_up"]
TreatmentStatus = Literal["previous", "administered", "recommended", "planned", "not_stated"]
Origin = Literal["auto", "edited", "manual", "redacted"]

CATEGORY_LABELS = {
    "complaints": "תלונות המטופל",
    "history": "רקע רפואי שנמסר",
    "findings": "ממצאי בדיקה שנאמרו",
    "assessment": "הערכת הרופא כפי שנאמרה",
    "treatment": "טיפול שניתן או הומלץ",
    "follow_up": "הנחיות להמשך",
}
STATUS_LABELS = {
    "previous": "תרופה/טיפול קודם",
    "administered": "ניתן בביקור",
    "recommended": "הומלץ",
    "planned": "מתוכנן",
    "not_stated": "לא צוין",
}
ROLE_LABELS = {"doctor": "רופא", "patient": "מטופל", "other": "דובר נוסף", "unknown": "לא ידוע"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Word(Strict):
    start: float
    end: float
    text: str


class Segment(Strict):
    id: str
    start: float = 0.0
    end: float = 0.0
    speaker: str = "UNKNOWN"
    text: str
    words: list[Word] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class Transcript(Strict):
    segments: list[Segment]
    roles: dict[str, Role] = Field(default_factory=dict)
    language: Optional[str] = None
    notices: list[str] = Field(default_factory=list)
    source: Literal["microphone", "upload", "manual"] = "microphone"
    roles_confirmed: bool = False
    redaction_reviewed: bool = False

    def role_of(self, segment: Segment) -> Role:
        return self.roles.get(segment.speaker, "unknown")

    def by_id(self) -> dict[str, Segment]:
        return {s.id: s for s in self.segments}


class Medication(Strict):
    name: Optional[str] = None
    dose: Optional[str] = None
    unit: Optional[str] = None
    frequency: Optional[str] = None
    route: Optional[str] = None
    duration: Optional[str] = None


class ModelFact(Strict):
    """What the language model is allowed to return."""
    category: Category
    statement: str
    segment_ids: list[str]
    quote: str
    negated: bool = False
    uncertain: bool = False
    treatment_status: TreatmentStatus = "not_stated"
    medication: Optional[Medication] = None


class ModelExtraction(Strict):
    facts: list[ModelFact]
    contradictions: list[str] = Field(default_factory=list)


class Issue(Strict):
    code: str
    message: str
    blocking: bool


class Fact(ModelFact):
    id: str
    speaker: str = "UNKNOWN"
    speaker_role: Role = "unknown"
    start: float = 0.0
    end: float = 0.0
    issues: list[Issue] = Field(default_factory=list)


class ClinicianDraft(Strict):
    facts: list[Fact]
    contradictions: list[str] = Field(default_factory=list)
    missing: list[Category] = Field(default_factory=list)
    model: str = ""


class ModelPatientItem(Strict):
    text: str
    fact_ids: list[str]


class ModelPatientSummary(Strict):
    items: list[ModelPatientItem]


class PatientItem(ModelPatientItem):
    # Filled by code from the cited clinician facts, not by the model.
    category: Optional[Category] = None
    medications: list[Medication] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class PatientSummary(Strict):
    items: list[PatientItem]
    model: str = ""


def blocking_issues(items) -> list[Issue]:
    return [i for item in items for i in item.issues if i.blocking]


# ---- doctor-entered patient context (clinical assistant) ------------------------------------------
class PatientContext(Strict):
    """Entered and confirmed by the doctor; never inferred by a model. Each field keeps 'unknown'
    distinct from a confirmed negative ('none_known', 'not_pregnant', 'normal')."""
    age_years: Optional[int] = Field(default=None, ge=0, le=120)
    sex: Literal["unknown", "female", "male", "other"] = "unknown"
    pregnancy: Literal["unknown", "not_pregnant", "pregnant", "not_applicable"] = "unknown"
    lactation: Literal["unknown", "no", "yes", "not_applicable"] = "unknown"
    allergy_status: Literal["unknown", "none_known", "listed"] = "unknown"
    allergies: list[str] = Field(default_factory=list)
    medication_status: Literal["unknown", "none", "listed"] = "unknown"
    medications: list[str] = Field(default_factory=list)
    renal: Literal["unknown", "normal", "impaired"] = "unknown"
    hepatic: Literal["unknown", "normal", "impaired"] = "unknown"
    conditions: list[str] = Field(default_factory=list)

    def model_post_init(self, _):
        if (self.allergy_status == "listed") != bool(self.allergies):
            raise ValueError("רשימת אלרגיות נדרשת רק כאשר הסטטוס הוא 'רשומות'.")
        if (self.medication_status == "listed") != bool(self.medications):
            raise ValueError("רשימת תרופות נדרשת רק כאשר הסטטוס הוא 'רשומות'.")
