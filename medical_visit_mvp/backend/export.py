"""Exports of approved records only (text / PDF), plus JSON helpers.

Callers must pass the dict returned by Repository.approved(), which exists only while the approval is
valid and its SHA-256 still matches the signed versions, so a changed draft cannot be exported as an
approved document. Every human-readable copy carries the record ids, versions, UTC timestamps,
approval status, content hash and the signature manifestation (printed name, date/time, meaning).
Recorded visit facts and the doctor's post-visit plan are printed in separate, labelled sections.
"""
import json
import os
import time
from pathlib import Path
from backend.audit import utc_iso
from backend.i18n import direction, translate
from backend.schemas import ClinicianDraft, PatientSummary
from backend.summarizer import render_clinician

HEADER = "סיכום ביקור — הופק בסיוע מערכת אוטומטית, נבדק ואושר בחתימה אלקטרונית של הרופא/ה"
PROTOTYPE = "אב־טיפוס לימודי שנבחן עם מטופלים בדויים. אינו מכשיר רפואי מאושר."
FONT_CANDIDATES = [os.getenv("EXPORT_FONT", ""), r"C:\Windows\Fonts\arial.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"]
PATIENT_GROUPS = [("assessment", "מה נאמר על מצבך"), ("treatment", "טיפול שנאמר בביקור"), ("follow_up", "הנחיות להמשך")]


def patient_lines(summary: PatientSummary, lang: str = "he"):
    lines = []
    for category, label in PATIENT_GROUPS:
        items = [i for i in summary.items if i.category == category]
        if items:
            lines.append(f"## {translate(label, lang)}")
            for item in items:
                lines.append("- " + item.text)
                for m in item.medications:
                    details = [v for v in (m.name, m.dose and f"{m.dose} {m.unit or ''}".strip(), m.frequency, m.route, m.duration) if v]
                    if details:
                        lines.append("  " + " | ".join(details))
    return lines or [translate("לא נאמרו בביקור הנחיות טיפול או המשך.", lang)]


def plan_lines(plan_items, include_provenance, lang: str = "he"):
    T = lambda text, **kw: translate(text, lang, **kw)  # noqa: E731
    if not plan_items:
        return []
    lines = ["# " + T("תוכנית הרופא/ה לאחר הביקור"), "(" + T("נכתבה ואושרה על ידי הרופא/ה לאחר הביקור; אינה חלק ממה שנאמר בביקור") + ")"]
    for item in plan_items:
        lines.append("- " + item["text"])
        if include_provenance and item.get("source") == "assistant_suggestion":
            note = T("מקור: הצעת העוזר הקליני") + (" (" + T("נערכה") + ")" if item.get("edited") else "") +                 ", " + T("הצעה #{id}", id=item.get("suggestion_id"))
            cites = ", ".join(c["passage_id"] for c in item.get("citations", []))
            lines.append(f"  {note}; {T('מקורות:')} {cites}" if cites else f"  {note}")
    return lines


