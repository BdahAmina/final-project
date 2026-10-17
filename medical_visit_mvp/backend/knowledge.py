"""Curated reference collection: loading, bilingual term mapping and lexical (BM25) retrieval.

Only passages ingested by tools/knowledge_ingest.py from data/knowledge/sources.json are
retrievable. Retrieval is lexical: Hebrew/Arabic questions are mapped to English canonical terms
through data/knowledge/terms.json, so concepts missing from that map are not found. This is a
known limitation, documented in data/documents/CLINICAL_ASSISTANT.md.
"""
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

KNOWLEDGE = Path(__file__).resolve().parents[1] / "data" / "knowledge"
WORD = re.compile(r"[a-zα-ωβ0-9]+(?:-[a-z0-9]+)?|[֐-׿\"']+|[؀-ۿ]+", re.I)
STOP = set("the a an of and or to in on for with by is are be was were as at from that this these those it its may can "
           "not no if any other than such have has had who which when patients patient use used should been into "
           "more most also than there their them they you your".split())


NOT_DRUG_NAMES = {"water", "alcohol", "oxygen", "sucrose", "glucose", "sodium", "calcium", "potassium", "magnesium", "sodium chloride",
                  "iron", "zinc", "vitamin", "lactose", "glycerin", "glycerol", "nitrogen", "ethanol", "dextrose", "honey"}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[֑-ׇً-ٟ]", "", text)
    return text.replace("״", '"').replace("׳", "'")


def sentences(text: str, limit_chars: int = 700) -> list[str]:
    """Verbatim sentences of a passage (up to ~limit_chars). The model cites them by number; code inserts the text."""
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])", text or "") if s.strip()]
    out, used = [], 0
    for s in parts:
        if out and used + len(s) > limit_chars:
            break
        out.append(s)
        used += len(s)
    return out


def tokens(text: str) -> list[str]:
    out = []
    for t in WORD.findall(normalize(text)):
        t = t.strip("\"'")
        if len(t) > 2 and t not in STOP:
            out.append(t[:-1] if t.endswith("s") and len(t) > 4 and t.isascii() else t)
    return out


@dataclass
class Passage:
    id: str
    source_id: str
    section: str
    text: str
    meta: dict = field(repr=False)


