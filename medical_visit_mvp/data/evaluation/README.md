# Evaluation

- `evaluate_assistant.py` — runs the clinical assistant end-to-end against the **real local model** on the fictional
  cases in `data/samples/assistant_cases.json` and checks the safeguards (relevance gate, citation validity, dose
  removal, allergy/interaction/pregnancy checks, withholding when information is missing).
- `results/` — the actual outputs of each run (JSON with every response + a Markdown table), named by model and UTC time.

A passing run shows that the safeguards behaved as specified on these fictional inputs with that model. It is not
evidence of clinical accuracy, completeness or safety. Clinical evaluation by qualified reviewers (see
`data/documents/CLINICAL_ASSISTANT.md`, section "Evaluation") is still pending.
