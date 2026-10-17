# עוזר קליני לרופא — doctor-only clinical support agent

**Educational prototype evaluated with fictional patients.** It helps the doctor think through the current patient's
case: clarification questions, missing information, possible next steps and label-supported medication options for
the doctor's consideration. It does not diagnose, prescribe, order, dose, or send anything to the patient.

## Intended use (FDA CDS guidance, Criterion 4 (a)-(d))

| Item | Statement |
|---|---|
| Intended user | Licensed doctor assigned to the patient (role `doctor` + active care assignment). Not patients, not admins. |
| Intended population | Fictional patients in an educational setting. Not validated for any real population. |
| Not intended for | Critical or time-sensitive decisions, emergencies, autonomous diagnosis, prescribing, dosing, patient-facing advice. |
| Required inputs and how to obtain them | (1) The reviewed transcript (speaker roles confirmed, redaction reviewed); (2) the current clinician summary — facts with blocking issues are excluded; (3) the doctor-entered patient context: age, sex, pregnancy, lactation, allergies, current medications, renal and hepatic status, known conditions. Each context field is entered by the doctor and stays "unknown" until confirmed; unknown is never treated as negative. |
| Basis of the output | Verbatim passages retrieved from the curated collection (`data/knowledge`, currently three U.S. FDA labels) plus the visit's own records. A local LLM (Ollama `qwen3:8b`) drafts; deterministic code validates. |
| Algorithm validation | Automated safeguard tests (mocked model) and a real-model run on fictional cases (below). **No clinical validation has been done.** |
| Patient-specific knowns/unknowns | Every answer lists known facts with their source record and version, missing information, uncertainties, blocked sources and items removed by validation. |

Whether the function is a device under FD&C Act §520(o)(1)(E) has not been determined. Design choices follow the
non-device criteria (HCP user, recommendations about care, ability to independently review the basis), and FDA's
note on automation bias (the tendency to over-rely on automated suggestions) informs the "draft only" labelling,
the explicit decision per suggestion and the mandatory signature.

## Architecture

```
doctor question ──► Repository.assistant_conversation (authorize doctor + bind to ONE visit)
                ──► rules_relevance: off-topic / other patient / other visit id / prompt-injection phrases → reject;
                    too short → ask how it relates to this patient  (no model call, nothing sent)
                ──► load_context: reviewed transcript gate, current clinician facts (non-blocking only),
                    doctor-entered PatientContext, context_version fingerprint
                ──► retrieve: Hebrew/Arabic/English → canonical terms (terms.json) → BM25 over verbatim passages;
                    a named medication always brings its label's boxed warning / indications / contraindications
                ──► instant evidence (no model, <0.2 s): label checks for every medication named in the question,
                    key verbatim passages, record summary — shown while the model works
                ──► Ollama (local, qwen3:4b by default) with JSON-schema output and length caps; evidence is sent as
                    numbered sentences (E1.s1…), the model only SELECTS sentences; Hebrew wording comes from reviewed
                    section templates (contraindication, warning, interaction, pregnancy, indication…)
                ──► validate (deterministic):
                      • model relevance "unrelated" → reject, "ambiguous" → clarify
                      • known facts: only real refs (F#, context.*); text taken from the record
                      • next step / treatment / medication: ≥1 citation to a passage retrieved for THIS question,
                        quote must be a verbatim substring (≥4 words)
                      • doses, frequencies, quantities → removed; numbers absent from context/quotes → removed
                      • evidence_conflict → specific treatment/medication options withheld
                      • medication option: canonical name in curated map, cited passage from that drug's label,
                        medication_safety(): allergy class match → blocked; label mentions of current meds →
                        flagged as "label text, not a verified interaction database"; pregnancy / lactation /
                        renal / hepatic / age unknown where relevant → option withheld + clarification questions
                ──► store response + suggestions with context_version; audit request/result (ids only)
doctor decision ──► accept / edit / reject per suggestion (audited) → accepted items appended to a new
                    doctor-authored PLAN version with provenance; transcript and summaries never change
signature       ──► approval binds clinician summary + patient summary + plan versions; patients see the plan only
                    after signature, labelled "plan the doctor added after the visit"
facts change    ──► new context_version: old suggestions marked stale (cannot be accepted), plan items flagged,
                    signing blocked until the doctor records a reassessment; an approval that included a plan is revoked
```

Files: `backend/assistant.py` (orchestration, relevance, validation, medication checks), `backend/knowledge.py`
(collection, term map, retrieval), `backend/repository.py` (conversation binding, suggestions, plan, staleness),
`design/assistant_panel.py` (UI), `tools/knowledge_ingest.py` (sources).

## Boundaries — what is enforced where

