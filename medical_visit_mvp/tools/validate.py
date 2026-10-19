"""Computer system validation run: Installation Qualification (IQ) and Operational Qualification (OQ).

    ..\\venv\\Scripts\\python.exe -m tools.validate [--executed-by "<name>"]

IQ checks the installed environment against the specification in data/documents/compliance/VALIDATION_PLAN.md:
Python and package versions, schema revision, data-key protection, audit-chain integrity and a SHA-256 manifest of
every source file (so a later run shows exactly what changed). OQ runs the automated test suite, whose test cases
are traced to requirements in VALIDATION_PLAN.md. The report is written to data/validation/ as Markdown and JSON;
the reviewer signs the Markdown report by hand (PQ and approval are manual steps in the plan).
Exit code 0 only if every IQ check and every OQ test passed.
"""
import argparse
import hashlib
import io
import json
import platform
import sys
import time
import unittest
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "validation"
REQUIRED = {"streamlit": "1.44", "sqlalchemy": "2.0", "alembic": "1.13", "pydantic": "2", "cryptography": "42", "fastapi": "0.110",
            "fpdf2": "2.7"}
SOURCES = ["app.py", "backend/*.py", "backend/migrations/versions/*.py", "design/*.py", "design/*.css", "backend/locales.json",
           "requirements.txt"]


def version_ok(installed: str, minimum: str) -> bool:
    def parts(v):
        return [int(x) for x in "".join(c if c.isdigit() or c == "." else " " for c in v).split()[0].split(".") if x.isdigit()]
    return parts(installed) >= parts(minimum)


def iq() -> list[dict]:
    from backend import crypto, db
    checks = [{"check": "Python >= 3.11", "result": platform.python_version(), "ok": sys.version_info >= (3, 11)}]
    for pkg, minimum in REQUIRED.items():
        try:
            v = metadata.version(pkg)
            checks.append({"check": f"{pkg} >= {minimum}", "result": v, "ok": version_ok(v, minimum)})
        except metadata.PackageNotFoundError:
            checks.append({"check": f"{pkg} >= {minimum}", "result": "not installed", "ok": False})
    if db.DB_PATH.exists():
        rev = db.current_revision(db.DB_PATH)
        checks.append({"check": "database schema at head revision", "result": f"{rev} (head {db.head_revision()})",
                       "ok": rev == db.head_revision()})
        from sqlalchemy.orm import Session
        from backend import audit
        eng = db.engine(db.DB_PATH, immediate=False)
        try:
            with Session(eng) as s:
                chain = audit.verify(s)
        finally:
            eng.dispose()
        checks.append({"check": "audit trail hash chain verifies", "result": f"{chain['checked']} events", "ok": chain["ok"]})
    else:
        checks.append({"check": "database present", "result": "not created yet (created on first start)", "ok": True})
    try:
        crypto.decrypt(crypto.encrypt("validation"))
        checks.append({"check": "data encryption key available and protected", "result": crypto.key_protection(), "ok": True})
    except Exception as exc:  # noqa: BLE001 - reported as a failed check
        checks.append({"check": "data encryption key available and protected", "result": str(exc), "ok": False})
    clock = crypto.clock_check()
    checks.append({"check": "system clock within tolerance of trusted time", "result": clock["source"] +
                   (f", offset {clock['offset_seconds']} s" if clock["verified"] else ""),
                   "ok": not clock["verified"] or abs(clock["offset_seconds"]) <= crypto.MAX_CLOCK_SKEW_SECONDS})
    return checks


def manifest() -> dict:
    files = sorted({p for pattern in SOURCES for p in ROOT.glob(pattern) if p.is_file()})
    return {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def oq() -> dict:
    names = sorted(f"tests.{p.stem}" for p in (ROOT / "tests").glob("test_*.py"))  # tests/ is a namespace package
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    lines = [ln for ln in stream.getvalue().splitlines() if " ... " in ln]
    return {"run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
            "ok": result.wasSuccessful(), "cases": lines}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m tools.validate")
    parser.add_argument("--executed-by", default="")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    iq_checks, files = iq(), manifest()
    oq_result = oq()
    ok = all(c["ok"] for c in iq_checks) and oq_result["ok"]
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = started.replace(":", "").replace("-", "")
    report = {"started_utc": started, "executed_by": args.executed_by, "host": platform.node(), "platform": platform.platform(),
              "result": "PASS" if ok else "FAIL", "iq": iq_checks, "oq": {k: v for k, v in oq_result.items() if k != "cases"},
              "oq_cases": oq_result["cases"], "source_manifest_sha256": files}
    (OUT / f"validation-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [f"# Validation run {started}", "", f"Result: **{report['result']}** · executed by: {args.executed_by or '—'} · host: {report['host']}",
          "", "## Installation Qualification (IQ)", "", "| Check | Result | Pass |", "|---|---|---|"]
    md += [f"| {c['check']} | {c['result']} | {'✓' if c['ok'] else '✗'} |" for c in iq_checks]
    md += ["", "## Operational Qualification (OQ)", "",
           f"{oq_result['run']} automated test cases · failures {oq_result['failures']} · errors {oq_result['errors']} · skipped {oq_result['skipped']}",
           "", "```", *oq_result["cases"], "```", "",
           "## Source manifest", "", f"{len(files)} files; SHA-256 per file in the JSON report. Combined SHA-256: "
           + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "",
           "## Review and approval", "", "Reviewed by (name, role): ____________________  Date: __________  Signature: ____________",
           "", "Quality / system owner approval: ____________________  Date: __________  Signature: ____________", ""]
    (OUT / f"validation-{stamp}.md").write_text("\n".join(md), encoding="utf-8")
    print(f"{report['result']}: IQ {sum(c['ok'] for c in iq_checks)}/{len(iq_checks)}, OQ {oq_result['run']} tests "
          f"({oq_result['failures']} failures, {oq_result['errors']} errors) -> {OUT / f'validation-{stamp}.md'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
