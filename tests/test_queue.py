"""Monday queue: ranking, dates, CSV safety, failure handling, rate limits, Word export. No API calls."""

import csv
import io
import zipfile
from datetime import date
from unittest.mock import patch

import pytest

from tests.test_llm import GOOD_JSON, api_error, run
from triage import llm
from triage.llm import LLMError
from triage.pdf_reader import PdfError
from triage.ranking import (build_row, proposal_due, recommendation_line, run_queue, sort_rows,
                            to_csv)
from triage.report_docx import build_docx
from triage.schemas import RequirementList

TODAY = date(2026, 11, 24)


def due(day, superseded=False, kind="proposal_due", mandatory=False, what="Proposals due"):
    return {"what": what, "date": day, "kind": kind, "superseded": superseded, "mandatory": mandatory}


def report(decision="BID", score=8, deadlines=None, agency="City of X", deal_breakers=None):
    return {"decision": decision, "fit_score": score, "facts": {"agency": agency},
            "deadlines": deadlines if deadlines is not None else [due("2027-01-15")],
            "deal_breakers": deal_breakers or []}


def ranked(*rows):
    return [r["File"] for r in sort_rows(list(rows))]


# ---------------- choosing the proposal deadline ----------------

def test_proposal_due_ignores_superseded_and_other_kinds():
    dates = [due("2027-01-08", superseded=True), due("2027-01-15"), due("2027-02-01", kind="start_date")]
    assert proposal_due(dates) == ("2027-01-15", False)


def test_several_current_proposal_dates_take_latest_and_flag():
    assert proposal_due([due("2027-01-08"), due("2027-01-15")]) == ("2027-01-15", True)
    row = build_row(report(deadlines=[due("2027-01-08"), due("2027-01-15")]), "a.pdf", TODAY)
    assert "⚠️" in row["Proposal due"]


def test_missing_or_not_stated_deadline():
    assert proposal_due([due("Not stated")]) == ("Not stated", False)
    assert proposal_due([]) == ("Not stated", False)
    assert proposal_due([{"what": "Proposals due", "date": "2027-01-15"}]) == ("Not stated", False)  # old report, no kind
    row = build_row(report(deadlines=[]), "a.pdf", TODAY)
    assert row["Days left"] is None and row["Status"] == "Live"


def test_days_left_and_expired_rule():
    assert build_row(report(deadlines=[due("2026-12-24")]), "a.pdf", TODAY)["Days left"] == 30
    assert build_row(report(deadlines=[due("2026-11-24")]), "a.pdf", TODAY)["Status"] == "Live"  # due today
    expired = build_row(report(deadlines=[due("2026-11-23")]), "a.pdf", TODAY)
    assert expired["Status"] == "Expired" and expired["Days left"] == -1


def test_next_must_attend_is_earliest_upcoming_mandatory_date():
    dates = [due("2027-01-15"), due("2026-12-03", kind="site_visit", mandatory=True, what="Site walk"),
             due("2026-11-01", kind="site_visit", mandatory=True, what="Past walk"),
             due("2026-12-10", kind="other", mandatory=False, what="Optional call")]
    assert build_row(report(deadlines=dates), "a.pdf", TODAY)["Next must-attend event"] == "Site walk (2026-12-03)"


# ---------------- ranking ----------------

def test_sort_order_bid_then_no_bid_then_expired_and_failed():
    rows = [build_row(None, "failed.pdf", TODAY, "scanned"),
            build_row(report("BID", 9, [due("2026-01-01")]), "expired.pdf", TODAY),
            build_row(report("NO_BID", 3), "nobid.pdf", TODAY),
            build_row(report("BID", 6), "bid.pdf", TODAY)]
    assert ranked(*rows) == ["bid.pdf", "nobid.pdf", "expired.pdf", "failed.pdf"]
    assert [r["Rank"] for r in sort_rows(rows)] == [1, 2, 3, 4]


def test_tie_breaks_score_then_earlier_deadline_then_filename():
    rows = [build_row(report("BID", 7, [due("2027-01-15")]), "b.pdf", TODAY),
            build_row(report("BID", 7, [due("Not stated")]), "a.pdf", TODAY),
            build_row(report("BID", 7, [due("2026-12-15")]), "c.pdf", TODAY),
            build_row(report("BID", 7, [due("2026-12-15")]), "a2.pdf", TODAY),
            build_row(report("BID", 9, [due("2027-03-01")]), "z.pdf", TODAY)]
    assert ranked(*rows) == ["z.pdf", "a2.pdf", "c.pdf", "b.pdf", "a.pdf"]


def test_recommendation_line_mixed_and_no_bid():
    rows = sort_rows([build_row(report("NO_BID", 2), "n.pdf", TODAY),
                      build_row(report("BID", 8, [due("2027-01-15")], agency="Port Alder"), "p.pdf", TODAY)])
    assert recommendation_line(rows) == "Start with: Port Alder (p.pdf): BID, fit 8/10, proposal due in 52 days."
    only_no = sort_rows([build_row(report("NO_BID", 2), "n.pdf", TODAY), build_row(None, "f.pdf", TODAY, "x")])
    assert recommendation_line(only_no) == "No BIDs in this batch."


