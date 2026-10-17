"""Local transcription, local-only model client, table-editor conversions and the Streamlit screens (fictional data)."""
import io
import json
import re
import struct
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from sqlalchemy import update
from backend import security, transcriber
from backend.llm_client import LLMError, OllamaClient, structured
from backend.models import LoginSession, User
from backend.pipeline import Pipeline, parse_manual
from backend.repository import PseudonymStore, Repository
from backend.schemas import ClinicianDraft, Fact, ModelExtraction, Word
from design.ui import edited_draft, edited_transcript, fact_rows
from tests.accounts import ADMIN, DOC, PASSWORD, PAT, add_accounts
from tests.test_components import FakeClient

TEXT = 'מטופל: אני משתעל.\nרופא: לא ניתן טיפול תרופתי.'


def audio_bytes(silent=False):
    b = io.BytesIO()
    with wave.open(b, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes((b'\x00\x00' if silent else struct.pack('<h', 1200)) * 32000)
    return b.getvalue()


class AudioTests(unittest.TestCase):
    def test_invalid_audio_rejected(self):
        for data in (b'', b'not a wav', audio_bytes(True)):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                transcriber.check_audio(data, 'recording.wav')

    def test_local_audio_processed_in_memory(self):
        engine = Mock()
        engine.transcribe.return_value = (iter([SimpleNamespace(start=0, end=2, text=TEXT)]), SimpleNamespace(duration=2, language='he'))
        with patch('backend.transcriber.local_model', return_value=engine):
            result = transcriber.transcribe_local(audio_bytes(), 'recording.wav', 'he')
        self.assertEqual(result['text'], TEXT)
        self.assertEqual(engine.transcribe.call_args.args[0].shape, (32000,))
        self.assertTrue(engine.transcribe.call_args.kwargs['vad_filter'])

    def test_vad_failure_retries_locally_without_vad(self):
        def transcribe(audio, vad_filter, **kw):
            if vad_filter:
                exec(compile('raise RuntimeError("onnx session failed")', 'site-packages/faster_whisper/vad.py', 'exec'))
            return iter([SimpleNamespace(start=0, end=2, text=TEXT)]), SimpleNamespace(duration=2, language='he')
        engine = Mock(); engine.transcribe.side_effect = transcribe
        with patch('backend.transcriber.local_model', return_value=engine):
            result = transcriber.transcribe_local(audio_bytes(), 'recording.wav', 'he')
        self.assertEqual([c.kwargs['vad_filter'] for c in engine.transcribe.call_args_list], [True, False])
        self.assertIn('VAD', result['notice'])

    def test_unrelated_failure_is_not_retried_and_not_logged_with_content(self):
        engine = Mock(); engine.transcribe.side_effect = MemoryError()
        with patch('backend.transcriber.local_model', return_value=engine), self.assertLogs('backend.transcriber') as logs:
            with self.assertRaises(ValueError) as e: transcriber.transcribe_local(audio_bytes(), 'recording.wav', 'he')
        self.assertEqual(engine.transcribe.call_count, 1)
        self.assertIn('MemoryError', str(e.exception))
        self.assertNotIn(TEXT, ' '.join(logs.output))

    def test_incompatible_pyav_explained(self):
        def bad_open(*a, **kw): raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        with patch('faster_whisper.audio.av.open', side_effect=bad_open):
            with self.assertRaises(ValueError) as e: transcriber.decode(audio_bytes())
        self.assertIn('av==18.1.0', str(e.exception))

    def test_no_speech_is_not_a_transcript(self):
        with self.assertRaises(ValueError): transcriber.finish([], 'he')


class LocalOnlyTests(unittest.TestCase):
    def test_no_cloud_provider_code_remains(self):
        hits = [str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.suffix in ('.py', '.example', '.txt', '.bat', '.md', '.json', '.ps1', '.toml', '.ini') and
                'venv' not in p.parts and p.is_file() and re.search('gr' + 'oq', p.read_text(encoding='utf-8', errors='ignore'), re.I)]
        self.assertEqual(hits, [])
        self.assertFalse(hasattr(transcriber, 'requests'))  # the transcriber has no network client at all

    def test_ollama_client_talks_only_to_the_configured_local_server(self):
        response = Mock(status_code=200)
        response.json.return_value = {'done_reason': 'stop', 'message': {'content': '{"facts": []}'}}
        with patch('backend.llm_client.requests.post', return_value=response) as send:
            result = structured(OllamaClient(base_url='http://127.0.0.1:11434'), 'sys', 'data', ModelExtraction)
        self.assertEqual(send.call_args.args[0], 'http://127.0.0.1:11434/api/chat')
        self.assertEqual(send.call_args.kwargs['json']['format']['type'], 'object')
        self.assertEqual(result.facts, [])

    def test_truncated_model_output_rejected(self):
        response = Mock(status_code=200)
        response.json.return_value = {'done_reason': 'length', 'message': {'content': '{"facts": []}'}}
        with patch('backend.llm_client.requests.post', return_value=response):
            with self.assertRaises(LLMError): structured(OllamaClient(), 's', 'u', ModelExtraction)


class EditorConversionTests(unittest.TestCase):
    """AppTest cannot drive the table editors, so their conversions are tested directly."""
    def test_transcript_edit_keeps_ids_resets_stale_words_and_maps_roles(self):
        t = parse_manual(TEXT)
        t.segments[0].words = [Word(start=0, end=1, text='אני')]
        table = pd.DataFrame([{'id': 'S1', 'speaker': 'SPEAKER_DOCTOR', 'text': 'אני משתעל מאוד.'},
                              {'id': 'S2', 'speaker': 'UNKNOWN', 'text': '   '}])
        out = edited_transcript(t, table, {}, True)
        self.assertEqual([s.id for s in out.segments], ['S1'])
        self.assertEqual(out.segments[0].words, [])
        self.assertEqual(out.roles, {'SPEAKER_DOCTOR': 'doctor'})
        self.assertTrue(out.roles_confirmed)

    def test_draft_edit_assigns_new_ids_and_parses_medication(self):
        draft = ClinicianDraft(facts=[Fact(id='F1', category='complaints', statement='שיעול', segment_ids=['S1'], quote='אני משתעל')])
        added = pd.DataFrame([{'category': 'טיפול שניתן או הומלץ', 'statement': 'אקמול', 'segment_ids': 'S2،S3',
                               'quote': 'קח אקמול', 'name': 'אקמול', 'treatment_status': 'הומלץ', 'negated': None}])
        out = edited_draft(draft, pd.concat([fact_rows(draft), added], ignore_index=True))
        self.assertEqual([f.id for f in out.facts], ['F1', 'F2'])
        self.assertEqual(out.facts[1].segment_ids, ['S2', 'S3'])
        self.assertEqual((out.facts[1].medication.name, out.facts[1].medication.dose), ('אקמול', None))
        self.assertFalse(out.facts[1].negated)


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = Path(self.dir.name)
        repo = Repository(d / 'visits.sqlite')
        self.addCleanup(repo.dispose)
        store = PseudonymStore(d / 'pseudonyms.sqlite')
        self.addCleanup(store.dispose)
        self.pipe = Pipeline(repo, store, d / 'tmp_audio')
        p = patch('design.ui.get_pipeline', return_value=self.pipe)
        p.start(); self.addCleanup(p.stop)

    def accounts(self):
        add_accounts(self.pipe.repo)
        with self.pipe.repo._tx() as s:
            s.execute(update(User).values(must_change_password=False))

    def app(self, login=None, password=PASSWORD):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=90).run()
        if login:
            self.login(at, login, password)
        return at

    def login(self, at, username, password=PASSWORD):
        at.text_input(key='login_username').input(username)
        at.text_input(key='login_password').input(password)
        self.click(at, 'כניסה')

    def click(self, at, label):
        next(b for b in at.button if b.label == label).click().run()
        self.assertFalse(at.exception, at.exception)

    def text(self, at):
        return ' '.join(str(e.value) for kind in ('markdown', 'caption', 'success', 'error', 'warning', 'info', 'header', 'subheader')
                        for e in getattr(at, kind))

    def test_first_run_has_no_self_registration(self):
        at = self.app()
        self.assertIn('create-admin', ' '.join(c.value for c in at.code))
        self.assertEqual([b.label for b in at.button], [])  # nothing to click: no way to create an account from the browser

    def test_forced_password_change_then_login(self):
        add_accounts(self.pipe.repo)  # accounts created by the admin carry a temporary password
        at = self.app('patient_a')
        self.assertIn('להחליף את הסיסמה הזמנית', self.text(at))
        form = at.get('form')[0]
        inputs = at.text_input
        inputs[0].input(PASSWORD); inputs[1].input('a-brand-new-secret-7'); inputs[2].input('a-brand-new-secret-7')
        self.click(at, 'שינוי סיסמה')
        self.login(at, 'patient_a', 'a-brand-new-secret-7')
        self.assertIn('אין עדיין סיכומים מאושרים', self.text(at))

    def test_session_expiry_returns_to_login(self):
        self.accounts()
        at = self.app('doctor_a')
        with self.pipe.repo._tx() as s:
            s.execute(update(LoginSession).values(last_seen_at=time.time() - security.SESSION_IDLE_MINUTES * 60 - 5))
        at.run()
        self.assertIn('ההתחברות הסתיימה', self.text(at))

    def test_full_workflow_with_signature_patient_view_and_revocation(self):
        self.accounts()
        extraction = {'facts': [
            {'category': 'complaints', 'statement': 'שיעול', 'segment_ids': ['S1'], 'quote': 'אני משתעל'},
            {'category': 'treatment', 'statement': 'לא ניתן טיפול תרופתי', 'segment_ids': ['S2'], 'quote': 'לא ניתן טיפול תרופתי',
             'negated': True, 'treatment_status': 'administered'}]}
        patient = {'items': [{'text': 'לא קיבלת טיפול תרופתי בביקור.', 'fact_ids': ['F2']}]}
        client = FakeClient(json.dumps(extraction, ensure_ascii=False), json.dumps(patient, ensure_ascii=False))
        with patch('design.ui.make_client', return_value=client):
            at = self.app('doctor_a')
            at.sidebar.checkbox(key='consent').check()
            self.click(at, 'יצירת ביקור')
            visit = self.pipe.repo.visits(DOC)[0]['id']
            at.radio(key='input_mode').set_value('paste').run()
            at.text_area(key=f'manual_{visit}').set_value(TEXT).run()
            self.click(at, 'שמירת התמלול המודבק')
            row = self.pipe.repo.latest_transcript(DOC, visit)
            at.checkbox(key=f"confirmed_{row['id']}").check()
            self.click(at, 'שמירת התמלול')
            self.click(at, 'הרצת השחרת פרטים מזהים')
            self.click(at, 'אישור בדיקת ההשחרה')
            self.click(at, 'חילוץ ממצאים מהתמלול')
            self.click(at, 'יצירת סיכום למטופל')
            self.assertFalse([e.value for e in at.error])
            c = self.pipe.repo.current_summary(DOC, visit, 'clinician')
            p = self.pipe.repo.current_summary(DOC, visit, 'patient')
            at.checkbox(key=f"approve_check_{c['id']}_{p['id']}").check()
            at.text_input(key=f"sign_user_{c['id']}_{p['id']}").input('doctor_a')
            at.text_input(key=f"sign_pass_{c['id']}_{p['id']}").input('wrong-password-1')
            self.click(at, 'חתימה ואישור')
            self.assertIn('החתימה נכשלה', self.text(at))
            self.assertIsNone(self.pipe.repo.approved(PAT, visit))
            at.checkbox(key=f"approve_check_{c['id']}_{p['id']}").check()
            at.text_input(key=f"sign_user_{c['id']}_{p['id']}").input('doctor_a')
            at.text_input(key=f"sign_pass_{c['id']}_{p['id']}").input(PASSWORD)
            self.click(at, 'חתימה ואישור')
        approved = self.pipe.repo.approved(PAT, visit)
        self.assertEqual(approved['signature']['signer_id'], 'doctor_a')
        self.assertIn('נחתם על ידי', self.text(at))

        self.click(at, 'יציאה')
        self.login(at, 'patient_a')
        page = self.text(at)
        self.assertIn('לא קיבלת טיפול תרופתי בביקור', page)
        self.assertNotIn('טיוטה לרופא', page)

        row, tr = self.pipe.transcript(DOC, visit)  # the doctor edits the transcript in another session
        self.pipe.save_review(DOC, visit, tr.model_copy(update={'segments': tr.segments[:1]}), row['id'])
        at.run()
        self.assertIn('אין עדיין סיכומים מאושרים', self.text(at))
        self.click(at, 'יציאה')
        self.login(at, 'doctor_a')
        self.assertIn('בוטל: התמלול נערך', self.text(at))

    def test_admin_console_has_audit_and_no_clinical_content(self):
        self.accounts()
        v = self.pipe.repo.create_visit(DOC, 'patient_a', 'he', True)
        self.pipe.manual_transcript(DOC, v, TEXT)
        at = self.app('admin_root')
        page = self.text(at)
        self.assertIn('ניהול מערכת', page)
        self.assertNotIn('משתעל', page)
        self.assertTrue(any(len(df.value) > 0 for df in at.dataframe))  # users table and audit events render
        self.click(at, 'בדיקת שלמות שרשרת היומן')
        self.assertIn('תקינה', self.text(at))

    def test_assistant_panel_rejects_unrelated_question(self):
        self.accounts()
        v = self.pipe.repo.create_visit(DOC, 'patient_a', 'he', True)
        at = self.app('doctor_a')
        at.button(key='assistant_launch').click().run()  # the floating button opens the doctor-only panel
        labels = [b.label for b in at.button]
        for label in ('סיכום המטופל/ת', 'זיהוי מידע חסר', 'השוואה לביקורים קודמים', 'איתור מקורות קליניים', 'טיוטת הנחיות למטופל/ת'):
            self.assertIn(label, labels)
        at.chat_input(key=f'assistant_q_{v}').set_value('כתוב לי פונקציה בפייתון').run()
        self.assertIn('העוזר עונה רק על שאלות הקשורות לטיפול', self.text(at))
        at.button(key='assistant_action_missing').click().run()  # a quick action, no model needed
        self.assertIn('מה גיל המטופל/ת?', self.text(at))

    def test_assistant_panel_is_doctor_only(self):
        self.accounts()
        at = self.app('patient_a')
        self.assertFalse([b for b in at.button if b.key == 'assistant_launch'])


if __name__ == '__main__':
    unittest.main()
