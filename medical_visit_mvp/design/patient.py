"""Patient view: only this patient's approved summaries (and an approved doctor plan, labelled as such)."""
import streamlit as st
from backend import export
from backend.schemas import PatientSummary
from design.common import safe_text, stamp, utc
from design.i18n import lang, t
from design.theme import page_title


def patient_view(repo, user):
    page_title(t("הסיכומים שלי"), t("סיכומי ביקור שנבדקו ונחתמו על ידי הרופא/ה"))
    shown = 0
    for visit in repo.visits(user):
        approved = repo.approved(user, visit["id"])
        if not approved:
            continue  # drafts, unsigned and revoked versions are never shown to patients
        shown += 1
        with st.container(border=True):
            st.subheader(t("ביקור מ־{date}", date=stamp(visit['created_at'])))
            st.markdown("**" + t("מה נאמר בביקור") + "**")
            lines = export.patient_lines(PatientSummary(**approved["patient"]), lang())
            st.markdown("\n".join("#### " + x[3:] if x.startswith("## ") else x for x in lines))
            if approved["plan"]:
                st.markdown("**" + t("תוכנית שהרופא/ה הוסיף/ה לאחר הביקור") + "**")
                st.markdown("\n".join("- " + safe_text(i["text"]) for i in approved["plan"]))
            sig = approved["signature"]
            st.caption(t("נחתם על ידי {name} · {time} UTC · מזהה אישור {id}", name=sig['signer_name'] or sig['signer_id'],
                         time=utc(sig['signed_at']), id=approved['approval_id']))
            name = f"visit_{approved['visit_id']}"
            cols = st.columns(3)
            if cols[0].download_button(t("הורדה כטקסט"), export.to_text(approved, False, lang()), f"{name}.txt", "text/plain", key=f"p_txt_{approved['approval_id']}"):
                repo.log_export(user, approved["visit_id"], approved["approval_id"], "txt")
            try:
                pdf = export.to_pdf(approved, False, lang())
                if cols[1].download_button(t("הורדה כ־PDF"), pdf, f"{name}.pdf", "application/pdf", key=f"p_pdf_{approved['approval_id']}"):
                    repo.log_export(user, approved["visit_id"], approved["approval_id"], "pdf")
            except (ValueError, ImportError) as exc:
                cols[1].caption(t("PDF אינו זמין: {error}", error=t(str(exc))))
            if cols[2].button(t("הכנת JSON"), key=f"p_json_{approved['approval_id']}"):
                st.session_state[f"p_json_{approved['approval_id']}_data"] = export.to_json(repo.record_export(user, visit["id"]))
            if st.session_state.get(f"p_json_{approved['approval_id']}_data"):
                cols[2].download_button(t("הורדת JSON"), st.session_state[f"p_json_{approved['approval_id']}_data"], f"{name}.json",
                                        "application/json", key=f"p_json_dl_{approved['approval_id']}")
    if not shown:
        st.info(t("אין עדיין סיכומים מאושרים."))