# ---------------- CSV ----------------

def test_csv_guards_against_formula_injection():
    evil = report("NO_BID", 2, agency="=HYPERLINK(\"http://evil\")", deal_breakers=["+1 must be bonded", "x"])
    rows = sort_rows([build_row(evil, "@evil.pdf", TODAY), build_row(report(), "ok.pdf", TODAY)])
    data = list(csv.DictReader(io.StringIO(to_csv(rows).decode("utf-8-sig"))))
    bad = next(r for r in data if "evil" in r["File"])
    assert bad["Agency"].startswith("'=") and bad["Biggest deal-breaker"].startswith("'+")
    assert bad["File"] == "'@evil.pdf"
    assert "_group" not in data[0] and data[0]["Rank"] == "1"


def test_csv_keeps_negative_numbers_as_numbers():
    rows = sort_rows([build_row(report(deadlines=[due("2026-11-20")]), "old.pdf", TODAY)])
    assert next(csv.DictReader(io.StringIO(to_csv(rows).decode("utf-8-sig"))))["Days left"] == "-4"


# ---------------- running the queue ----------------

def test_queue_continues_after_one_file_fails():
    def fake_triage(data):
        if data == b"scan":
            raise PdfError("This PDF looks scanned.")
        if data == b"boom":
            raise RuntimeError("bad")
        return report()

    files = [("scan.pdf", b"scan"), ("good.pdf", b"good"), ("boom.pdf", b"boom")]
    result = run_queue(files, fake_triage, TODAY, pause_seconds=0)
    assert [r["File"] for r in result["rows"]] == ["good.pdf", "boom.pdf", "scan.pdf"]
    assert result["rows"][2]["Status"] == "Failed: This PDF looks scanned."
    assert result["rows"][1]["Status"].startswith("Failed: Unexpected error")
    assert list(result["reports"]) == ["good.pdf"]


def test_llm_error_becomes_failed_row():
    def fake_triage(data):
        raise LLMError("Still rate-limited")
    result = run_queue([("a.pdf", b"a")], fake_triage, TODAY, pause_seconds=0)
    assert result["rows"][0]["Decision"] == "Failed" and "rate-limited" in result["rows"][0]["Status"]


def test_duplicate_files_processed_once_and_pause_between_files():
    calls, sleeps = [], []
    fake = lambda data: calls.append(data) or report()  # noqa: E731
    files = [("a.pdf", b"same"), ("a-copy.pdf", b"same"), ("b.pdf", b"other")]
    result = run_queue(files, fake, TODAY, pause_seconds=2, sleep=sleeps.append)
    assert calls == [b"same", b"other"] and sleeps == [2]
    assert result["skipped"] == ["a-copy.pdf (same file as a.pdf)"] and len(result["rows"]) == 2


def test_more_than_five_files_is_refused():
    with pytest.raises(ValueError):
        run_queue([(f"{i}.pdf", bytes([i])) for i in range(6)], lambda d: report(), TODAY)


# ---------------- rate limits (llm.py) ----------------

def test_rate_limit_waits_15_then_30_then_succeeds():
    limit = api_error(429, "RESOURCE_EXHAUSTED: GenerateRequestsPerMinutePerProjectPerModel")
    replies, waits = [limit, limit, GOOD_JSON], []

    def fake_call(client, model, prompt, schema, system, set_thinking=True):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    with patch.dict("os.environ", {"GEMINI_MODEL": "m", "GEMINI_FALLBACK_MODEL": "m"}), \
         patch.object(llm, "_call", side_effect=fake_call), patch.object(llm.time, "sleep") as sleep:
        _, model = llm.generate_json("key", "p", RequirementList, "s", on_wait=waits.append)
    assert [c.args[0] for c in sleep.call_args_list] == [15, 30] and waits == [15, 30] and model == "m"


def test_rate_limit_gives_up_after_three_retries_without_trying_other_models():
    limit = api_error(429, "RESOURCE_EXHAUSTED per minute")
    with pytest.raises(LLMError, match="after 3 retries"):
        run([limit, limit, limit, limit, GOOD_JSON])  # 1 try + 3 retries, never reaches the fallback


def test_invalid_key_is_not_retried():
    replies = [api_error(401, "unauthenticated"), GOOD_JSON]
    with pytest.raises(LLMError, match="API key"):
        run(replies)
    assert replies == [GOOD_JSON]  # only one call was made


# ---------------- Word export ----------------

def test_docx_is_a_valid_zip_with_escaped_text():
    full = {"decision": "BID", "fit_score": 8, "facts": {"title": "A & B <test>", "summary": "S\x0c1",
                                                         "agency": "X"},
            "deadlines": [], "requirements": [], "score_factors": [], "reasons": ["r1", "r2", "r3"],
            "deal_breakers": [], "what_would_change_decision": [], "open_questions": [],
            "ambiguities": [], "warnings": []}
    with zipfile.ZipFile(io.BytesIO(build_docx(full))) as docx:
        xml = docx.read("word/document.xml").decode()
        assert set(docx.namelist()) == {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
    assert "A &amp; B &lt;test&gt;" in xml and "\x0c" not in xml
