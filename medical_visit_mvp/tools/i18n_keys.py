"""List every Hebrew interface string that must be in backend/locales.json.

Usage (from medical_visit_mvp):  ..\\venv\\Scripts\\python.exe tools\\i18n_keys.py [--missing]

Collected: literal first arguments of t(), T(), self.T(), translate(); the Hebrew values of the label maps and
message constants rendered through the translators; static backend error messages (raise ...("...")); and the parts
of system-built rows translated by backend.i18n.localize. Regex patterns, model prompts and docstrings are skipped.
"""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEB = re.compile(r"[֐-׿]")
CALLS = {"t", "T", "translate"}
# Module-level constants whose (Hebrew) values are rendered through a translator.
CONSTANTS = {
    "backend/schemas.py": {"CATEGORY_LABELS", "STATUS_LABELS", "ROLE_LABELS"},
    "backend/assistant.py": {"CLARIFY", "EXAMPLES", "SECTION_TEMPLATES", "FIELD_LABELS", "VALUE_LABELS", "MISSING_QUESTIONS"},
    "backend/assistant_actions.py": {"ACTIONS", "CATEGORY_QUESTIONS", "NOT_STRUCTURED"},
    "backend/export.py": {"HEADER", "PROTOTYPE", "PATIENT_GROUPS", "SYSTEM_NAME"},
    "backend/repository.py": {"SIGNATURE_MEANING"},
    "design/theme.py": {"SYSTEM_NAME", "SYSTEM_TAGLINE", "ROLE_NAMES"},
    "design/admin.py": {"ROLES", "OUTCOMES", "ACTIVE"},
    "design/doctor.py": {"SPOKEN_LANGUAGES", "VISIT_LANGUAGES", "SUMMARY_LANGUAGES", "INPUT_SOURCES", "STATE_LABELS", "ORIGIN_LABELS",
                         "REVOKE_LABELS", "CONTEXT_OPTIONS", "CONTEXT_LABELS"},
    "design/editors.py": {"MED_FIELDS", "ISSUE_MESSAGES"},
    "design/assistant_panel.py": {"KINDS", "SAFETY", "DECISIONS", "MISSING_LABELS", "SECTIONS"},
}
SCAN_CALLS = ["design/*.py", "backend/assistant.py", "backend/assistant_actions.py", "backend/export.py", "backend/summarizer.py"]
SCAN_RAISES = ["backend/*.py", "design/*.py"]
# Strings built by code and localized as "<label>: <value>" (backend.i18n.localize) or set as facts_note.
EXTRA = ["רשומות", "לא נרשמו", "לא ידוע (לא נבדק/לא אושר — אינו שלילה)", "לא נאמר בביקור", "עובדות הביקור",
         "אין עדיין תמלול לביקור", "התמלול עוד לא עבר אישור דוברים ובדיקת השחרה (שלב 2)", "אין עדיין טיוטת רופא עדכנית (שלב 3)",
         "מחלות רקע ידועות", "אין (אושר)", "כן", "לא (אושר)"]


def strings_in(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and HEB.search(n.value):
            yield n.value


def collect():
    keys = set(EXTRA)
    for pattern in SCAN_CALLS:
        for path in ROOT.glob(pattern):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and n.args:
                    name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                    if name in CALLS:
                        keys |= set(strings_in(n.args[0]))
    for rel, names in CONSTANTS.items():
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for n in tree.body:
            targets = [t.id for t in getattr(n, "targets", []) if isinstance(t, ast.Name)]
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                targets = [n.target.id]
            if set(targets) & names:
                keys |= set(strings_in(n.value))
    for pattern in SCAN_RAISES:
        for path in ROOT.glob(pattern):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and n.exc.args:
                    arg = n.exc.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and HEB.search(arg.value):
                        keys.add(arg.value)
    return sorted(keys)


if __name__ == "__main__":
    keys = collect()
    if "--missing" in sys.argv:
        catalog = json.loads((ROOT / "backend" / "locales.json").read_text(encoding="utf-8")) if (ROOT / "backend" / "locales.json").exists() else {}
        keys = [k for k in keys if not (catalog.get(k) or {}).get("ar") or not (catalog.get(k) or {}).get("en")]
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(keys, ensure_ascii=False, indent=0))
    print(len(keys), file=sys.stderr)
