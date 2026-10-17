import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import diarization, redaction
from backend.llm_client import LLMError, structured
from backend.pipeline import Pipeline, parse_manual
from backend.repository import PseudonymStore, Repository
from backend.schemas import ModelExtraction, ModelFact, Medication, Transcript
from backend.validators import build_draft

from tests.accounts import DOC, PASSWORD, PAT, add_accounts
TEXT = """מטופל: אני משתעל כבר שלושה ימים. לקחתי בעבר אקמול.
רופא: הריאות נשמעות תקינות. לא נמדד חום.
רופא: נרשום לך אמוקסיצילין.
רופא: קח אקמול 500 מ"ג פעמיים ביום."""


def confirmed(text=TEXT):
    return parse_manual(text).model_copy(update={"roles_confirmed": True})


def fact(**kw):
    base = dict(category="complaints", statement="שיעול שלושה ימים", segment_ids=["S1"], quote="אני משתעל כבר שלושה ימים")
    return ModelFact(**{**base, **kw})


def codes(draft, n=0):
    return {i.code for i in draft.facts[n].issues}


class ValidatorTests(unittest.TestCase):
    def check(self, *facts):
        return build_draft(ModelExtraction(facts=list(facts)), confirmed())

    def test_supported_fact_passes_and_gets_speaker_and_time_from_source(self):
        d = self.check(fact())
        self.assertEqual(codes(d), set())
        self.assertEqual(d.facts[0].speaker_role, "patient")

    def test_quote_must_be_in_cited_segment(self):
        self.assertIn("quote_not_found", codes(self.check(fact(segment_ids=["S2"]))))
        self.assertIn("quote_not_found", codes(self.check(fact(quote="אני משתעל שבוע"))))
        self.assertIn("bad_reference", codes(self.check(fact(segment_ids=["S99"]))))

    def test_missing_dose_is_not_invented(self):
        d = self.check(fact(category="treatment", statement="אמוקסיצילין 250 מ\"ג", segment_ids=["S3"], quote="נרשום לך אמוקסיצילין",
                            treatment_status="planned", medication=Medication(name="אמוקסיצילין", dose="250", unit="מ\"ג")))
        self.assertTrue({"unsupported_number", "unsupported_medication"} <= codes(d))
        self.assertTrue(all(i.blocking for i in d.facts[0].issues if i.code.startswith("unsupported")))

    def test_stated_medication_passes(self):
        d = self.check(fact(category="treatment", statement="אקמול 500 מ\"ג פעמיים ביום", segment_ids=["S4"], quote="קח אקמול 500 מ\"ג פעמיים ביום",
                            treatment_status="recommended", medication=Medication(name="אקמול", dose="500", unit="מ\"ג", frequency="פעמיים ביום")))
        self.assertEqual(codes(d), set())

    def test_previous_patient_medication_is_not_a_new_instruction(self):
        d = self.check(fact(category="treatment", statement="אקמול", segment_ids=["S1"], quote="לקחתי בעבר אקמול", treatment_status="recommended"))
        self.assertIn("role_mismatch", codes(d))
        ok = self.check(fact(category="treatment", statement="נטל אקמול בעבר", segment_ids=["S1"], quote="לקחתי בעבר אקמול", treatment_status="previous"))
        self.assertNotIn("role_mismatch", codes(ok))

    def test_doctor_only_categories_and_negation(self):
        self.assertIn("role_mismatch", codes(self.check(fact(category="assessment"))))
        d = self.check(fact(category="findings", statement="חום נמדד", segment_ids=["S2"], quote="לא נמדד חום"))
        self.assertIn("negation_check", codes(d))

    def test_same_sentence_in_two_categories_keeps_the_valid_one(self):
        d = self.check(fact(), fact(category="findings"))  # patient's complaint also filed as an examination finding
        self.assertEqual([(f.id, f.category) for f in d.facts], [("F1", "complaints")])
        self.assertEqual(codes(d), set())
        d = self.check(fact(category="findings"), fact())  # order does not matter: the blocked copy is dropped
        self.assertEqual([(f.id, f.category) for f in d.facts], [("F1", "complaints")])

    def test_different_facts_from_one_quote_are_kept(self):
        d = self.check(fact(), fact(statement="משתעל"))
        self.assertEqual(len(d.facts), 2)

    def test_statement_with_another_alphabet_is_blocked(self):
        d = self.check(fact(statement="המטופל сообщает על שיעול"))
        self.assertIn("foreign_script", codes(d))
        self.assertTrue(d.facts[0].issues[0].blocking)
        self.assertNotIn("foreign_script", codes(self.check(fact(statement="שיעול, נטל paracetamol"))))  # Latin drug names are fine

    def test_missing_categories_reported_not_treated_as_negative(self):
        d = self.check(fact())
        self.assertIn("follow_up", d.missing)


