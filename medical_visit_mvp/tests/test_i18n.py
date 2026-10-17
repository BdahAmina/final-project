"""Interface languages (Hebrew, Arabic, English): catalog completeness, agent answers in the chosen language,
direction, exports, and every screen rendering in each language. Fictional data only."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from backend import export
from backend.assistant import ClinicalAssistant
from backend.i18n import catalog, direction, localize, translate
from backend.knowledge import load
from tests.accounts import DOC, PASSWORD
from tests.test_assistant import FULL_CONTEXT, AssistantBase, answer
from tests.test_components import FakeClient
import i18n_keys

ARABIC = re.compile(r"[؀-ۿ]")
HEBREW = re.compile(r"[֐-׿]")


class CatalogTests(unittest.TestCase):
    def test_every_interface_string_has_arabic_and_english(self):
        missing = [k for k in i18n_keys.collect() if not (catalog().get(k) or {}).get("ar") or not (catalog().get(k) or {}).get("en")]
        self.assertEqual(missing, [])

    def test_placeholders_survive_translation(self):
        ph = re.compile(r"\{[a-z_]+\}")
        for key, tr in catalog().items():
            for lang in ("ar", "en"):
                self.assertEqual(sorted(ph.findall(tr[lang])), sorted(ph.findall(key)), (key, lang))

    def test_translate_and_localize(self):
        self.assertEqual(translate("יציאה", "en"), "Sign out")
        self.assertEqual(translate("ביקור {id}", "ar", id="abc"), "زيارة abc")
        self.assertEqual(translate("טקסט שאינו בקטלוג", "en"), "טקסט שאינו בקטלוג")  # falls back, never fails
        self.assertEqual(localize("גיל: לא ידוע", "en"), "Age: Unknown")
        self.assertEqual(localize("אלרגיות: רשומות: פניצילין", "en"), "Allergies: Listed: פניצילין")  # values typed by the doctor stay
        self.assertEqual(localize("כאב גרון וחום יומיים", "en"), "כאב גרון וחום יומיים")  # record content is never translated
        self.assertEqual((direction("he"), direction("ar"), direction("en")), ("rtl", "rtl", "ltr"))


class AgentLanguageTests(AssistantBase):
    def ask_in(self, language, question, *responses):
        self.client = FakeClient(*responses)
        return ClinicalAssistant(self.repo, load(), client_factory=lambda: self.client).ask(DOC, self.visit, question, language=language)

    def test_answer_language_follows_the_request(self):
        for lang, script in (("ar", ARABIC), ("en", re.compile(r"[A-Za-z]"))):
            r = self.ask_in(lang, "האם אקמול מתאים לה?", answer())  # unknown patient context -> missing-information rows
            self.assertEqual(r["language"], lang)
            self.assertEqual(json.loads(self.client.calls[0])["answer_language"], {"ar": "Arabic", "en": "English"}[lang])
            for text in r["limitations"]:
                self.assertTrue(script.search(text), text)
                self.assertFalse(HEBREW.search(text), text)
            self.assertTrue(r["missing_information"])
            for text in r["missing_information"]:
                self.assertFalse(HEBREW.search(text), text)  # system-built rows are localized

    def test_rejection_and_quick_actions_in_arabic(self):
        r = self.ask_in("ar", "כתוב לי פונקציה בפייתון")
        self.assertEqual(r["message"], translate("העוזר עונה רק על שאלות הקשורות לטיפול במטופל/ת של ביקור זה.", "ar"))
        self.client = FakeClient()
        r = ClinicalAssistant(self.repo, load(), client_factory=lambda: self.client).run_action(DOC, self.visit, "missing", "ar")
        self.assertTrue(ARABIC.search(r["message"]))
        self.assertIn(translate("מה גיל המטופל/ת?", "ar"), r["clarification_questions"])
        history = self.repo.assistant_history(DOC, self.visit)
        self.assertEqual(history[-2]["content"]["question"], "⚡ " + translate("זיהוי מידע חסר", "ar"))

    def test_record_content_is_kept_as_recorded(self):
        self.client = FakeClient()
        r = ClinicalAssistant(self.repo, load(), client_factory=lambda: self.client).run_action(DOC, self.visit, "summary", "en")
        facts = [f["text"] for f in r["known_facts"] if f["ref"].startswith("F")]
        self.assertIn("כאב גרון וחום יומיים", facts)  # the doctor-reviewed fact, not a machine translation
        self.assertTrue(all(not HEBREW.search(f["source"]) for f in r["known_facts"] if f["ref"].startswith("F")))

    def test_model_text_in_the_wrong_language_is_not_shown(self):
        r = self.ask_in("ar", "מה ידוע על החום?", answer(answer="המטופלת מתלוננת על כאב גרון וחום.",
                                                           clarification_questions=["מתי התחיל החום?", "متى بدأت الحمى؟"]))
        self.assertNotIn("answer", r)
        self.assertIn({"kind": "answer", "reason": "wrong_language"}, r["removed"])
        self.assertIn("متى بدأت الحمى؟", r["clarification_questions"])
        self.assertNotIn("מתי התחיל החום?", r["clarification_questions"])
        r = self.ask_in("ar", "מה ידוע על החום?", answer(answer="تشتكي المريضة من ألم في الحلق وحمى منذ يومين."))
        self.assertEqual(r["answer"], "تشتكي المريضة من ألم في الحلق وحمى منذ يومين.")

    def test_hebrew_stays_the_default(self):
        r = self.ask_in("he", "מה ידוע על החום?", answer())
        self.assertEqual(r["language"], "he")
        self.assertTrue(any(HEBREW.search(x) for x in r["limitations"]))


class ExportLanguageTests(AssistantBase):
    def test_text_and_pdf_exports_in_each_language(self):
        self.repo.add_patient_context(DOC, self.visit, FULL_CONTEXT)
        self.pipe.generate_patient(DOC, self.visit, FakeClient(json.dumps({"items": [{"text": "נחכה לתוצאת המשטח.", "fact_ids": ["F3"]}]},
                                                                           ensure_ascii=False)))
        pkg = self.pipe.review_package(DOC, self.visit)
        self.pipe.approve(DOC, self.visit, pkg["clinician"]["id"], pkg["patient"]["id"], pkg["sha256"], "doctor_a", PASSWORD)
        approved = self.repo.approved(DOC, self.visit)
        for lang, marker in (("he", "מזהה ביקור"), ("ar", "معرّف الزيارة"), ("en", "Visit ID")):
            text = export.to_text(approved, True, lang)
            self.assertIn(marker, text)
            self.assertIn("נחכה לתוצאת המשטח.", text)  # the signed content itself is unchanged
            try:
                pdf = export.to_pdf(approved, True, lang)
            except ValueError as exc:  # no Hebrew/Arabic font or shaping library on this machine
                self.skipTest(str(exc))
            self.assertTrue(pdf.startswith(b"%PDF"))


class ScreenLanguageTests(unittest.TestCase):
    """Every role's screen renders in each language without an exception (Streamlit AppTest)."""
    def setUp(self):
        from unittest.mock import patch
        from sqlalchemy import update
        from backend.models import User
        from backend.pipeline import Pipeline
        from backend.repository import PseudonymStore, Repository
        from tests.accounts import add_accounts
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = Path(self.dir.name)
        repo = Repository(d / "visits.sqlite")
        self.addCleanup(repo.dispose)
        store = PseudonymStore(d / "pseudonyms.sqlite")
        self.addCleanup(store.dispose)
        self.pipe = Pipeline(repo, store, d / "tmp_audio")
        p = patch("design.ui.get_pipeline", return_value=self.pipe)
        p.start()
        self.addCleanup(p.stop)
        add_accounts(repo)
        with repo._tx() as s:
            s.execute(update(User).values(must_change_password=False))
        repo.create_visit(DOC, "patient_a", "he", True)

    def run_app(self, lang, username):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)
        at.query_params["lang"] = lang
        at.run()
        at.text_input(key="login_username").input(username)
        at.text_input(key="login_password").input(PASSWORD)
        next(b for b in at.button if b.label == translate("כניסה", lang)).click().run()
        return at

    def test_screens_in_three_languages(self):
        for lang, title in (("he", "סביבת עבודה קלינית"), ("ar", "بيئة العمل السريرية"), ("en", "Clinical workspace")):
            at = self.run_app(lang, "doctor_a")
            self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
            page = " ".join(str(m.value) for m in at.markdown)
            self.assertIn(title, page)
            self.assertIn("direction: " + direction(lang), page)  # page direction follows the language
            if lang != "he":
                self.assertNotIn("סביבת עבודה קלינית", page)
            at.button(key="assistant_launch").click().run()  # the agent drawer in this language
            self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
            self.assertIn(translate("סיכום המטופל/ת", lang), [b.label for b in at.button])
            for user in ("patient_a", "admin_root"):
                at = self.run_app(lang, user)
                self.assertEqual(len(at.exception), 0, (lang, user, [e.value for e in at.exception]))


if __name__ == "__main__":
    unittest.main()
