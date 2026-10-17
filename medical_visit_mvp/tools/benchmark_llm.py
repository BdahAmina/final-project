"""Measure local Ollama load time, response time and memory on a transcript you provide.

Usage (from medical_visit_mvp):
  ..\\venv\\Scripts\\python.exe tools\\benchmark_llm.py <model> <transcript.txt>
Writes data/evaluation/results/llm_benchmark_<model>.json. Prints no transcript text.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import requests
from backend import redaction
from backend.llm_client import OllamaClient
from backend.pipeline import parse_manual
from backend.schemas import blocking_issues
from backend.summarizer import extract_facts, patient_summary


def ps(client):
    try:
        return requests.get(client.base_url + "/api/ps", timeout=5).json().get("models", [])
    except requests.RequestException:
        return []


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: benchmark_llm.py <model> <transcript.txt>  (lines start with 'רופא:' / 'מטופל:')")
    model, sample = sys.argv[1], Path(sys.argv[2])
    client = OllamaClient(model=model)
    ok, message = client.status()
    if not ok:
        sys.exit(message)
    requests.post(client.base_url + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=60)  # unload
    t = time.monotonic()
    requests.post(client.base_url + "/api/generate", json={"model": model, "prompt": "", "keep_alive": "10m"}, timeout=600)
    load_seconds = time.monotonic() - t
    loaded = ps(client)
    transcript = parse_manual(sample.read_text(encoding="utf-8")).model_copy(update={"roles_confirmed": True})
    transcript, _, _ = redaction.redact_transcript(transcript)
    runs = []
    for n in range(2):
        t = time.monotonic()
        draft = extract_facts(transcript, client, "Hebrew")
        extract_seconds = time.monotonic() - t
        t = time.monotonic()
        summary = patient_summary(draft, client, "Hebrew")
        patient_seconds = time.monotonic() - t
        runs.append({"run": n + 1, "extract_seconds": round(extract_seconds, 1), "patient_seconds": round(patient_seconds, 1),
                     "facts": len(draft.facts), "facts_with_blocking_issues": sum(any(i.blocking for i in f.issues) for f in draft.facts),
                     "issue_codes": sorted({i.code for f in draft.facts for i in f.issues}),
                     "missing_categories": draft.missing, "patient_items": len(summary.items),
                     "patient_blocking": len(blocking_issues(summary.items))})
    result = {"model": model, "sample": sample.name, "load_seconds": round(load_seconds, 1),
              "memory": [{"name": m.get("name"), "size_gb": round(m.get("size", 0) / 1e9, 2), "vram_gb": round(m.get("size_vram", 0) / 1e9, 2)} for m in loaded],
              "runs": runs, "measured_at": time.strftime("%Y-%m-%d %H:%M")}
    out = ROOT / "data" / "evaluation" / "results" / f"llm_benchmark_{model.replace(':', '_')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
