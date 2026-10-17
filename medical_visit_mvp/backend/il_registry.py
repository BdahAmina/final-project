"""Client for the Israeli Ministry of Health medicines registry (מאגר התרופות), used only by the curated ingest
(tools/knowledge_ingest.py), never at question time.

Verified 2026-10-17: israeldrugs.health.gov.il redirects to the Ministry's registries site
https://registries.health.gov.il/Medicines, whose own web page calls the JSON endpoints below; official documents
are served from the Ministry's public storage host. No separate public API documentation was found, so this client
uses exactly the requests that the official page makes, at a low rate.

Privacy: the only value ever sent is one active-ingredient name from the curated term map (data/knowledge/terms.json),
checked by `safe_query`. Patient names, IDs, transcripts and clinical details cannot reach this module's requests.
"""
import re
import time
from urllib.parse import quote, urlparse

import requests

REGISTRY_PAGE = "https://registries.health.gov.il/Medicines"
API = "https://registries.health.gov.il/api/Medicines/"
DOCS = "https://mohpublic.z6.web.core.windows.net/IsraelDrugs/"
ALLOWED_HOSTS = {"registries.health.gov.il", "mohpublic.z6.web.core.windows.net"}
ORGANIZATION = "משרד הבריאות — מאגר התרופות (אגף הרוקחות)"
UA = {"User-Agent": "medical-visit-mvp knowledge ingest (educational prototype; low-rate, ingredient names only)"}
PAUSE_SECONDS = 1.0
# Document types as labelled by the registry itself.
DOC_TYPES = {
    17: {"key": "doctor_leaflet", "label": "עלון לרופא", "audience": "professional"},
    18: {"key": "consumer_leaflet", "label": "עלון לצרכן", "audience": "patient_education"},
    54: {"key": "safety_update", "label": "החמרה לעלון", "audience": "professional"},
}
LANGUAGES = {"עברית": "he", "ערבית": "ar", "אנגלית": "en", None: "en"}
INGREDIENT = re.compile(r"^[A-Za-z][A-Za-z \-]{2,60}$")


class NotAllowed(ValueError):
    pass


def check_url(url: str) -> str:
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise NotAllowed(f"host not on the allowlist: {host or url}")
    return url


def safe_query(ingredient: str, vocabulary: set[str]) -> str:
    """The ingredient must be a plain Latin name present in the curated vocabulary: nothing else is ever sent."""
    name = (ingredient or "").strip()
    if not INGREDIENT.match(name) or name.lower() not in vocabulary:
        raise NotAllowed("query must be a curated active-ingredient name")
    return name


class Registry:
    def __init__(self, session=None, pause=PAUSE_SECONDS):
        self.http = session or requests.Session()
        self.pause = pause
        self.sent = []  # (method, url, payload) of every request, for tests and the ingest log

    def _request(self, method, url, **kw):
        check_url(url)
        self.sent.append((method, url, kw.get("json") or kw.get("params")))
        r = self.http.request(method, url, headers=UA, timeout=60, **kw)
        r.raise_for_status()
        time.sleep(self.pause)
        return r

    def search(self, ingredient: str, vocabulary: set[str]) -> list[dict]:
        """Active, human-use medicines whose active ingredients include `ingredient`."""
        name = safe_query(ingredient, vocabulary)
        body = {"searchText": name, "sortBy": "tradeNameHeb", "sortDirection": "asc", "maxResult": 50, "pageNumber": 0,
                "includeActive": True, "includeCanceled": False}
        rows = self._request("POST", API + "GetMedicines", json=body).json()["returnObject"]["medicinesList"]
        want = name.upper()
        return [m for m in rows if m.get("veterinaryUse") != "כן" and m.get("active") == 1
                and any(want in (c.get("componentDesc") or "").upper() for c in m.get("components") or [])]

    def documents(self, medicine_id: int) -> list[dict]:
        return self._request("GET", API + "GetMedicineDocuments", params={"medicineId": str(int(medicine_id))}).json()["returnObject"]

    def ingredient_names(self) -> list[str]:
        """All active-ingredient names known to the registry (English). Used only to *detect* drug mentions in model
        output; never as evidence. No query value is sent."""
        rows = self._request("GET", API + "GetComponents").json()["returnObject"]
        return sorted({(r.get("value") or "").strip().upper() for r in rows if (r.get("value") or "").strip()})

    def download(self, file_name: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.pdf", file_name or ""):
            raise NotAllowed("unexpected document file name")
        return self._request("GET", DOCS + quote(file_name)).content


def rank_products(medicines: list[dict], prefer_adult=True) -> list[dict]:
    """Representative products first: single active ingredient, then a non-pediatric name, then the most recent registration."""
    def rank(m):
        name = (m.get("tradeName") or "") + " " + (m.get("tradeNameHeb") or "")
        pediatric = bool(re.search(r"child|kid|infant|baby|pediatric|ילדים|תינוק", name, re.I))
        return (len(m.get("components") or []) != 1, prefer_adult and pediatric, -(int((m.get("regDate") or "0")[:4] or 0)))
    return sorted(medicines, key=rank)


def latest_documents(docs: list[dict], wanted=("doctor_leaflet", "consumer_leaflet")) -> list[dict]:
    """Newest document per (type, language), restricted to `wanted` registry types."""
    best = {}
    for d in docs:
        t = DOC_TYPES.get(d.get("docTypeCode"))
        if not t or t["key"] not in wanted or not d.get("fName"):
            continue
        key = (t["key"], LANGUAGES.get(d.get("language"), "en"))
        if key not in best or (d.get("updateDate") or "") > (best[key].get("updateDate") or ""):
            best[key] = d
    return [best[k] for k in sorted(best)]


def describe(doc: dict) -> dict:
    t = DOC_TYPES[doc["docTypeCode"]]
    return {"document_type": t["key"], "document_label": t["label"], "audience": t["audience"],
            "language": LANGUAGES.get(doc.get("language"), "en"), "url": DOCS + doc["fName"],
            "updated_at": (doc.get("updateDate") or "")[:10], "created_at": (doc.get("creationDate") or "")[:10]}
