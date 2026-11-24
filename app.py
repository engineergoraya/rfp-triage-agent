"""Streamlit UI for the RFP Triage Agent. Run with:  streamlit run app.py"""

import hashlib
import json
import os
import threading
from datetime import date

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from triage.calendar_export import build_ics, count_exportable
from triage.evidence import summarize
from triage.llm import LLMError, models_to_try
from triage.pdf_reader import PdfError
from triage.pipeline import run_triage
from triage.ranking import MAX_FILES, recommendation_line, run_queue, to_csv
from triage.report_docx import build_docx

st.set_page_config(page_title="RFP Triage Agent", page_icon="📄", layout="centered")

STATUS_LABEL = {"verified": "✅ verified", "wrong_page": "⚠️ wrong page", "not_found": "❌ not found"}
STATUS_ORDER = {"not_found": 0, "wrong_page": 1, "verified": 2}  # riskiest first
# Part of the cache key: bump it when the report's shape changes, so old cached reports are not reused.
# Version 2 added the date `kind` and `mandatory` fields that the Monday queue relies on.
REPORT_VERSION = 2


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
def cached_triage(file_hash: str, model: str, version: int, _pdf_bytes: bytes, _api_key: str,
                  _progress=None) -> dict:
    """Cache by file hash (+ model): re-uploading the same PDF costs no API quota (the free tier
    allows only 20 requests per model per day). persist="disk" keeps results across app restarts.
    Arguments starting with "_" are not hashed by Streamlit, so the key is only (file_hash, model, version)."""
    return run_triage(_pdf_bytes, _api_key, _progress) if _progress else run_triage(_pdf_bytes, _api_key)


def render_report(report: dict, key: str = "single") -> None:
    """The full report for one RFP. `key` keeps widget IDs unique when several reports are on one page."""
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
    st.download_button("📝 Download report (Word .docx)", data=build_docx(report), key=f"{key}-docx",
                       file_name="rfp-triage-report.docx",
                       mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    tab_report, tab_evidence, tab_json = st.tabs(["Report", "Evidence check", "JSON"])

    with tab_report:
        st.subheader("1. Summary")
        st.write(facts["summary"])
        for name, field in [("Agency", "agency"), ("Solicitation", "solicitation_number"),
                          ("Scope of work", "scope_of_work"), ("Contract type", "contract_type"),
                          ("Contract value", "contract_value"), ("Period of performance", "period_of_performance"),
                          ("Place of performance", "place_of_performance"), ("How to submit", "how_to_submit")]:
            st.markdown(f"**{name}:** {facts[field]}")

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
            file_name="rfp-deadlines.ics", mime="text/calendar", disabled=exportable == 0, key=f"{key}-ics",
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
                           file_name="triage-report.json", mime="application/json", key=f"{key}-json")
        st.json(report, expanded=False)


def render_single(api_key: str) -> None:
    """Single RFP view: the original flow, unchanged."""
    uploaded = st.file_uploader("RFP PDF", type=["pdf"])
    if uploaded is None:
        return
    pdf_bytes = uploaded.getvalue()
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        with st.spinner("Reading every page and checking the fine print (usually under a minute)..."):
            report = cached_triage(file_hash, models_to_try()[0], REPORT_VERSION, pdf_bytes, api_key)
    except (PdfError, LLMError) as exc:
        st.error(str(exc))
    except Exception as exc:  # anything unexpected: show it instead of a blank page
        st.error(f"Something went wrong: {exc}")
    else:
        render_report(report)


def render_queue(api_key: str) -> None:
    """Monday queue view: 2-5 RFPs in, one ranked table out. Ranking is plain code (triage/ranking.py)."""
    st.write("Three new RFPs on a Monday? Upload 2 to 5 and see which one deserves the week of proposal writing.")
    files = st.file_uploader("RFP PDFs", type=["pdf"], accept_multiple_files=True, key="queue_files")
    if len(files) > MAX_FILES:
        st.error(f"Please upload at most {MAX_FILES} PDFs; you added {len(files)}. Remove some to continue.")
    elif files:
        st.caption(f"Up to {3 * len(files)} model calls (2 to read each RFP, plus 1 to score it unless it is a "
                   "fast no-bid). Files run one at a time; already-analysed files come from the cache.")
        if st.button("Rank these RFPs", type="primary"):
            progress_bar, status = st.progress(0.0), st.empty()
            ctx = get_script_run_ctx()

            def show(message: str, fraction: float | None = None) -> None:
                # Called from the pipeline's worker threads too; attach the Streamlit context so they can draw.
                add_script_run_ctx(threading.current_thread(), ctx)
                status.caption(message)
                if fraction is not None:
                    progress_bar.progress(fraction)

            def triage_one(pdf_bytes: bytes) -> dict:
                file_hash = hashlib.sha256(pdf_bytes).hexdigest()
                return cached_triage(file_hash, models_to_try()[0], REPORT_VERSION, pdf_bytes, api_key, show)

            # Kept in session_state: opening an expander or downloading the CSV reruns the script,
            # and must not call the model again.
            st.session_state["queue"] = run_queue(
                [(f.name, f.getvalue()) for f in files], triage_one, date.today(), show,
                pause_seconds=float(os.environ.get("QUEUE_PAUSE_SECONDS", "2")))
            progress_bar.progress(1.0)
            status.empty()

    result = st.session_state.get("queue")
    if not result:
        return
    rows = result["rows"]
    st.success(recommendation_line(rows))
    for name in result["skipped"]:
        st.info(f"Skipped duplicate: {name}")
    st.dataframe([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
                 hide_index=True, width="stretch")
    st.caption("Days left are approximate: deadlines are in the agency's own time zone. "
               "Ranking: live BIDs, then live NO_BIDs, then Expired and Failed; then fit score, "
               "earlier deadline, filename.")
    c1, c2 = st.columns(2)
    c1.download_button("⬇️ Download CSV", data=to_csv(rows), file_name="monday-queue.csv", mime="text/csv")
    if c2.button("Clear results"):
        del st.session_state["queue"]
        st.rerun()
    for row in rows:
        if row["File"] in result["reports"]:
            with st.expander(f"#{row['Rank']} {row['File']}: {row['Decision']} {row['Fit score']}/10"):
                render_report(result["reports"][row["File"]], key=f"queue-{row['Rank']}")


# ------------------------------- Page -------------------------------
st.title("📄 RFP Triage Agent")
st.write("Upload a government RFP (text-based PDF). In about a minute you get a bid / no-bid "
         "recommendation for idiaz.io, with every date and requirement traced back to its page.")

api_key = get_api_key()
if not api_key:
    st.error("No Gemini API key found. Set GEMINI_API_KEY as an environment variable or in .streamlit/secrets.toml.")
    st.stop()

# A radio (not st.tabs) so only the chosen view runs, and the choice survives reruns.
view = st.radio("Mode", ["Single RFP", "Monday queue"], horizontal=True, key="view")
if view == "Single RFP":
    render_single(api_key)
else:
    render_queue(api_key)
