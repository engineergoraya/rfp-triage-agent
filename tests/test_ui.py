"""Drive the real Streamlit app (app.py) with Streamlit's AppTest, with the agent mocked out."""

import io
import json
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")
SAMPLE_REPORT = json.loads((Path(__file__).parent / "sample_report.json").read_text())


def blank_pdf() -> bytes:
    """A PDF with pages but no text, like a scanned document."""
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload(file_bytes: bytes, report=None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
         patch("triage.pipeline.run_triage", return_value=report) as fake:
        at.run()
        at.file_uploader[0].set_value(("rfp.pdf", file_bytes, "application/pdf"))
        at.run()
    at.fake = fake
    return at


def test_missing_api_key_shows_error():
    at = AppTest.from_file(APP)
    with patch.dict("os.environ", {"GEMINI_API_KEY": ""}), patch("streamlit.secrets", {}):
        at.run()
    assert any("No Gemini API key" in e.value for e in at.error)


def test_report_renders_banner_tabs_and_downloads():
    at = upload(b"%PDF-1.4 fake bytes for the hash", SAMPLE_REPORT)
    assert not at.exception
    banner = at.success[0].value if SAMPLE_REPORT["decision"] == "BID" else at.error[0].value
    assert f"{SAMPLE_REPORT['fit_score']}/10" in banner
    assert [t.label for t in at.tabs] == ["Report", "Evidence check", "JSON"]
    assert any("quotes verified" in m.value for m in at.markdown)
    labels = [b.label for b in at.get("download_button")]
    assert any(".ics" in label for label in labels) and any("JSON" in label for label in labels)


def test_scanned_pdf_shows_clear_error():
    # No mock needed for the error: the real PDF reader raises before any model call.
    at = AppTest.from_file(APP, default_timeout=30)
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
        at.run()
        at.file_uploader[0].set_value(("scan.pdf", blank_pdf(), "application/pdf"))
        at.run()
    assert any("scanned" in e.value for e in at.error)


# ---------------- Monday queue view ----------------

def open_queue(files, report=None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30)
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key", "QUEUE_PAUSE_SECONDS": "0"}), \
         patch("triage.pipeline.run_triage", return_value=report) as fake:
        at.run()
        at.radio(key="view").set_value("Monday queue").run()
        at.file_uploader(key="queue_files").set_value(files).run()
        if at.button:
            at.button[0].click().run()   # "Rank these RFPs"
        at.run()                        # an unrelated rerun (like a download click)
    at.fake = fake
    return at


def test_queue_ranks_files_and_keeps_results_across_reruns():
    files = [("a.pdf", b"%PDF-1.4 one", "application/pdf"), ("b.pdf", b"%PDF-1.4 two", "application/pdf")]
    at = open_queue(files, SAMPLE_REPORT)
    assert not at.exception
    assert at.fake.call_count == 2          # one triage per file, and the extra rerun did not call again
    assert any(s.value.startswith("Start with:") for s in at.success)
    assert any(b.label == "⬇️ Download CSV" for b in at.get("download_button"))
    assert len(at.expander) == 2            # one full report per RFP
    assert any("Up to 6 model calls" in c.value for c in at.caption)


def test_queue_refuses_more_than_five_files():
    files = [(f"{i}.pdf", f"%PDF {i}".encode(), "application/pdf") for i in range(6)]
    at = open_queue(files)
    assert any("at most 5" in e.value for e in at.error)
    assert at.fake.call_count == 0


def test_queue_shows_failure_reason_above_the_table():
    from triage.llm import LLMError
    at = AppTest.from_file(APP, default_timeout=30)
    files = [("a.pdf", b"%PDF-1.4 fail one", "application/pdf"), ("b.pdf", b"%PDF-1.4 fail two", "application/pdf")]
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key", "QUEUE_PAUSE_SECONDS": "0"}), \
         patch("triage.pipeline.run_triage", side_effect=LLMError("daily free quota used up")):
        at.run()
        at.radio(key="view").set_value("Monday queue").run()
        at.file_uploader(key="queue_files").set_value(files).run()
        at.button[0].click().run()
    assert any("No BIDs" in i.value for i in at.info)
    assert sum("daily free quota used up" in w.value for w in at.warning) == 2


def test_queue_twice_replays_cache_without_error():
    """Regression: ranking the same files a second time (a cache hit) used to crash, because a
    progress message drawn inside the cached function could not be replayed."""
    def fake_run_triage(pdf_bytes, api_key, progress=lambda message: None):
        progress("Extracting...")
        return SAMPLE_REPORT

    at = AppTest.from_file(APP, default_timeout=30)
    files = [("a.pdf", b"%PDF-1.4 replay one", "application/pdf"), ("b.pdf", b"%PDF-1.4 replay two", "application/pdf")]
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key", "QUEUE_PAUSE_SECONDS": "0"}), \
         patch("triage.pipeline.run_triage", side_effect=fake_run_triage):
        at.run()
        at.radio(key="view").set_value("Monday queue").run()
        at.file_uploader(key="queue_files").set_value(files).run()
        at.button[0].click().run()                       # first run: computes and caches
        at.button(key="clear_queue").click().run()
        at.button[0].click().run()                       # second run: served from the cache
    assert not at.exception
    assert not any("could not be analysed" in w.value for w in at.warning), [w.value for w in at.warning]
    assert len(at.expander) == 2


def text_pdf(text: str) -> bytes:
    """A small PDF with real text, so the real PDF reader and pipeline run on it. A random marker
    makes every test run's files new, so the app's on-disk cache never answers instead of the pipeline."""
    import uuid
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(50, 30, uuid.uuid4().hex)
    for page in range(2):
        for line in range(20):
            pdf.drawString(50, 800 - 20 * line, f"{text} page {page + 1} line {line}: proposals are due soon.")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def fake_generate_json(api_key, prompt, schema, system, on_wait=None):
    """Answers every LLM call from the saved sample report (the real pipeline does everything else)."""
    from triage.schemas import Decision, FactsAndDates, RequirementList
    strip = lambda items: [{k: v for k, v in x.items() if k != "evidence"} for x in items]  # noqa: E731
    dates = [dict(d, kind=d.get("kind", "other"), mandatory=d.get("mandatory", False))
             for d in strip(SAMPLE_REPORT["deadlines"])]
    r = SAMPLE_REPORT
    if schema is FactsAndDates:
        return FactsAndDates(facts=r["facts"], dates=dates, ambiguities=r["ambiguities"],
                             clearly_not_our_work=False, not_our_work_reasons=[]), "fake"
    if schema is RequirementList:
        return RequirementList(requirements=strip(r["requirements"])), "fake"
    return Decision(score_factors=r["score_factors"], fit_score=r["fit_score"], decision=r["decision"],
                    reasons=r["reasons"], deal_breakers=r["deal_breakers"],
                    what_would_change_decision=r["what_would_change_decision"],
                    open_questions=r["open_questions"]), "fake"


def test_queue_with_five_files_duplicate_and_scanned_pdf():
    one, two, three = text_pdf("Alpha RFP"), text_pdf("Beta RFP"), text_pdf("Gamma RFP")
    files = [("alpha.pdf", one, "application/pdf"), ("beta.pdf", two, "application/pdf"),
             ("gamma.pdf", three, "application/pdf"), ("alpha-copy.pdf", one, "application/pdf"),
             ("scan.pdf", blank_pdf(), "application/pdf")]
    at = AppTest.from_file(APP, default_timeout=60)
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key", "QUEUE_PAUSE_SECONDS": "0"}), \
         patch("triage.pipeline.generate_json", side_effect=fake_generate_json) as llm:
        at.run()
        at.radio(key="view").set_value("Monday queue").run()
        at.file_uploader(key="queue_files").set_value(files).run()
        assert any("Up to 15 model calls" in c.value for c in at.caption)
        at.button[0].click().run()
        first_calls = llm.call_count
        assert not at.exception
        assert len(at.expander) == 3                                   # alpha, beta, gamma
        assert any("Skipped duplicate: alpha-copy.pdf" in i.value for i in at.info)
        assert any("scan.pdf" in w.value and "scanned" in w.value for w in at.warning)
        assert first_calls == 9                                        # 3 new files x 3 calls; scan makes none

        at.button(key="clear_queue").click().run()
        at.button[0].click().run()                                     # same files again: all from the cache
        assert not at.exception and llm.call_count == first_calls and len(at.expander) == 3

        at.file_uploader(key="queue_files").set_value(files[:3]).run()  # user removes two files
        assert any("files changed since this ranking" in i.value for i in at.info)