| Requirement | Enforcement | Test |
|---|---|---|
| Bound to one authorized visit | `assistant_conversation` checks doctor + care assignment; conversations keyed by (visit, doctor) | `test_assistant.ContextIsolationTests.test_assistant_requires_authorized_doctor` |
| Only that visit's reviewed context; reset on patient switch | Context loaded per call from the bound visit only; UI clears assistant state on visit change | `…test_context_is_bound_to_one_visit_and_switching_patient_resets_it` |
| Reject unrelated / other patient / injection | Rule gate before any model call; model relevance as second check | `test_assistant.RelevanceTests.*` |
| Ambiguous → ask relation to this patient | Rule (too short) and model `ambiguous` → fixed clarification | `…test_ambiguous_question_asks_for_relation_to_patient` |
| Prefer reviewed facts, mark source/version | Known facts rendered from records with summary/context version | `EvidenceTests.test_known_facts_cannot_be_invented` |
| No invented facts | Unknown refs removed; unsupported numbers removed | same + `test_doses_and_unsupported_numbers_are_removed` |
| Transcript/passages are data | Prompt + validation; injection-like visit text flagged in limitations | `…test_injection_inside_visit_text_is_data_and_unsupported_output_removed` |
| Citations exist and support | Retrieved-set membership + verbatim quote check | `EvidenceTests.test_invented_citation_or_quote_is_removed` |
| Absent / conflicting evidence → abstain | Empty retrieval → limitation, no options; `evidence_conflict` → options withheld | `…test_absent_references_lead_to_abstention`, `…test_conflicting_evidence_withholds_specific_recommendation` |
| Medication only with label support + context | Curated map + label-source citation + `medication_safety` | `MedicationSafetyTests.*` |
| Unknown ≠ negative | Unknown fields withhold the option and generate questions | `…test_unknown_is_not_treated_as_negative` |
| LLM interaction check not presented as verified | Flags say "label text, not a verified interaction database" | `…test_label_mention_of_current_medication_is_flagged_not_called_verified` |
| No individualized doses | Dose sections not ingested; dose patterns removed | `…test_doses_and_unsupported_numbers_are_removed` |
| Never a prescription | No prescription object exists; plan items are free text authored by the doctor | — |
| Draft label, accept/edit/reject, audited | `review_status=draft_for_doctor_review`; `decide_suggestion` | `ReviewAndStalenessTests.test_accept_edit_reject_create_plan_with_provenance_and_audit` |
| Separate from visit facts; patient sees plan only after signature | Plan versions separate from summaries | `…test_suggestions_never_change_recorded_visit_facts`, `…test_patient_sees_plan_only_after_doctor_approval_and_labelled_as_plan` |
| Stale after fact changes | `context_version` comparison | `…test_patient_fact_change_marks_suggestions_and_plan_stale`, `…test_approved_plan_is_revoked_when_patient_facts_change` |

Schema validation (Pydantic + JSON schema) only guarantees the shape of the output. It does not establish that a
suggestion is medically correct, complete, or appropriate for the patient.

## Risks and mitigations

| Risk | Mitigation | Residual |
|---|---|---|
| Automation bias | Draft labelling, per-suggestion decision, signature, visible basis and limitations | Cannot be eliminated by software |
| Hallucinated or misattributed citations | Retrieved-set + verbatim-quote validation | A verbatim quote can still be cited for a claim it does not fully support; lexical checks cannot judge meaning |
| Missing evidence (no guidelines) | Blocked sources disclosed in every answer; options require label citations | The assistant cannot judge whether a medication is indicated for a condition beyond label text |
| Jurisdiction mismatch | Every answer states sources are U.S. FDA labels | Israeli labels/registration not checked |
| Retrieval misses (lexical, term map) | Named medications pull their core label sections | Concepts not in `terms.json` are not found |
| Prompt injection via transcript or question | Rule gate, data-only prompt, output validation | Novel phrasing may pass the rule gate; validation limits what can be shown |
| Stale advice after facts change | context_version staleness, plan reassessment gate | Changes made outside the app are not detected |
| Interaction checking | Label-text mentions only, explicitly not a verified database | No real interaction database is integrated (blocked: none licensed/available) |
| Privacy | Local model only; audit stores ids, never question text | Questions and answers are stored in the database as clinical records |

## Panel, quick actions and Israeli sources (2026-10-17)

- **Panel**: a floating "עוזר קליני" button on every doctor screen opens a side drawer (`design/assistant_panel.py`).
  The doctor picks a patient, then one of that patient's visits (only visits of patients assigned to this doctor),
  and the answer language (עברית / العربية / English; Hebrew and Arabic right-to-left, English left-to-right).
  Switching patient or visit clears the panel state; conversations are stored per visit by the backend.
