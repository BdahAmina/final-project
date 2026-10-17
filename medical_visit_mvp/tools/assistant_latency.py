"""Time one clinical-assistant question end to end on a FICTIONAL case (throw-away database, local Ollama).

Usage (from medical_visit_mvp):
  ..\\venv\\Scripts\\python.exe tools\\assistant_latency.py [model] ["question"]
Default model: the assistant's automatic choice (qwen3:4b when installed). Prints one JSON line.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data" / "evaluation"))
from backend import security

security.PBKDF2_ITERATIONS = 1_000  # throw-away database only
from backend import assistant as A
from backend.knowledge import load
from backend.llm_client import OllamaClient
from backend.pipeline import Pipeline
from backend.repository import PseudonymStore, Repository
from evaluate_assistant import Fixed

PW = "latency-tool-only-pw-1"


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else A.assistant_model()
    case = json.loads((ROOT / "data/samples/assistant_cases.json").read_text(encoding="utf-8"))["cases"][0]
    question = sys.argv[2] if len(sys.argv) > 2 else case["questions"][0]["q"]
    with tempfile.TemporaryDirectory() as d:
        repo, store = Repository(Path(d) / "x.sqlite"), PseudonymStore(Path(d) / "m.sqlite")
        try:
            pipe = Pipeline(repo, store, Path(d) / "t")
            adm, doc = {"id": "adm", "role": "admin"}, {"id": "doc", "role": "doctor"}
            repo.bootstrap_admin("adm", "a", PW)
            repo.create_user(adm, "doc", "doctor", "d", PW)
            repo.create_user(adm, "pat", "patient", "p", PW)
            repo.assign(adm, "doc", "pat", "latency")
            v = repo.create_visit(doc, "pat", "he", True)
            pipe.manual_transcript(doc, v, case["transcript"])
            row, tr = pipe.transcript(doc, v)
            pipe.save_review(doc, v, tr.model_copy(update={"roles_confirmed": True}), row["id"])
            pipe.redact(doc, v)
            row, tr = pipe.transcript(doc, v)
            pipe.confirm_redaction(doc, v, tr, row["id"])
            pipe.extract(doc, v, Fixed(case["facts"]))
            repo.add_patient_context(doc, v, case["context"])
            client = OllamaClient(model=model, num_ctx=4096, num_predict=500)
            calls, orig = [], client._call

            def timed(system, user, schema):
                t = time.monotonic()
                out = orig(system, user, schema)
                calls.append({"seconds": round(time.monotonic() - t, 1), "input_chars": len(system) + len(user), "output_chars": len(out)})
                return out
            client._call = timed
            asst = A.ClinicalAssistant(repo, load(), client_factory=lambda: client)
            t = time.monotonic()
            preview = asst.preview(doc, v, question)
            preview_s = round(time.monotonic() - t, 3)
            t = time.monotonic()
            r = asst.ask(doc, v, question)
            print(json.dumps({"model": model, "question": question, "instant_preview_seconds": preview_s,
                              "total_seconds": round(time.monotonic() - t, 1), "model_calls": calls, "status": r["status"],
                              "suggestions": [{"kind": s["kind"], "text": s["text"], "medication": s["medication"],
                                               "citations": [c["passage_id"] for c in s["citations"]],
                                               "safety": s["safety"] and s["safety"]["status"]} for s in r["suggestions"]],
                              "removed": [x["reason"] for x in r["removed"]], "clarification_questions": r["clarification_questions"],
                              "named_medication_checks": [(c["medication"], c["status"]) for c in r.get("named_medication_checks", [])]},
                             ensure_ascii=False))
        finally:
            repo.dispose()
            store.dispose()


if __name__ == "__main__":
    main()
