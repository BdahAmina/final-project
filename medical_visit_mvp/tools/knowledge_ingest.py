"""Fetch and store verbatim passages for every source in data/knowledge/sources.json.

Usage (from medical_visit_mvp):
  ..\\venv\\Scripts\\python.exe tools\\knowledge_ingest.py            # all sources
  ..\\venv\\Scripts\\python.exe tools\\knowledge_ingest.py <source-id> # one source

For each source it writes data/knowledge/passages/<id>.json with: source metadata, the exact
version fetched (DailyMed SPL version + effective date, or HTTP Last-Modified), retrieval time
(UTC), SHA-256 of the raw download and the passages. A failed download marks the source
"blocked" in data/knowledge/manifest.json; existing passages of that source are kept unchanged.
No text is ever generated or paraphrased here.
"""
import hashlib
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # backend.il_registry
KNOWLEDGE = ROOT / "data" / "knowledge"
PASSAGES = KNOWLEDGE / "passages"
NS = {"v3": "urn:hl7-org:v3"}
UA = {"User-Agent": "medical-visit-mvp knowledge ingest (educational prototype)"}
# LOINC section codes kept from drug labels. Dosage/administration (34068-7), dosage forms (43678-2)
# and overdosage (34088-5) are excluded on purpose: the assistant never produces doses.
KEEP = {
    "34066-1": "BOXED WARNING", "34067-9": "INDICATIONS AND USAGE", "34070-3": "CONTRAINDICATIONS",
    "43685-7": "WARNINGS AND PRECAUTIONS", "34071-1": "WARNINGS", "42232-9": "PRECAUTIONS", "34073-7": "DRUG INTERACTIONS",
    "43684-0": "USE IN SPECIFIC POPULATIONS", "42228-7": "PREGNANCY", "77290-5": "LACTATION", "34080-2": "NURSING MOTHERS",
    "34081-0": "PEDIATRIC USE", "34082-8": "GERIATRIC USE", "34084-4": "ADVERSE REACTIONS",
    "50570-1": "DO NOT USE", "50569-3": "ASK DOCTOR", "50568-5": "ASK DOCTOR/PHARMACIST", "50567-7": "WHEN USING",
    "50566-9": "STOP USE", "53414-9": "PREGNANCY OR BREAST FEEDING", "34067-9U": "USES", "44425-7": "PURPOSE",
}
EXCLUDE = {"34068-7", "43678-2", "34088-5"}
MAX_CHARS = 900


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def text_of(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def chunk(paragraphs, limit=MAX_CHARS):
    out, cur = [], ""
    for p in paragraphs:
        if cur and len(cur) + len(p) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = (cur + " " + p).strip()
        while len(cur) > limit * 1.6:  # one very long paragraph: split at sentence boundaries
            cut = cur.rfind(". ", 0, limit)
            cut = cut + 1 if cut > 200 else limit
            out.append(cur[:cut].strip())
            cur = cur[cut:].strip()
    if cur:
        out.append(cur)
    return out


def dailymed(source):
    url = f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{source['setid']}.xml"
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    raw = r.content
    root = ET.fromstring(raw)
    version = root.find("v3:versionNumber", NS).get("value")
    effective = root.find("v3:effectiveTime", NS).get("value")
    title = text_of(root.find("v3:title", NS)) if root.find("v3:title", NS) is not None else source["title"]
    passages = []

    def walk(section, path, kept):
        code_el = section.find("v3:code", NS)
        code = code_el.get("code") if code_el is not None else ""
        if code in EXCLUDE:
            return
        title_el = section.find("v3:title", NS)
        heading = text_of(title_el) if title_el is not None else KEEP.get(code, "")
        here = path + [heading] if heading else path
        keep = kept or code in KEEP
        if keep:
            # Older (non-PLR) labels put several subsections in one section, marked only by short
            # heading paragraphs; group text under those headings so citations name the right subsection.
            groups, sub = [([], [])], []
            body = section.find("v3:text", NS)
            for child in body if body is not None else []:
                t = text_of(child)
                if not t:
                    continue
                first = next(iter(child), None)
                style = (first.get("styleCode") or "").lower() if first is not None and first.tag.endswith("content") else ""
                if len(t) <= 60 and not t.endswith((".", ":")) and t != "​":
                    sub = [sub[0], t] if style in ("underline", "italics") and sub else [t]
                    groups.append((list(sub), []))
                    continue
                groups[-1][1].append(t)
            # Lossless: a line taken as a heading is also kept as text ("temporarily reduces fever" looks
            # like a heading but is label content). A group too short to stand alone joins the previous one.
            merged = []
            for heads, paras in groups:
                texts = ([heads[-1]] if heads else []) + paras
                if not texts:
                    continue
                if merged and len(" ".join(texts)) < 80:
                    merged[-1][1].extend(texts)
                else:
                    merged.append((heads, texts))
            for heads, texts in merged:
                for piece in chunk(texts):
                    passages.append({"id": f"{source['id']}#{code or 'sub'}-{len(passages) + 1}",
                                     "section": " > ".join(h for h in here + heads if h), "section_code": code, "text": piece})
        for comp in section.findall("v3:component/v3:section", NS):
            walk(comp, here, keep)

    for sec in root.findall(".//v3:structuredBody/v3:component/v3:section", NS):
        walk(sec, [], False)
    return raw, {"version": f"SPL version {version}", "effective_date": f"{effective[:4]}-{effective[4:6]}-{effective[6:8]}",
                 "label_title": title, "url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={source['setid']}"}, passages


def html_page(source):
    r = requests.get(source["url"], headers={**UA, "Accept": "text/html"}, timeout=60)
    r.raise_for_status()
    raw = r.content
    body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", r.text)
    paras = [html.unescape(re.sub(r"<[^>]+>", " ", m)).strip() for m in re.findall(r"(?is)<(?:p|li|h[1-4])[^>]*>(.*?)</(?:p|li|h[1-4])>", body)]
    paras = [re.sub(r"\s+", " ", p) for p in paras if len(p) > 40]
    passages = [{"id": f"{source['id']}#p-{n}", "section": "", "section_code": "", "text": t} for n, t in enumerate(chunk(paras), 1)]
    return raw, {"version": r.headers.get("Last-Modified", "unknown"), "effective_date": r.headers.get("Last-Modified", "unknown"),
                 "url": source["url"]}, passages


# ---- Israeli Ministry of Health sources ---------------------------------------------------------------------
# Doctor leaflets (SmPC or US-style PI, usually English) and consumer leaflets (Hebrew/Arabic/English). Dosing and
# overdose sections are dropped for the same reason as above; so are storage/pharmaceutical/administrative parts.
SMPC_HEAD = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})?)\.?\s+([A-Z][A-Za-z ,/&()'\-]{3,80})\s*$")
CONSUMER_HEAD = re.compile(r"^\s*(\d)\s*[.)]\s*(.{3,60}\?|.{3,60})\s*$")
DROP_HEAD = re.compile(r"posology|dosage|administration|overdos|pharmaceutical|marketing authori|manufacturer|license|"
                       r"registration|date of revision|storage|store|shelf life|excipient|composition|how should you use|"
                       r"how to use|additional information|syringe|mechanism|pharmacokinetic|pharmacodynamic|clinical trial|"
                       r"microbiology|toxicology|non-clinical|supporting product|disposal|"
                       r"כיצד תשתמש|איך תשתמש|מינון|מנת יתר|לאחסן|מידע נוסף|מזרק|"
                       r"كيف تستعمل|كيف يجب استعمال|الجرعة|تخزين|يخزن|معلومات إضافية|المحقنة", re.I)


# Only clinically applicable sections are kept (an allowlist), then the dosing/administrative blocklist above applies.
KEEP_HEAD = re.compile(r"indication|contraindicat|warning|precaution|interaction|pregnan|lactation|fertility|breast|"
                       r"special population|adverse|undesirable|side effect|ability to drive|intended for|before using|"
                       r"למה מיועדת|לפני השימוש|לפני שימוש|תופעות לוואי|لأي غرض|قبل استعمال|الأعراض الجانبية", re.I)


def pdf_paragraphs(raw: bytes) -> list[str]:
    from io import BytesIO
    from pypdf import PdfReader
    lines = []
    for page in PdfReader(BytesIO(raw)).pages:
        lines += [re.sub(r"\s+", " ", ln).strip() for ln in (page.extract_text() or "").splitlines()]
    return [ln for ln in lines if ln]


def leaflet_sections(lines: list[str], consumer: bool) -> list[tuple[str, list[str]]]:
    """(heading, lines) per section; dropped sections are removed. Text is kept verbatim."""
    head_re = CONSUMER_HEAD if consumer else SMPC_HEAD
    sections, heading, buf = [], "", []
    for ln in lines:
        m = head_re.match(ln)
        if m and len(ln) < 90:
            if buf:
                sections.append((heading, buf))
            heading, buf = ln.strip(), []
            continue
        buf.append(ln)
    if buf:
        sections.append((heading, buf))
    return [(h, b) for h, b in sections if h and KEEP_HEAD.search(h) and not DROP_HEAD.search(h)]


def il_moh_registry(source, registry=None):
    """One curated active ingredient: pick a representative registered product and store its newest official
    doctor leaflet (professional) and consumer leaflets (patient education), each passage with its own metadata."""
    from backend import il_registry as ilr
    registry = registry or ilr.Registry()
    terms = json.loads((KNOWLEDGE / "terms.json").read_text(encoding="utf-8"))
    vocabulary = {s.lower() for m in terms["medications"].values() for s in m["synonyms"] if s.isascii()} | \
                 {c.lower() for c in terms["medications"]}
    candidates = ilr.rank_products(registry.search(source["ingredient"], vocabulary))
    if not candidates:
        raise ValueError("no active human-use product found in the registry")
    wanted = tuple(source.get("documents", ["doctor_leaflet", "consumer_leaflet"]))
    product, docs = None, []
    for candidate in candidates[:5]:  # the first products that list a doctor leaflet; at most 5 lookups
        docs = ilr.latest_documents(registry.documents(candidate["medicineId"]), wanted)
        if any(ilr.describe(d)["audience"] == "professional" for d in docs):
            product = candidate
            break
    if not product:
        raise ValueError("none of the top registered products lists a doctor leaflet")
    raws, passages = [], []
    page = f"{ilr.REGISTRY_PAGE}/{product['medicineId']}"
    for doc in docs:
        d = ilr.describe(doc)
        raw = registry.download(doc["fName"])
        raws.append(raw)
        consumer = d["audience"] == "patient_education"
        for heading, lines in leaflet_sections(pdf_paragraphs(raw), consumer):
            for piece in chunk(lines):
                passages.append({"id": f"{source['id']}#{d['document_type']}-{d['language']}-{len(passages) + 1}",
                                 "section": f"{d['document_label']} · {heading}", "section_code": d["document_type"], "text": piece,
                                 "meta": d | {"product": f"{product['tradeName']} / {product['tradeNameHeb']}",
                                              "reg_number": product["regNumber"], "registry_url": page}})
    newest = max(ilr.describe(d)["updated_at"] for d in docs)
    return b"".join(raws), {"version": f"registry {product['regNumber']} · leaflets updated {newest}", "effective_date": newest,
                            "url": page, "organization": ilr.ORGANIZATION, "product": product["tradeName"]}, passages


def check_allowed(source, allowed: list[str]):
    from urllib.parse import urlparse
    host = urlparse(source.get("url", "")).hostname or ""
    if source.get("url") and host not in allowed:
        raise ValueError(f"domain not on the allowlist: {host}")


def manual_import(source, file_path):
    """Ministry pages that block automated access (gov.il returns HTTP 403 to scripts): an administrator saves the
    official document from source['url'] in a browser and imports the file. The text is stored verbatim."""
    raw = Path(file_path).read_bytes()
    if raw[:4] == b"%PDF":
        paras = pdf_paragraphs(raw)
    else:
        body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw.decode("utf-8", "replace"))
        paras = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m))).strip()
                 for m in re.findall(r"(?is)<(?:p|li|h[1-4]|td)[^>]*>(.*?)</(?:p|li|h[1-4]|td)>", body)] or \
                [ln.strip() for ln in raw.decode("utf-8", "replace").splitlines()]
    paras = [p for p in paras if len(p) > 30]
    meta = {k: source.get(k) for k in ("audience", "language", "document_type", "updated_at")} | {"url": source["url"]}
    passages = [{"id": f"{source['id']}#p-{n}", "section": source.get("document_label", ""), "section_code": source.get("document_type", ""),
                 "text": t, "meta": meta} for n, t in enumerate(chunk(paras), 1)]
    return raw, {"version": f"manual import of {Path(file_path).name}", "effective_date": source.get("updated_at") or "unknown",
                 "url": source["url"], "organization": source.get("organization", "")}, passages