class RedactionTests(unittest.TestCase):
    def test_identifiers_replaced_consistently(self):
        t = parse_manual("מטופל: שמי דנה לוי, ת.ז 123456782, טלפון 052-1234567.\nרופא: דנה, נולדת ב 12/03/1985? מייל dana@example.com\nמטופל: אני גרה ברחוב הרצל 12.")
        red, mapping, findings = redaction.redact_transcript(t)
        joined = " ".join(s.text for s in red.segments)
        for secret in ("דנה", "123456782", "052-1234567", "12/03/1985", "dana@example.com", "הרצל"):
            self.assertNotIn(secret, joined)
        self.assertIn("PATIENT_1", red.segments[0].text)
        self.assertIn("PATIENT_1", red.segments[1].text)  # same person, same placeholder
        self.assertEqual(mapping["PATIENT_1"], "דנה לוי")
        self.assertTrue({"ID", "PHONE", "DATE", "EMAIL", "ADDRESS"} <= {f["type"] for f in findings})

    def test_arabic_context_names_and_doses_untouched(self):
        t = parse_manual("مريض: اسمي أحمد ورقم الهوية 987654321\nطبيب: خذ 500 ملغ مرتين")
        red, mapping, _ = redaction.redact_transcript(t)
        self.assertNotIn("أحمد", red.segments[0].text)
        self.assertNotIn("987654321", red.segments[0].text)
        self.assertIn("500", red.segments[1].text)

    def test_name_without_context_is_a_known_limitation(self):
        red, _, _ = redaction.redact_transcript(parse_manual("מטופל: יוסי אמר לי לבוא"))
        self.assertIn("יוסי", red.segments[0].text)  # documented: manual review required


