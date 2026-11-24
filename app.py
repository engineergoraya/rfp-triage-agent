"""Streamlit UI for the RFP Triage Agent. Run with:  streamlit run app.py"""

import hashlib
import json
import os

import streamlit as st

from triage.calendar_export import build_ics, count_exportable
from triage.evidence import summarize
from triage.llm import LLMError, models_to_try
from triage.pdf_reader import PdfError
from triage.pipeline import run_triage

st.set_page_config(page_title="RFP Triage Agent", page_icon="📄", layout="centered")

STATUS_LABEL = {"verified": "✅ verified", "wrong_page": "⚠️ wrong page", "not_found": "❌ not found"}
STATUS_ORDER = {"not_found": 0, "wrong_page": 1, "verified": 2}  # riskiest first


def get_api_key() -> str:
    """Environment variable first (local dev), then Streamlit secrets (Community Cloud)."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    try:
        return st.secrets.get("GEMINI_API_KEY", "")
    except Exception:  # no secrets.toml at all
        return ""


@st.cache_data(show_spinner=False, max_entries=50, persist="disk")
def cached_triage(file_hash: str, model: str, _pdf_bytes: bytes, _api_key: str) -> dict:
    """Cache by file hash (+ model): re-uploading the same PDF costs no API quota (the free tier
    allows only 20 requests per model per day). persist="disk" keeps results across app restarts.
    Arguments starting with "_" are not hashed by Streamlit, so the key is only (file_hash, model)."""
    return run_triage(_pdf_bytes, _api_key)


def show_report(report: dict) -> None:
    facts = report["facts"]
    verified, total = summarize(report["deadlines"] + report["requirements"])

    # ---- Banner: decision and score at a glance ----
    label = "BID" if report["decision"] == "BID" else "NO BID"
    banner = f"**{label}** · fit score **{report['fit_score']}/10** · {facts['title']}"
    (st.success if report["decision"] == "BID" else st.error)(banner)
    if report["fast_no_bid"]:
        st.info("Fast no-bid: the work is clearly outside our profile, so the detailed scoring step was skipped.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Decision", label)
    c2.metric("Fit score", f"{report['fit_score']}/10")
    c3.metric("Quotes verified", f"{verified}/{total}")
    for warning in report["warnings"]:
        st.warning(warning)

    tab_report, tab_evidence, tab_json = st.tabs(["Report", "Evidence check", "JSON"])

    with tab_report:
        st.subheader("1. Summary")
        st.write(facts["summary"])
        for name, key in [("Agency", "agency"), ("Solicitation", "solicitation_number"),
                          ("Scope of work", "scope_of_work"), ("Contract type", "contract_type"),
                          ("Contract value", "contract_value"), ("Period of performance", "period_of_performance"),
                          ("Place of performance", "place_of_performance"), ("How to submit", "how_to_submit")]:
            st.markdown(f"**{name}:** {facts[key]}")

        st.subheader("2. Key dates")
        if report["deadlines"]:
            # Chronological, with "Not stated" dates last ("9999" sorts after any real date).
            ordered = sorted(report["deadlines"], key=lambda d: d["date"] if d["date"][0].isdigit() else "9999")
            st.dataframe(
                [{"What": d["what"] + (" (superseded)" if d["superseded"] else ""), "Date": d["date"],
                  "Time": d["time"], "As written": d["as_written"], "Page": d["page"],
                  "Evidence": STATUS_LABEL[d["evidence"]["status"]], "Note": d["note"]}
                 for d in ordered],
                hide_index=True, width="stretch",
            )
        else:
            st.write("Not stated")
        exportable = count_exportable(report["deadlines"])
        st.download_button(
            f"📅 Add {exportable} current exact date(s) to calendar (.ics)",
            data=build_ics(report["deadlines"], facts["title"]),
            file_name="rfp-deadlines.ics", mime="text/calendar", disabled=exportable == 0,
        )

        st.subheader("3. Requirements")
        if report["requirements"]:
            st.dataframe(
                [{"Category": r["category"].replace("_", " "), "Requirement": r["requirement"],
                  "Mandatory": "yes" if r["mandatory"] else "no", "Page": r["page"],
                  "Evidence": STATUS_LABEL[r["evidence"]["status"]]}
                 for r in sorted(report["requirements"], key=lambda r: r["category"])],
                hide_index=True, width="stretch",
            )
        else:
            st.write("Not stated")

        st.subheader(f"4. Fit score: {report['fit_score']}/10")
        st.caption(f"Baseline 5, plus or minus each factor below. BID needs at least "
                   f"{report['meta']['bid_threshold']} and no deal-breakers.")
        for f in report["score_factors"]:
            st.markdown(f"- **{f['points']:+d}** {f['factor']}: {f['reason']}")

        st.subheader(f"5. Recommendation: {label}")
        st.markdown("**Three strongest reasons**")
        for i, reason in enumerate(report["reasons"], start=1):
            st.markdown(f"{i}. {reason}")
        sections = [("Deal-breakers", report["deal_breakers"]),
                    ("What would change this decision", report["what_would_change_decision"]),
                    ("Open questions for the agency", report["open_questions"]),
                    ("Ambiguities in the document", [f"{a['issue']} (p. {a['page']})" for a in report["ambiguities"]])]
        for title, items in sections:
            st.markdown(f"**{title}**")
            st.markdown("\n".join(f"- {item}" for item in items) if items else "- None")

        meta = report["meta"]
        st.caption(f"{meta['pages']} pages · models: {meta['extract_model']} / {meta['decide_model']} · "
                   f"{meta['seconds']} s")

    with tab_evidence:
        st.markdown(f"### {verified} of {total} quotes verified")
        st.caption("Plain code (no AI) looked for each quote on the page the model cited. "
                   "Items that could not be verified are listed first: those are the ones to recheck by hand.")
        rows = []
        for d in report["deadlines"]:
            match = d["evidence"]["date_matches_quote"]
            rows.append({"Type": "date", "Item": f"{d['what']}: {d['date']}", "Cited page": d["page"],
                         "Status": STATUS_LABEL[d["evidence"]["status"]],
                         "Found on page": d["evidence"]["found_on_page"],
                         "Date matches quote": {True: "yes", False: "❌ NO", None: "n/a"}[match],
                         "Quote": d["quote"], "_order": STATUS_ORDER[d["evidence"]["status"]] - (match is False)})
        for r in report["requirements"]:
            rows.append({"Type": "requirement", "Item": r["requirement"], "Cited page": r["page"],
                         "Status": STATUS_LABEL[r["evidence"]["status"]],
                         "Found on page": r["evidence"]["found_on_page"], "Date matches quote": "n/a",
                         "Quote": r["quote"], "_order": STATUS_ORDER[r["evidence"]["status"]]})
        rows.sort(key=lambda row: row.pop("_order"))
        st.dataframe(rows, hide_index=True, width="stretch")

    with tab_json:
        st.download_button("⬇️ Download JSON", data=json.dumps(report, indent=2),
                           file_name="triage-report.json", mime="application/json")
        st.json(report, expanded=False)


# ------------------------------- Page -------------------------------
st.title("📄 RFP Triage Agent")
st.write("Upload a government RFP (text-based PDF). In about a minute you get a bid / no-bid "
         "recommendation for idiaz.io, with every date and requirement traced back to its page.")

api_key = get_api_key()
if not api_key:
    st.error("No Gemini API key found. Set GEMINI_API_KEY as an environment variable or in .streamlit/secrets.toml.")
    st.stop()

uploaded = st.file_uploader("RFP PDF", type=["pdf"])
if uploaded is not None:
    pdf_bytes = uploaded.getvalue()
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        with st.spinner("Reading every page and checking the fine print (usually under a minute)..."):
            report = cached_triage(file_hash, models_to_try()[0], pdf_bytes, api_key)
    except (PdfError, LLMError) as exc:
        st.error(str(exc))
    except Exception as exc:  # anything unexpected: show it instead of a blank page
        st.error(f"Something went wrong: {exc}")
    else:
        show_report(report)