def ingest(source, manifest, allowed=(), file_path=None):
    fetch = {"dailymed_spl": dailymed, "html": html_page, "il_moh_registry": il_moh_registry}.get(source["type"])
    if source["type"] == "manual_import":
        fetch = (lambda s: manual_import(s, file_path)) if file_path else None
    entry = {"title": source["title"], "attempted_at": utc_now()}
    try:
        if fetch is None:
            raise ValueError("awaiting manual import (see data/knowledge/README.md)" if source["type"] == "manual_import"
                             else f"unsupported source type {source['type']}")
        if allowed:
            check_allowed(source, list(allowed))
        raw, meta, passages = fetch(source)
        if not passages:
            raise ValueError("no passages extracted")
    except Exception as exc:
        reason = (f"HTTP {exc.response.status_code}" if getattr(exc, "response", None) is not None
                  else f"{type(exc).__name__}: {exc}" if isinstance(exc, ValueError) else type(exc).__name__)
        entry.update(status="blocked", reason=reason)
        print(f"BLOCKED  {source['id']}: {reason}")
        manifest["sources"][source["id"]] = {**manifest["sources"].get(source["id"], {}), **entry}
        return
    record = {k: source.get(k) for k in ("id", "title", "publisher", "jurisdiction", "population", "medications", "type",
                                         "audience", "language")} | meta | {
        "retrieved_at": utc_now(), "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "passages_sha256": hashlib.sha256(json.dumps(passages, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "passages": passages}
    PASSAGES.mkdir(parents=True, exist_ok=True)
    (PASSAGES / f"{source['id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    entry.update(status="active", version=meta["version"], effective_date=meta["effective_date"], retrieved_at=record["retrieved_at"],
                 passages=len(passages), raw_sha256=record["raw_sha256"], passages_sha256=record["passages_sha256"])
    manifest["sources"][source["id"]] = entry
    print(f"OK       {source['id']}: {len(passages)} passages, {meta['version']}, effective {meta['effective_date']}")


def main():
    config = json.loads((KNOWLEDGE / "sources.json").read_text(encoding="utf-8"))
    path = KNOWLEDGE / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sources": {}}
    args = sys.argv[1:]
    allowed = config.get("allowed_domains", [])
    if args[:1] == ["--drug-names"]:  # registry ingredient list -> data/knowledge/drug_names.json (mention detection only)
        from backend import il_registry as ilr
        names = ilr.Registry().ingredient_names()
        (KNOWLEDGE / "drug_names.json").write_text(json.dumps({
            "source": ilr.API + "GetComponents", "organization": ilr.ORGANIZATION, "retrieved_at": utc_now(),
            "purpose": "detect medication names in model output; not evidence", "names": names}, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"OK       drug names: {len(names)} active ingredients")
        return
    if args[:1] == ["--import"]:  # --import <source-id> <saved file>
        if len(args) != 3:
            sys.exit("usage: knowledge_ingest.py --import <source-id> <file>")
        source = next((s for s in config["sources"] if s["id"] == args[1] and s["type"] == "manual_import"), None)
        if not source:
            sys.exit(f"{args[1]} is not a manual_import source in sources.json")
        ingest(source, manifest, allowed, file_path=args[2])
    else:
        wanted = set(args)
        for source in config["sources"]:
            if (not wanted and source["type"] != "manual_import") or source["id"] in wanted:
                ingest(source, manifest, allowed)
                time.sleep(0.5)
    manifest["collection_version"] = hashlib.sha256(json.dumps(
        {k: [v.get("raw_sha256"), v.get("passages_sha256")] for k, v in sorted(manifest["sources"].items())},
        sort_keys=True).encode()).hexdigest()[:12]
    manifest["updated_at"] = utc_now()
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print("collection version", manifest["collection_version"])


if __name__ == "__main__":
    main()
