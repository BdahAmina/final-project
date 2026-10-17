"""Speaker diarization (who spoke when) and word/segment alignment.

Diarization yields anonymous labels (SPEAKER_00, ...). It never decides who
is the doctor; the clinician maps labels to roles in the review screen.
"""
import importlib.util
import logging
import os
from functools import lru_cache

LOG = logging.getLogger(__name__)
MODEL_ID = "pyannote/speaker-diarization-community-1"
OVERLAP_SHARE = 0.3
SETUP_HELP = ("הפרדת דוברים אינה זמינה. נדרשים: (1) התקנת pyannote.audio ו־torch ב־venv; "
              "(2) חשבון Hugging Face שאישר את תנאי המודל " + MODEL_ID + "; "
              "(3) HF_TOKEN בקובץ .env. עד אז אפשר לשייך דוברים ידנית בעריכת התמלול.")


class DiarizationUnavailable(Exception):
    pass


def ready():
    """Cheap check (no import, no model load) for showing the option in the UI: (ok, message)."""
    if importlib.util.find_spec("pyannote") is None or importlib.util.find_spec("torch") is None:
        return False, SETUP_HELP
    if not os.getenv("HF_TOKEN", "").strip():
        return False, SETUP_HELP
    return True, ""


@lru_cache(maxsize=1)
def _pipeline():
    token = os.getenv("HF_TOKEN", "").strip()
    if not ready()[0]:
        raise DiarizationUnavailable(SETUP_HELP)  # expected when not installed: no warning log
    try:
        from pyannote.audio import Pipeline
    except Exception as exc:
        LOG.warning("Diarization import failed: error=%s", type(exc).__name__)
        raise DiarizationUnavailable(SETUP_HELP) from None
    if not token:
        raise DiarizationUnavailable(SETUP_HELP)
    try:
        return Pipeline.from_pretrained(MODEL_ID, token=token)
    except Exception as exc:
        LOG.warning("Diarization model load failed: error=%s", type(exc).__name__)
        raise DiarizationUnavailable("טעינת מודל הפרדת הדוברים נכשלה. ודאו שה־HF_TOKEN תקף, שאישרתם את תנאי המודל בדף שלו ושיש חיבור אינטרנט בהורדה הראשונה.") from None


def available():
    try:
        _pipeline()
        return True, ""
    except DiarizationUnavailable as exc:
        return False, str(exc)


def diarize(audio, sample_rate=16000, max_speakers=3):
    """Return [(start, end, speaker_label)] for a mono float32 numpy array."""
    pipeline = _pipeline()
    import torch
    output = pipeline({"waveform": torch.from_numpy(audio).float()[None], "sample_rate": sample_rate},
                      min_speakers=1, max_speakers=max_speakers)
    annotation = getattr(output, "speaker_diarization", output)
    return [(float(t.start), float(t.end), str(label)) for t, _, label in annotation.itertracks(yield_label=True)]


def _overlaps(start, end, turns):
    totals = {}
    for t_start, t_end, label in turns:
        o = min(end, t_end) - max(start, t_start)
        if o > 0:
            totals[label] = totals.get(label, 0.0) + o
    return sorted(totals.items(), key=lambda kv: -kv[1])


def _speaker_for(start, end, turns, tolerance=0.5):
    ranked = _overlaps(start, end, turns)
    duration = max(end - start, 1e-3)
    if ranked:
        overlap = len(ranked) > 1 and ranked[1][1] / duration >= OVERLAP_SHARE
        return ranked[0][0], overlap
    mid = (start + end) / 2
    near = min(turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])), default=None)
    if near and min(abs(mid - near[0]), abs(mid - near[1])) <= tolerance:
        return near[2], False
    return "UNKNOWN", False


def align(segments, turns):
    """Assign speakers to transcription segments, splitting at speaker changes.

    segments: list of dicts with start, end, text and optional words [{start,end,text}].
    Returns new segment dicts with ids S1..Sn, speaker labels and review flags.
    """
    talk = {}
    for start, end, label in turns:
        talk[label] = talk.get(label, 0.0) + end - start
    main = {label for label, _ in sorted(talk.items(), key=lambda kv: -kv[1])[:2]}
    pieces = []
    for seg in segments:
        words = seg.get("words") or []
        if not words:
            speaker, overlap = _speaker_for(seg["start"], seg["end"], turns)
            pieces.append({"start": seg["start"], "end": seg["end"], "speaker": speaker, "words": [], "text": seg["text"], "overlap": overlap})
            continue
        current = None
        for w in words:
            speaker, overlap = _speaker_for(w["start"], w["end"], turns)
            if current is None or speaker != current["speaker"]:
                current = {"start": w["start"], "end": w["end"], "speaker": speaker, "words": [], "overlap": False}
                pieces.append(current)
            current["words"].append(w)
            current["end"] = w["end"]
            current["overlap"] |= overlap
        for p in pieces:
            if "text" not in p:
                p["text"] = "".join(w["text"] for w in p["words"]).strip()
    out = []
    for n, p in enumerate((p for p in pieces if p["text"]), 1):
        flags = []
        if p["overlap"]:
            flags.append("overlap")
        if p["speaker"] == "UNKNOWN":
            flags.append("unassigned")
        elif main and p["speaker"] not in main:
            flags.append("third_speaker")
        out.append({"id": f"S{n}", "start": p["start"], "end": p["end"], "speaker": p["speaker"],
                    "text": p["text"], "words": [{"start": w["start"], "end": w["end"], "text": w["text"]} for w in p["words"]],
                    "flags": flags})
    return out
