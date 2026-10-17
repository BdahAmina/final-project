# Curated clinical reference collection

The clinical assistant retrieves **only** from passages stored here. It never searches the web and the model is
instructed, and checked in code, to cite only passages retrieved for the current question with verbatim quotes.

| File | Content |
|---|---|
| `sources.json` | Source definitions: id, type, pinned version (DailyMed set id or URL), title, publisher, jurisdiction, population |
| `passages/<id>.json` | Verbatim passages with section path, the exact label version and effective date, retrieval time (UTC), SHA-256 of the download and of the passages |
| `manifest.json` | Per-source status (`active` / `blocked` + reason), versions and the collection version shown with every answer |
| `terms.json` | Hand-curated Hebrew/Arabic/English term map and medication classes used for retrieval and safety checks |

## Current sources (collection ingested 2026-10-16/17)

| Source | Version | Effective | Jurisdiction | Population | Status |
|---|---|---|---|---|---|
| Amoxicillin capsules — prescribing information (DailyMed set `1d978882-…`) | SPL 10 | 2026-10-06 | United States | Adults and pediatric patients per label | active |
| Ibuprofen tablets (Rx) — prescribing information (DailyMed set `8804564c-…`) | SPL 25 | 2026-10-07 | United States | Per label | active |
| Acetaminophen 500 mg (OTC) — Drug Facts (DailyMed set `c25bcfdc-…`) | SPL 5 | 2026-10-07 | United States | Adults and children 12+ | active |
| CDC Adult Outpatient Treatment Recommendations | — | — | United States | Adults, outpatient | **blocked** (HTTP 403 from this network) |

| Amoxicillin — Israeli registry leaflets (`il-moh-registry-amoxicillin`, product reg. 132 01 31050 00) | registry | 2025-10-17 | Israel | Per leaflet | active |
| Ibuprofen — Israeli registry leaflets (`il-moh-registry-ibuprofen`, reg. 145 20 32014 00) | registry | 2026-07-13 | Israel | Per leaflet | active |
| Paracetamol — Israeli registry leaflets (`il-moh-registry-acetaminophen`, reg. 181 53 38471 99) | registry | 2026-06-02 | Israel | Per leaflet (this product is a pediatric suspension; the assistant flags it for adults) | active |

Consequences, shown to the doctor with every answer:
- There is **no clinical practice guideline** in the collection, only medication labels and leaflets. The assistant
  can surface contraindications, warnings, interactions and population sections; it should not present
  guideline-level treatment recommendations.
- U.S. labels carry a note that Israeli registration and labels can differ; Israeli leaflets rank first.
- Dosage sections are deliberately not ingested; the assistant never produces doses.

## Israeli Ministry of Health sources (`backend/il_registry.py`)

Verified 2026-10-17:
- `https://israeldrugs.health.gov.il` redirects to the Ministry's registries site **https://registries.health.gov.il/Medicines**
  (מאגר התרופות). The page itself calls `https://registries.health.gov.il/api/Medicines/...` (GetMedicines,
  GetMedicineDocuments, GetComponents); official documents are served from `https://mohpublic.z6.web.core.windows.net/IsraelDrugs/`.
  No separate public API documentation was found, so the client uses exactly the requests the official page makes,
  at a low rate (1 s between requests), with an identifying User-Agent.
- **gov.il blocks automated access** (HTTP 403, also from a headless browser), so Ministry guidance pages and the
  gov.il terms of use could not be read automatically. Review the terms at
  https://www.gov.il/he/Departments/General/gov_terms_of_use yourself before relying on automated retrieval.

Rules enforced in code:
- Allowlist: `allowed_domains` in `sources.json`; the registry client accepts only `registries.health.gov.il` and
  `mohpublic.z6.web.core.windows.net` over HTTPS.
- De-identified queries: the only value ever sent is one active-ingredient name from `terms.json`
  (`il_registry.safe_query`). Retrieval at question time is offline (from `passages/`), so nothing about a patient
  is sent anywhere.
