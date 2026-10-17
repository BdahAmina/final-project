"""Transcribe browser recordings without permanently saving audio."""
from functools import lru_cache
from io import BytesIO
import logging
from pathlib import Path
import traceback
import wave

MAX_BYTES = 24 * 1024 * 1024
MAX_SECONDS = 600

class AudioInputError(ValueError):
    """The recording itself is invalid; retrying will not help."""

def check_audio(data, filename):
    if not data or len(data) > MAX_BYTES:
        raise AudioInputError("נדרשת הקלטה עד 24MB. הקליטו שיחה קצרה יותר.")
    if filename.lower().endswith('.wav'):
        try:
            with wave.open(BytesIO(data)) as recording:
                duration = recording.getnframes() / recording.getframerate()
                if duration < 1 or duration > MAX_SECONDS:
                    raise AudioInputError("אורך הקלטת WAV צריך להיות בין שנייה לעשר דקות.")
                if not any(recording.readframes(recording.getnframes())):
                    raise AudioInputError("ההקלטה שקטה לחלוטין. בדקו את המיקרופון והקליטו שוב.")
        except (wave.Error, EOFError):
            raise AudioInputError("קובץ WAV אינו תקין. הקליטו מחדש או העלו קובץ נתמך.") from None

LOG = logging.getLogger(__name__)
SAMPLE_RATE = 16000
VAD_NOTICE = "סינון השקט (VAD) נכשל במחשב זה, ולכן התמלול בוצע מחדש מהאודיו המקורי ללא VAD. בדקו גם את הקטעים השקטים: ייתכנו בהם מילים שגויות או טקסט שלא נאמר. ההקלטה לא נשלחה לענן."

def log_failure(stage, exc):
    # Only the stage, exception type and code location: never audio, transcript text or keys.
    frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
    where = f" at {Path(frame.filename).name}:{frame.lineno}" if frame else ""
    LOG.warning("Local transcription failed: stage=%s error=%s%s", stage, type(exc).__name__, where)

def is_vad_failure(exc):
    files = [f.filename.replace("\\", "/") for f in traceback.extract_tb(exc.__traceback__)]
    return any(f.endswith("faster_whisper/vad.py") or "/onnxruntime/" in f for f in files) or type(exc).__module__.startswith("onnxruntime")

@lru_cache(maxsize=1)
def local_model(name):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        log_failure("import", exc)
        if isinstance(exc, ModuleNotFoundError) and exc.name == "faster_whisper":
            raise ValueError("חסרה חבילת faster-whisper. התקינו את requirements.txt עם ה־python של venv.") from None
        raise ValueError(f"טעינת רכיבי התמלול המקומי נכשלה ({type(exc).__name__}). ב־Windows ייתכן שחסר DLL: התקינו Microsoft Visual C++ Redistributable x64 והתקינו מחדש את requirements.txt.") from None
    except OSError as exc:
        log_failure("import", exc)
        raise ValueError(f"טעינת DLL של התמלול המקומי נכשלה ({type(exc).__name__}). התקינו Microsoft Visual C++ Redistributable x64 והתקינו מחדש את requirements.txt.") from None
    try:
        return WhisperModel(name, device="cpu", compute_type="int8")
    except Exception as exc:
        log_failure("model", exc)
        raise ValueError(f"לא ניתן לטעון את מודל התמלול '{name}' ({type(exc).__name__}). בהפעלה הראשונה נדרש אינטרנט להורדת המודל מ־Hugging Face; בדקו חיבור ומקום פנוי בדיסק ונסו שוב, או בחרו מודל small.") from None

def decode(data):
    try:
        from faster_whisper.audio import decode_audio
        return decode_audio(BytesIO(data), sampling_rate=SAMPLE_RATE)
    except TypeError as exc:
        log_failure("decode", exc)
        if "metadata_errors" in str(exc):
            raise ValueError("גרסת PyAV (חבילת av) אינה תואמת ל־faster-whisper. התקינו את requirements.txt מחדש, או הריצו: python -m pip install av==18.1.0") from None
        raise AudioInputError(f"פענוח ההקלטה נכשל ({type(exc).__name__}). הקליטו שוב או העלו WAV.") from None
    except Exception as exc:
        log_failure("decode", exc)
        raise AudioInputError(f"פענוח ההקלטה נכשל ({type(exc).__name__}). ודאו שהקובץ הוא הקלטת אודיו תקינה, או הקליטו שוב.") from None

def transcribe_local(data, filename, language=None, model_name="small", audio=None):
    check_audio(data, filename)
    audio = decode(data) if audio is None else audio
    if len(audio) / SAMPLE_RATE > MAX_SECONDS:
        raise AudioInputError("ההקלטה ארוכה מעשר דקות. פצלו אותה לפני ההעלאה.")
    engine = local_model(model_name)
    options = dict(language=language, task="transcribe", beam_size=5, word_timestamps=True, condition_on_previous_text=False)
    notice = None
    try:
        try:
            segments, info = engine.transcribe(audio, vad_filter=True, **options)
        except Exception as exc:
            if not is_vad_failure(exc):
                raise
            log_failure("vad", exc)
            notice = VAD_NOTICE
            segments, info = engine.transcribe(audio, vad_filter=False, **options)
        rows = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip(),
                 "words": [{"start": float(w.start), "end": float(w.end), "text": w.word} for w in (getattr(s, "words", None) or [])]}
                for s in segments if s.text.strip()]
    except Exception as exc:
        log_failure("transcribe", exc)
        raise ValueError(f"התמלול המקומי נכשל ({type(exc).__name__}). נסו שוב או בחרו מודל small; פרטים טכניים ללא תוכן השיחה מופיעים בחלון הטרמינל.") from None
    result = finish(rows, info.language)
    if notice:
        result["notice"] = notice
    return result

def finish(rows, language):
    text = "\n".join(s['text'] for s in rows)
    if not text.strip():
        raise AudioInputError("לא זוהה דיבור. נסו הקלטה ברורה יותר ובדקו הרשאת מיקרופון.")
    for n, row in enumerate(rows, 1):
        row.setdefault("id", f"S{n}")
        row.setdefault("words", [])
    return {"text": text, "segments": rows, "language": language}
