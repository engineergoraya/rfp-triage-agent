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