def manifest_lines(approved: dict, lang: str = "he") -> list[str]:
    T = lambda text, **kw: translate(text, lang, **kw)  # noqa: E731
    v, sig = approved["versions"], approved["signature"]
    lines = [T(HEADER), T(PROTOTYPE), "",
             T("מזהה ביקור: {id}", id=approved["visit_id"]),
             T("סטטוס: מאושר | מזהה אישור: {id}", id=approved["approval_id"]),
             T("גרסאות: סיכום רופא #{c} (גרסה {cv}), סיכום מטופל #{p} (גרסה {pv}), תמלול #{tr}",
               c=v["clinician"][0], cv=v["clinician"][1], p=v["patient"][0], pv=v["patient"][1], tr=v["transcript"])
             + (", " + T("תוכנית #{id} (גרסה {v})", id=v["plan"][0], v=v["plan"][1]) if v.get("plan") else ""),
             T("SHA-256 של התוכן החתום: {hash}", hash=approved["content_sha256"])]
    if sig.get("signer_name"):
        lines += [T("חתימה אלקטרונית: {name} ({id})", name=sig["signer_name"], id=sig["signer_id"]),
                  T("מועד החתימה (UTC): {time}", time=utc_iso(sig["signed_at"])),
                  T("משמעות החתימה: {meaning}", meaning=T(sig["meaning"])),
                  T("אופן החתימה: {method}", method=sig["method"])]
        if sig.get("algorithm"):
            lines += [T("חתימה קריפטוגרפית: {alg} · טביעת מפתח: {key}", alg=sig["algorithm"], key=sig["key_fingerprint"]),
                      T("אימות דו־שלבי בעת החתימה: {mfa} · מקור זמן: {source}", mfa=T("כן") if sig["mfa"] else T("לא"),
                        source=sig["time_source"] or "—"),
                      T("ערך החתימה (Base64): {value}", value=sig["signature"])]
    else:
        lines += [T("אישור מגרסה קודמת של המערכת (ללא רכיבי חתימה אלקטרונית), מועד (UTC): {time}", time=utc_iso(sig["signed_at"]))]
    return lines + [""]


def to_text(approved: dict, include_clinician: bool, lang: str = "he") -> str:
    parts = manifest_lines(approved, lang)
    if include_clinician and "clinician" in approved:
        parts += ["# " + translate("סיכום לרופא — עובדות שנאמרו בביקור", lang), render_clinician(ClinicianDraft(**approved["clinician"]), lang), ""]
    parts += ["# " + translate("סיכום למטופל — מה שנאמר בביקור", lang)] + patient_lines(PatientSummary(**approved["patient"]), lang) + [""]
    parts += plan_lines(approved.get("plan_items") if include_clinician else approved.get("plan"), include_clinician, lang)
    return "\n".join(parts)


def to_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1, default=str)


def _font():
    for path in FONT_CANDIDATES:
        if path and Path(path).exists():
            return path
    raise ValueError("לא נמצא גופן התומך בעברית ובערבית לייצוא PDF. הגדירו EXPORT_FONT לנתיב קובץ TTF, או ייצאו כטקסט.")


# ---- PDF: official A4 layout (header band, record details, titled sections, signature block, footer) ----------
SYSTEM_NAME = "מערכת סיכום ביקורים רפואיים"
NAVY, TEAL, MUTED, LINE, SOFT = (11, 46, 89), (15, 118, 110), (91, 107, 119), (217, 226, 229), (243, 246, 249)


def _local(t) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(t)) if t else "—"


def _sections(approved: dict, include_clinician: bool, lang: str = "he") -> list[tuple[str, str, list[str]]]:
    """(title, note, lines) per section; lines keep the '## ', '- ' and indented-detail markers of the text export."""
    out = []
    if include_clinician and "clinician" in approved:
        out.append((translate("סיכום לרופא — עובדות שנאמרו בביקור", lang), "",
                    render_clinician(ClinicianDraft(**approved["clinician"]), lang).splitlines()))
    out.append((translate("סיכום למטופל/ת — מה שנאמר בביקור", lang), "", patient_lines(PatientSummary(**approved["patient"]), lang)))
    plan = plan_lines(approved.get("plan_items") if include_clinician else approved.get("plan"), include_clinician, lang)
    if plan:
        out.append((plan[0].lstrip("# "), plan[1].strip("()"), plan[2:]))
    return out