- Document types as labelled by the registry: **עלון לרופא** (doctor leaflet, `audience: professional`) and
  **עלון לצרכן** (consumer leaflet, `audience: patient_education`, Hebrew/Arabic/English). Consumer leaflets are
  never used as prescribing guidance (`Knowledge.search` / `label_passages` default to professional passages).
- Only clinical sections are kept (indications, contraindications, warnings, interactions, pregnancy/lactation,
  special populations, adverse effects; for consumer leaflets: what it is for, before use, side effects). Dosing,
  administration, overdose, storage and pharmaceutical sections are dropped.
- Every passage keeps its original language text verbatim and carries: document type, audience, language,
  document URL, registry page URL, product and registration number, document update date, retrieval date (UTC).
- `drug_names.json`: the registry's list of active-ingredient names (`--drug-names`), used **only** to detect
  medication names in model output that are not supported by the record or the sources. It is not evidence.

Commands (from `medical_visit_mvp`):
```powershell
..\venv\Scripts\python.exe tools\knowledge_ingest.py il-moh-registry-ibuprofen   # one Israeli source
..\venv\Scripts\python.exe tools\knowledge_ingest.py --drug-names                 # refresh the ingredient-name list
```

### Ministry guidance pages (manual import)
Because gov.il refuses automated access, a guidance document is added as a `manual_import` source:
1. Open the official page in a browser, confirm it is the Ministry's (gov.il / health.gov.il) and note its URL and date.
2. Add to `sources.json`: `{"id": "moh-…", "type": "manual_import", "url": "<official URL>", "title": "…",
   "publisher": "משרד הבריאות", "organization": "משרד הבריאות", "jurisdiction": "Israel", "population": "…",
   "audience": "professional" | "patient_education", "language": "he", "document_type": "clinical_guideline",
   "document_label": "חוזר מנהל רפואה", "updated_at": "YYYY-MM-DD", "medications": [], "status": "active"}`.
   Use `patient_education` for general information pages: they are never used as prescribing guidance.
3. Save the page or PDF from that URL and import it:
   `..\venv\Scripts\python.exe tools\knowledge_ingest.py --import moh-… C:\path\to\saved.pdf`
   The URL's domain must be on `allowed_domains`. Until imported, the source is listed as unavailable.
No guidance document is configured yet: none could be verified automatically.

## Adding or updating a source

1. Add an entry to `sources.json`. For a drug label use `"type": "dailymed_spl"` with the DailyMed `setid` of the
   exact label (find it at https://dailymed.nlm.nih.gov, "SET ID"). For a guideline page use `"type": "html"` with
   its URL. Fill in `title`, `publisher`, `jurisdiction`, `population` and, for labels, `medications`.
2. For a new medication, add it to `terms.json → medications` with Hebrew/Arabic/English synonyms, its classes
   (with the label sentence that justifies them in `basis`) and `label_source`.
3. Run the ingest (from `medical_visit_mvp`):
   ```powershell
   ..\venv\Scripts\python.exe tools\knowledge_ingest.py               # all sources
   ..\venv\Scripts\python.exe tools\knowledge_ingest.py <source-id>   # one source
   ```
   Check the printed status, review `passages/<id>.json` (section names and text), and confirm the new
   `collection_version` in `manifest.json`.
4. Run the tests (`..\venv\Scripts\python.exe -m unittest discover -s tests`) and the evaluation
   (`data\evaluation\evaluate_assistant.py`), then have a qualified clinician review what changed.
5. Updating = re-running the ingest. The DailyMed set id points to the latest label version; the file records which
   version was fetched. Suggestions made under an older collection keep their recorded citations.

Sources whose download fails (e.g. the CDC page, which returns HTTP 403 to automated requests from this network)
stay `blocked` and contribute nothing. Do not paste text produced by a chatbot or summariser as a substitute: only
verbatim text from the official source, with its URL and date, belongs here.