- **Quick actions** (`backend/assistant_actions.py`), each authorized, visit-bound, stored and audited like a question:
  summary (with record references), missing information, follow-up questions, documentation contradictions,
  comparison with previous visits of the same patient, clinical references, patient instructions.
  Summary / missing / contradictions / compare / references are deterministic (no model). Follow-up questions and
  patient instructions use the local model with validated output; patient instructions are rewordings of the
  doctor's own plan items, stored as pending suggestions, and reach the patient only after accept + signature.
- **Response sections**: relevant known facts, missing information, questions or suggestions, sources, uncertainties.
  Unknown context fields are always "missing", never known facts or negatives.
- **Direct answer**: the model may write a 2-5 sentence reply; it is shown only if it has no dose, no number and no
  medication name absent from the record and the retrieved sources, and no words in another alphabet.
- **Before transcript review** the assistant answers from doctor-entered context and sources only; no unreviewed
  transcript text is sent to the model, and the answer says the visit facts were not used.
- **Unavailable, marked as such**: drug-interaction checking (no validated interaction database: only label
  mentions are shown, labelled as such), structured test results (no field exists), Ministry guidance pages (gov.il
  blocks automated access; manual import path in `data/knowledge/README.md`).
- **Israeli sources**: doctor and consumer leaflets from the Ministry of Health medicines registry; see
  `data/knowledge/README.md` for URLs verified, allowlist, de-identified queries and document typing. Each citation
  shows organization, document type, update date, retrieval date and applicability notes (foreign jurisdiction,
  patient leaflet, pediatric product for an adult, outdated source).

## Evaluation

### Done
- Automated tests: `tests/test_assistant.py` (29 tests, scripted model, real retrieval over real passages) plus
  assistant API/UI tests. They verify safeguard behaviour only.
- Real-model run: `data/evaluation/evaluate_assistant.py` with Ollama `qwen3:8b` on the fictional cases in
  `data/samples/assistant_cases.json`. Results: see "Real-model results" below and `data/evaluation/results/`.

### Pending (required before any claim about clinical usefulness or safety)
1. A larger fictional case set written by clinicians, covering common presentations, contraindications, interactions,
   pregnancy, paediatrics, renal/hepatic impairment and adversarial prompts, with reference answers.
2. Blinded review of outputs by at least two qualified clinicians (pharmacist + physician) rating relevance,
   correctness, citation support, omission of important cautions and potential for harm; inter-rater agreement.
3. Ingestion and review of authoritative guidelines for the intended jurisdiction (e.g. Israeli MoH), with licensing
   checked; a verified drug-interaction source if interaction checking is to be relied on.
4. Usability study for automation bias with doctors; error analysis; monitoring plan.
5. Regulatory determination (device / non-device) and an institutional review before any use with real patients.

### Real-model results (fictional cases, 2026-10-17)

Model choice, measured on the same fictional question on this laptop (RTX 3050, 4 GB GPU memory):

| Configuration | Time per answer | Outcome |
|---|---|---|
| `qwen3:8b`, full prompt, model writes Hebrew (first version) | 189 s | answered, but "options" were questions and named aspirin without a label |
| `qwen3:8b`, trimmed prompt + length caps | 90–102 s | paraphrased quotes and invented references → all removed by validation |
| `qwen3:4b` (fits the GPU), selection-only output, Hebrew from reviewed templates | 17 s median (13–27 s) | valid citations, clean Hebrew |

`qwen3:8b` does not fit in 4 GB of GPU memory and generates about 4.4 tokens/s on the CPU; `qwen3:4b` is selected
automatically when installed (`OLLAMA_ASSISTANT_MODEL` overrides it). The instant evidence part of every answer
(label checks for named medications, key passages, record summary) needs no model and is shown in under 0.2 s.

`data/evaluation/evaluate_assistant.py qwen3:4b`, 10 questions across 6 fictional cases:

| Run | Passed | Errors | Notes |
|---|---|---|---|
| `assistant_eval_qwen3_4b_20261016T220443Z` | 6/10 | 3 | Found two real bugs: inserted verbatim sentences longer than 300 characters crashed validation; the generated rationale repeated the product title "500 mg". Both fixed, with a regression test. |
| `assistant_eval_qwen3_4b_20261016T220818Z` | **10/10** | 0 | Rule rejections (code, other patient, injection) instant; allergy case withheld amoxicillin; warfarin case flagged the label interaction mention; pregnancy case flagged the label pregnancy section; missing-context case asked for allergies, medications and age. |

A pass means the safeguards behaved as specified on these fictional inputs. It does not mean the answers are
clinically correct or complete; the pending clinical review above is still required.