class Knowledge:
    def __init__(self, root: Path = KNOWLEDGE):
        self.root = Path(root)
        self.config = json.loads((self.root / "sources.json").read_text(encoding="utf-8"))
        self.terms = json.loads((self.root / "terms.json").read_text(encoding="utf-8"))
        names = self.root / "drug_names.json"
        # Registry active-ingredient names, only for detecting drug mentions (not evidence). Common substances excluded.
        self.drug_names = {n.lower() for n in (json.loads(names.read_text(encoding="utf-8"))["names"] if names.exists() else [])
                           if len(n) >= 5 and n.lower() not in NOT_DRUG_NAMES}
        curated = {normalize(x) for m in self.terms["medications"].values() for x in m["synonyms"]}
        self.drug_names -= curated  # curated medications are matched (with their synonyms) by canonical_terms
        manifest = self.root / "manifest.json"
        self.manifest = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"sources": {}}
        self.sources, self.passages = {}, {}
        for src in self.config["sources"]:
            path = self.root / "passages" / f"{src['id']}.json"
            status = self.manifest["sources"].get(src["id"], {}).get("status", "not_ingested")
            if src.get("status") != "active" or not path.exists():
                self.sources[src["id"]] = src | {"status": "blocked" if status == "blocked" else "not_ingested",
                                                 "reason": self.manifest["sources"].get(src["id"], {}).get("reason", "")}
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            meta = {k: record.get(k) for k in ("id", "title", "publisher", "jurisdiction", "population", "version",
                                               "effective_date", "retrieved_at", "url", "raw_sha256", "organization")}
            # Sources without an audience (FDA labels, CDC) are professional references; per-passage metadata
            # (Israeli registry leaflets: document type, audience, language, document URL, update date) overrides it.
            meta |= {"audience": record.get("audience") or src.get("audience") or "professional",
                     "language": record.get("language") or src.get("language") or "en",
                     "organization": meta.get("organization") or record.get("publisher")}
            self.sources[src["id"]] = src | meta | {"status": "active"}
            for p in record["passages"]:
                self.passages[p["id"]] = Passage(p["id"], src["id"], p["section"], p["text"], meta | (p.get("meta") or {}))
        self._index()

    # ---- terms -----------------------------------------------------------------------------------------
    @property
    def collection_version(self):
        return self.manifest.get("collection_version", "none")

    def _synonym_table(self):
        """phrase -> every canonical term it denotes (e.g. "אספירין" is both the drug 'aspirin' and the class 'nsaid')."""
        table = {}
        groups = [{c: m["synonyms"] for c, m in self.terms["medications"].items()}] + \
                 [self.terms[g] for g in ("allergy_classes", "interaction_terms", "clinical_terms")]
        for group in groups:
            for canon, syns in group.items():
                for s in syns:
                    names = table.setdefault(normalize(s), [])
                    if canon not in names:
                        names.append(canon)
        return table

    def canonical_terms(self, text: str) -> list[str]:
        """English canonical terms found in free text (any of the three languages), longest phrases first."""
        norm = " " + re.sub(r"\s+", " ", normalize(text)) + " "
        found = []
        for phrase, canons in sorted(self._synonyms.items(), key=lambda kv: -len(kv[0])):
            pattern = r"(?<![\w֐-׿؀-ۿ])[ובהלמשכ]?" + re.escape(phrase) + r"(?![\w֐-׿؀-ۿ])"
            if re.search(pattern, norm):
                found += [c for c in canons if c not in found]
        return found

    def drug_mentions(self, text: str) -> set[str]:
        """Curated canonical medications plus registry ingredient names (English) mentioned in text."""
        norm = " " + re.sub(r"[^a-z0-9\- ]", " ", normalize(text)) + " "
        found = {c for c in self.canonical_terms(text) if c in self.terms["medications"]}
        if re.search(r"[a-z]{5}", norm):
            found |= {n for n in self.drug_names if f" {n} " in norm}
        return found

    def medication(self, text: str):
        """Canonical medication name if `text` names a medication in the curated map, else None."""
        for canon in self.canonical_terms(text or ""):
            if canon in self.terms["medications"]:
                return canon
        return None

    def medication_classes(self, canon: str) -> list[str]:
        return self.terms["medications"].get(canon, {}).get("classes", [])

    def allergy_matches(self, allergy: str, canon: str) -> bool:
        """True if a documented allergy names this medication or one of its curated classes."""
        terms = set(self.canonical_terms(allergy))
        return canon in terms or bool(terms & set(self.medication_classes(canon)))

    def label_sources(self, canon: str) -> set[str]:
        """Every active source that is official labeling for this medication: the curated primary label and the
        Israeli registry leaflets (sources whose `medications` list names it)."""
        primary = self.terms["medications"].get(canon, {}).get("label_source")
        return {sid for sid, s in self.sources.items() if s.get("status") == "active"
                and (sid == primary or canon in (s.get("medications") or []))}

    def label_passages(self, canon: str, *section_words, audience="professional") -> list[Passage]:
        """Professional labeling passages only by default: a consumer leaflet is never prescribing guidance."""
        srcs = self.label_sources(canon)
        out = [p for p in self.passages.values() if p.source_id in srcs and (audience is None or p.meta.get("audience") == audience)]
        if section_words:
            out = [p for p in out if any(w in p.section.upper() for w in section_words)]
        return out

    # ---- retrieval -----------------------------------------------------------------------------------------
    def _index(self):
        self._synonyms = self._synonym_table()
        self._docs = {pid: tokens(p.section + " " + p.text) for pid, p in self.passages.items()}
        n = len(self._docs) or 1
        self._avg = sum(len(d) for d in self._docs.values()) / n
        df = {}
        for d in self._docs.values():
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query_terms: list[str], k=8, min_score=1.0, audience="professional") -> list[tuple[Passage, float]]:
        """BM25 over the collection. audience='professional' (default) excludes patient-education leaflets;
        None searches everything (used for patient instructions, where patient leaflets are appropriate)."""
        q = [t for term in query_terms for t in tokens(term)]
        scores = []
        for pid, doc in self._docs.items():
            if audience is not None and self.passages[pid].meta.get("audience") != audience:
                continue
            tf = {}
            for t in doc:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for t in set(q):
                if t in tf:
                    f = tf[t]
                    s += self._idf.get(t, 0) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(doc) / self._avg))
            if s >= min_score:
                scores.append((self.passages[pid], round(s, 2)))
        return sorted(scores, key=lambda x: -x[1])[:k]

    def cite(self, passage_id: str) -> dict:
        p = self.passages[passage_id]
        m = p.meta
        return {"passage_id": p.id, "source_id": p.source_id, "title": m["title"], "publisher": m["publisher"],
                "organization": m.get("organization") or m["publisher"], "section": p.section, "version": m["version"],
                "effective_date": m["effective_date"], "updated_at": m.get("updated_at") or m["effective_date"],
                "retrieved_at": m["retrieved_at"], "jurisdiction": m["jurisdiction"], "population": m["population"],
                "audience": m.get("audience", "professional"), "language": m.get("language", "en"),
                "document_label": m.get("document_label", ""), "product": m.get("product", ""),
                "registry_url": m.get("registry_url", ""), "url": m["url"], "text": p.text}


@lru_cache(maxsize=4)
def load(root: str = str(KNOWLEDGE)) -> Knowledge:
    return Knowledge(Path(root))