def to_pdf(approved: dict, include_clinician: bool, lang: str = "he") -> bytes:
    """Same layout in every language; alignment, column order and text direction follow the document language."""
    from fpdf import FPDF
    T = lambda text, **kw: translate(text, lang, **kw)  # noqa: E731
    rtl = direction(lang) == "rtl"
    DIR, START, END = ("rtl", "R", "L") if rtl else ("ltr", "L", "R")

    class Doc(FPDF):
        def header(self):
            self.set_fill_color(*NAVY)
            self.rect(0, 0, self.w, 24, "F")
            self.set_fill_color(*TEAL)
            self.rect(0, 24, self.w, 1.2, "F")
            self.set_text_color(255, 255, 255)
            self.set_xy(self.l_margin, 5)
            self.set_font("body", size=15)
            self.cell(0, 8, T(SYSTEM_NAME), align=START, new_x="LMARGIN", new_y="NEXT")
            self.set_font("body", size=9)
            self.cell(0, 6, T("סיכום ביקור מאושר") + " · " + (T("עותק לרופא/ה") if include_clinician else T("עותק למטופל/ת")), align=START)
            self.set_xy(self.l_margin, 8)
            self.set_font("body", size=9)
            self.cell(0, 6, T("ביקור {id}", id=approved["visit_id"]), align=END)
            self.set_text_color(0, 0, 0)
            self.set_y(32)

        def footer(self):
            self.set_y(-14)
            self.set_draw_color(*LINE)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.set_font("body", size=7.5)
            self.set_text_color(*MUTED)
            self.cell(0, 6, T(PROTOTYPE), align=START)
            self.set_x(self.l_margin)
            self.set_text_shaping(use_shaping_engine=True, direction="ltr")
            self.cell(0, 6, f"{self.page_no()} / {{nb}}", align=END)
            self.set_text_shaping(use_shaping_engine=True, direction=DIR)

    pdf = Doc(format="A4")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_font("body", fname=_font())
    pdf.set_font("body", size=11)
    try:
        pdf.set_text_shaping(use_shaping_engine=True, direction=DIR)
    except Exception:
        raise ValueError("ייצוא PDF בעברית דורש את החבילה uharfbuzz. התקינו את requirements.txt או ייצאו כטקסט.") from None
    pdf.set_title(f"Visit {approved['visit_id']} approval {approved['approval_id']}")
    pdf.add_page()
    epw = pdf.epw

    # Title
    pdf.set_font("body", size=18)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 10, T("סיכום ביקור רפואי"), align=START, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("body", size=9)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(0, 5, T("המסמך הופק בסיוע מערכת אוטומטית, נבדק ואושר בחתימה אלקטרונית של הרופא/ה."), align=START,
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Record details: label column on the reading-start side, alternating shading
    sig = approved["signature"]
    rows = [(T("מזהה ביקור"), approved["visit_id"]), (T("מועד הביקור"), _local(approved.get("created_at"))),
            (T("סטטוס"), T("מאושר וחתום")), (T("מזהה אישור"), str(approved["approval_id"])),
            (T("מועד האישור"), _local(sig.get("signed_at") or approved.get("approved_at")))]
    label_w = 42
    pdf.set_draw_color(*LINE)
    for n, (label, value) in enumerate(rows):
        fill = n % 2 == 0
        pdf.set_fill_color(*SOFT)
        pdf.set_font("body", size=10)
        pdf.set_x(pdf.l_margin)
        cells = [(epw - label_w, value, (0, 0, 0)), (label_w, label, MUTED)]  # left to right on the page
        for k, (w, text, color) in enumerate(cells if rtl else cells[::-1]):
            pdf.set_text_color(*color)
            last = k == 1
            pdf.cell(w, 8, text, border="TBR" if last else "TBL", align=START, fill=fill,
                     **({"new_x": "LMARGIN", "new_y": "NEXT"} if last else {}))
    pdf.ln(6)

    # Sections
    for title, note, lines in _sections(approved, include_clinician, lang):
        if pdf.get_y() > pdf.h - 50:
            pdf.add_page()
        pdf.set_font("body", size=13)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 8, title, align=START, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*TEAL)
        pdf.set_line_width(0.6)
        x0 = pdf.w - pdf.r_margin - 40 if rtl else pdf.l_margin
        pdf.line(x0, pdf.get_y(), x0 + 40, pdf.get_y())
        pdf.set_line_width(0.2)
        pdf.ln(2)
        if note:
            pdf.set_font("body", size=8.5)
            pdf.set_text_color(*MUTED)
            pdf.multi_cell(0, 5, note, align=START, new_x="LMARGIN", new_y="NEXT")
        for line in lines:
            if not line.strip():
                pdf.ln(1.5)
                continue
            if line.startswith("#"):
                pdf.ln(1)
                pdf.set_font("body", size=11)
                pdf.set_text_color(*TEAL)
                pdf.multi_cell(0, 6.5, line.lstrip("# "), align=START, new_x="LMARGIN", new_y="NEXT")
            elif line.startswith("- "):
                pdf.set_font("body", size=10.5)
                pdf.set_text_color(0, 0, 0)
                pdf.set_x(pdf.l_margin if rtl else pdf.l_margin + 4)
                pdf.multi_cell(epw - 4, 6, "•  " + line[2:], align=START, new_x="LMARGIN", new_y="NEXT")
            else:  # indented detail (medication fields, source quote, provenance)
                pdf.set_font("body", size=8.5)
                pdf.set_text_color(*MUTED)
                pdf.set_x(pdf.l_margin if rtl else pdf.l_margin + 9)
                pdf.multi_cell(epw - 9, 5, line.strip(), align=START, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)

    # Signature block
    if pdf.get_y() > pdf.h - 70:
        pdf.add_page()
    top = pdf.get_y()
    pdf.set_draw_color(*NAVY)
    pdf.set_fill_color(*SOFT)
    box_h = (46 if sig.get("algorithm") else 34) if sig.get("signer_name") else 22
    pdf.rect(pdf.l_margin, top, epw, box_h, "DF")
    pdf.set_xy(pdf.l_margin + 4, top + 3)
    pdf.set_font("body", size=11)
    pdf.set_text_color(*NAVY)
    pdf.cell(epw - 8, 7, T("חתימה אלקטרונית"), align=START, new_x="LEFT", new_y="NEXT")
    pdf.set_font("body", size=10)
    pdf.set_text_color(0, 0, 0)
    if sig.get("signer_name"):
        for text in (T("נחתם על ידי: {name} ({id})", name=sig["signer_name"], id=sig["signer_id"]),
                     T("מועד החתימה: {local} (UTC {utc})", local=_local(sig["signed_at"]), utc=utc_iso(sig["signed_at"])),
                     T("משמעות החתימה: {meaning}", meaning=T(sig["meaning"])),
                     *([T("חתימה קריפטוגרפית: {alg} · טביעת מפתח: {key}", alg=sig["algorithm"], key=sig["key_fingerprint"][:16] + "…"),
                        T("אימות דו־שלבי בעת החתימה: {mfa} · מקור זמן: {source}", mfa=T("כן") if sig["mfa"] else T("לא"),
                          source=sig["time_source"] or "—")] if sig.get("algorithm") else [])):
            pdf.cell(epw - 8, 6, text, align=START, new_x="LEFT", new_y="NEXT")
    else:
        pdf.cell(epw - 8, 6, T("אושר בגרסה קודמת של המערכת (ללא רכיבי חתימה) · {time}", time=_local(sig["signed_at"])), align=START,
                 new_x="LEFT", new_y="NEXT")
    pdf.set_y(top + box_h + 6)

    # Technical record (for verification)
    v = approved["versions"]
    pdf.set_font("body", size=8)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(0, 4.5, T("גרסאות: סיכום רופא #{c} (גרסה {cv}), סיכום מטופל #{p} (גרסה {pv}), תמלול #{tr}",
                             c=v["clinician"][0], cv=v["clinician"][1], p=v["patient"][0], pv=v["patient"][1], tr=v["transcript"])
                   + (", " + T("תוכנית #{id} (גרסה {v})", id=v["plan"][0], v=v["plan"][1]) if v.get("plan") else ""),
                   align=START, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 4.5, T("SHA-256 של התוכן החתום:"), align=START, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_shaping(use_shaping_engine=True, direction="ltr")
    pdf.cell(0, 4.5, approved["content_sha256"], align=START, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_shaping(use_shaping_engine=True, direction=DIR)
    return bytes(pdf.output())