class DiarizationAlignmentTests(unittest.TestCase):
    def test_split_at_speaker_change_overlap_and_third_speaker(self):
        segs = [{"start": 0, "end": 4, "text": "שלום מה שלומך טוב",
                 "words": [{"start": 0, "end": 1, "text": "שלום"}, {"start": 1, "end": 2, "text": " מה"},
                           {"start": 2, "end": 3, "text": " שלומך"}, {"start": 3, "end": 4, "text": " טוב"}]},
                {"start": 10, "end": 11, "text": "אני אח", "words": []}]
        turns = [(0, 2.1, "SPEAKER_00"), (2.1, 3.0, "SPEAKER_01"), (2.9, 4.0, "SPEAKER_00"), (2.4, 3.2, "SPEAKER_00"),
                 (10, 11, "SPEAKER_02"), (20, 40, "SPEAKER_01"), (40, 60, "SPEAKER_00")]
        out = diarization.align(segs, turns)
        self.assertEqual([s["speaker"] for s in out], ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_02"])
        self.assertEqual(out[0]["text"], "שלום מה")
        self.assertIn("overlap", out[1]["flags"])
        self.assertIn("third_speaker", out[3]["flags"])

    def test_no_turns_marks_unassigned(self):
        out = diarization.align([{"start": 0, "end": 1, "text": "x", "words": []}], [])
        self.assertIn("unassigned", out[0]["flags"])


class FakeClient:
    provider, model, external = "fake", "fake", False

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _call(self, system, user, schema):
        self.calls.append(user)
        return self.responses.pop(0)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.p = Pipeline(Repository(d / "v.sqlite"), PseudonymStore(d / "m.sqlite"), d / "tmp")
        add_accounts(self.p.repo)
        self.visit = self.p.repo.create_visit(DOC, "patient_a", "he", True)

    def tearDown(self):
        for eng in (self.p.repo._read.kw["bind"], self.p.repo._write.kw["bind"], self.p.pseudonyms._session.kw["bind"]):
            eng.dispose()
        self.dir.cleanup()

    def test_schema_violation_retried_then_rejected(self):
        with self.assertRaises(LLMError):
            structured(FakeClient("not json", '{"facts": "x"}'), "s", "u", ModelExtraction)
        ok = structured(FakeClient("{}", '{"facts": []}'), "s", "u", ModelExtraction)
        self.assertEqual(ok.facts, [])

    def test_end_to_end_with_review_gates_and_patient_view(self):
        t = self.p.manual_transcript(DOC, self.visit, "מטופל: שמי דנה לוי, אני משתעל.\nרופא: קח אקמול 500 מ\"ג פעמיים ביום. חזור בעוד שבוע.")
        self.assertEqual(self.p.repo.latest_transcript(DOC, self.visit)["origin"], "manual")
        row, tr = self.p.transcript(DOC, self.visit)
        with self.assertRaises(ValueError):
            self.p.redact(DOC, self.visit)  # roles not confirmed yet
        t = self.p.save_review(DOC, self.visit, tr.model_copy(update={"roles_confirmed": True}), row["id"])
        with self.assertRaises(ValueError):
            self.p.extract(DOC, self.visit, FakeClient())  # redaction not reviewed
        t, findings = self.p.redact(DOC, self.visit)
        row, tr = self.p.transcript(DOC, self.visit)
        self.assertNotIn("דנה", tr.segments[0].text)
        t = self.p.confirm_redaction(DOC, self.visit, tr, row["id"])
        extraction = {"facts": [
            {"category": "complaints", "statement": "שיעול", "segment_ids": ["S1"], "quote": "אני משתעל"},
            {"category": "treatment", "statement": "אקמול 500 מ\"ג פעמיים ביום", "segment_ids": ["S2"], "quote": "קח אקמול 500 מ\"ג פעמיים ביום",
             "treatment_status": "recommended", "medication": {"name": "אקמול", "dose": "500", "unit": "מ\"ג", "frequency": "פעמיים ביום"}},
            {"category": "follow_up", "statement": "ביקורת בעוד שבוע", "segment_ids": ["S2"], "quote": "חזור בעוד שבוע"}]}
        client = FakeClient(json.dumps(extraction, ensure_ascii=False),
                            json.dumps({"items": [{"text": "קח אקמול 500 מ\"ג פעמיים ביום.", "fact_ids": ["F2"]},
                                                  {"text": "חזור לביקורת בעוד שבוע.", "fact_ids": ["F3"]}]}, ensure_ascii=False))
        self.p.extract(DOC, self.visit, client)
        self.assertNotIn("דנה", client.calls[0])  # model saw only redacted text
        self.p.generate_patient(DOC, self.visit, client)
        pkg = self.p.review_package(DOC, self.visit)
        self.assertIsNone(self.p.repo.approved(PAT, self.visit))
        self.p.approve(DOC, self.visit, pkg["clinician"]["id"], pkg["patient"]["id"], pkg["sha256"], "doctor_a", PASSWORD)
        view = self.p.repo.approved(PAT, self.visit)
        self.assertEqual(len(view["patient"]["items"]), 2)
        self.assertNotIn("clinician", view)
        # editing the transcript afterwards invalidates everything downstream
        row, tr = self.p.transcript(DOC, self.visit)
        self.p.save_review(DOC, self.visit, tr, row["id"])
        self.assertIsNone(self.p.repo.approved(PAT, self.visit))
        self.assertIsNone(self.p.review_package(DOC, self.visit))

    def test_failed_extraction_sets_failed_state(self):
        self.p.manual_transcript(DOC, self.visit, "רופא: שלום")
        row, tr = self.p.transcript(DOC, self.visit)
        self.p.save_review(DOC, self.visit, tr.model_copy(update={"roles_confirmed": True}), row["id"])
        self.p.redact(DOC, self.visit)
        row, tr = self.p.transcript(DOC, self.visit)
        self.p.confirm_redaction(DOC, self.visit, tr, row["id"])
        with self.assertRaises(LLMError):
            self.p.extract(DOC, self.visit, FakeClient("bad", "bad"))
        visit = self.p.repo.visit(DOC, self.visit)
        self.assertEqual((visit["state"], visit["error_category"]), ("failed", "extraction"))
        self.assertIsNone(self.p.repo.current_summary(DOC, self.visit, "clinician"))

    def test_session_end_deletes_temp_audio_and_mapping(self):
        import io, struct, wave
        b = io.BytesIO()
        with wave.open(b, "wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(struct.pack("<h", 900) * 32000)
        path = self.p.store_audio(DOC, self.visit, b.getvalue(), "r.wav")
        self.p.pseudonyms.save(self.visit, {"PATIENT_1": "x"})
        self.assertTrue(path.exists())
        self.p.end_session(DOC, self.visit)
        self.assertFalse(path.exists())
        self.assertEqual(self.p.pseudonyms.get(self.visit), {})


if __name__ == "__main__":
    unittest.main()
