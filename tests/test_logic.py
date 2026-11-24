"""Tests for everything that is plain code (no real model calls)."""

from unittest.mock import patch

import pytest

from triage import pipeline
from triage.calendar_export import build_ics, count_exportable
from triage.evidence import check_quote, date_matches_quote, normalize
from triage.pdf_reader import PdfError, tag_pages
from triage.schemas import Decision, Extraction, FactsAndDates, RequirementList, ScoreFactor

PAGES = [
    "Cover page. Proposals are due January 15, 2027 at 2:00 PM Eastern Time.",
    "Offerors must be regis-\ntered in SAM.gov with an active\nUnique Entity ID.",
    "Appendix C. The Contractor shall host all data in a FedRAMP Moderate environment.",
]


# ---------------- evidence check ----------------

def test_normalize_ignores_case_punctuation_and_line_breaks():
    assert normalize("Regis-\ntered  in “SAM.gov”") == normalize("registered in SAM.gov")


def test_quote_verified_on_cited_page_despite_pdf_line_breaks():
    result = check_quote("registered in SAM.gov with an active Unique Entity ID", 2, PAGES)
    assert result == {"status": "verified", "found_on_page": 2}


def test_quote_on_other_page_is_wrong_page():
    result = check_quote("host all data in a FedRAMP Moderate environment", 1, PAGES)
    assert result == {"status": "wrong_page", "found_on_page": 3}


def test_invented_quote_is_not_found():
    result = check_quote("Offerors must hold a Top Secret facility clearance", 2, PAGES)
    assert result == {"status": "not_found", "found_on_page": None}


def test_quote_across_page_break_is_verified():
    result = check_quote("Eastern Time. Offerors must be registered", 1, PAGES)
    assert result["status"] == "verified"


def test_out_of_range_page_still_searches_document():
    assert check_quote("Proposals are due January 15, 2027", 99, PAGES)["status"] == "wrong_page"


def test_empty_quote_is_not_found():
    assert check_quote("", 1, PAGES)["status"] == "not_found"


@pytest.mark.parametrize("iso, quote, expected", [
    ("2027-01-15", "Proposals are due January 15, 2027 at 2:00 PM", True),
    ("2027-01-15", "due 01/15/2027", True),
    ("2027-01-16", "Proposals are due January 15, 2027", False),   # wrong day
    ("2026-01-15", "Proposals are due January 15, 2027", False),   # wrong year
    ("Not stated", "Anticipated award: February 2027", None),      # no exact date
])
def test_date_matches_quote(iso, quote, expected):
    assert date_matches_quote(iso, quote) is expected


# ---------------- calendar export ----------------

DEADLINES = [
    {"what": "Proposal due", "date": "2027-01-15", "time": "2:00 PM ET", "page": 1,
     "quote": "Proposals are due January 15, 2027, at 2:00 PM; late bids rejected"},
    {"what": "Contract award", "date": "Not stated", "time": "Not stated", "page": 3, "quote": "February 2027"},
    {"what": "Proposal due (old)", "date": "2027-01-08", "time": "Not stated", "page": 1, "quote": "x",
     "superseded": True, "kind": "other", "mandatory": False},
]


def test_ics_contains_only_current_exact_dates():
    ics = build_ics(DEADLINES, "Test RFP")
    assert ics.count("BEGIN:VEVENT") == 1 and count_exportable(DEADLINES) == 1
    assert "DTSTART;VALUE=DATE:20270115" in ics and "DTEND;VALUE=DATE:20270116" in ics
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")


def test_ics_escapes_and_folds_lines():
    ics = build_ics(DEADLINES, "Test RFP")
    assert "2027\\, at 2:00 PM\\; late" in ics.replace("\r\n ", "")  # commas/semicolons escaped
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))  # folded per RFC 5545


# ---------------- pipeline: gate, consistency, JSON shape ----------------

def make_extraction(not_ours: bool) -> Extraction:
    return Extraction.model_validate({
        "facts": {k: "Not stated" for k in ["agency", "solicitation_number", "title", "summary", "scope_of_work",
                                             "contract_type", "contract_value", "period_of_performance",
                                             "place_of_performance", "how_to_submit"]},
        "dates": [{"what": "Proposal due", "date": "2027-01-15", "time": "2:00 PM ET",
                   "as_written": "January 15, 2027", "quote": "Proposals are due January 15, 2027", "page": 1, "note": "",
                   "superseded": False, "kind": "other", "mandatory": False},
                  {"what": "Award", "date": "February 2027", "time": "Not stated",
                   "as_written": "February 2027", "quote": "February 2027", "page": 1, "note": "", "superseded": False, "kind": "other", "mandatory": False}],
        "requirements": [{"category": "license_or_registration", "requirement": "SAM.gov registration",
                          "mandatory": True, "quote": "registered in SAM.gov with an active Unique Entity ID", "page": 2}],
        "ambiguities": [],
        "clearly_not_our_work": not_ours,
        "not_our_work_reasons": ["The work is renovation of a fire station."] if not_ours else [],
    })


GOOD_DECISION = Decision(
    score_factors=[ScoreFactor(factor="Service match", points=3, reason="Compliance tooling.")],
    fit_score=8, decision="BID", reasons=["a", "b", "c"], deal_breakers=[],
    what_would_change_decision=["x"], open_questions=["y"],
)


def run_with_mocks(extraction, decision=None):
    """Run the pipeline with the PDF reader and the LLM replaced by fakes."""
    calls = []
    answers = {
        FactsAndDates: FactsAndDates.model_validate(extraction.model_dump(exclude={"requirements"})),
        RequirementList: RequirementList(requirements=extraction.requirements),
        Decision: decision,
    }

    def fake_generate_json(api_key, prompt, schema, system, on_wait=None):
        calls.append(schema.__name__)
        return answers[schema], "fake-model"

    with patch.object(pipeline, "read_pdf", return_value=(PAGES, tag_pages(PAGES))), \
         patch.object(pipeline, "generate_json", side_effect=fake_generate_json):
        return pipeline.run_triage(b"%PDF fake", "fake-key"), calls


def test_fast_no_bid_skips_second_llm_call():
    report, calls = run_with_mocks(make_extraction(not_ours=True))
    assert sorted(calls) == ["FactsAndDates", "RequirementList"]  # decide() was never called
    assert report["decision"] == "NO_BID" and report["fit_score"] == 0 and report["fast_no_bid"]
    assert len(report["reasons"]) == 3


def test_normal_path_calls_decide_and_has_required_json_shape():
    report, calls = run_with_mocks(make_extraction(not_ours=False), GOOD_DECISION.model_copy(deep=True))
    assert sorted(calls) == ["Decision", "FactsAndDates", "RequirementList"]
    # The five keys from the brief's output shape:
    assert report["decision"] in ("BID", "NO_BID")
    assert isinstance(report["fit_score"], int) and 0 <= report["fit_score"] <= 10
    assert all({"what", "date"} <= set(d) for d in report["deadlines"])
    assert isinstance(report["requirements"], list) and len(report["reasons"]) == 3
    # Evidence labels were attached by code:
    assert report["requirements"][0]["evidence"]["status"] == "verified"


def test_non_exact_date_is_forced_to_not_stated():
    report, _ = run_with_mocks(make_extraction(not_ours=False), GOOD_DECISION.model_copy(deep=True))
    award = next(d for d in report["deadlines"] if d["what"] == "Award")
    assert award["date"] == "Not stated" and "February 2027" in award["note"]


def test_inconsistent_score_and_decision_are_corrected():
    bad = GOOD_DECISION.model_copy(deep=True)
    bad.fit_score = 9                      # factors say 5 + 3 = 8
    bad.deal_breakers = ["Requires 8(a)"]  # any deal-breaker means NO_BID
    warnings = pipeline.enforce_consistency(bad)
    assert bad.fit_score == 8 and bad.decision == "NO_BID" and len(warnings) == 2


def test_scanned_pdf_gives_clear_error():
    from triage.pdf_reader import extract_pages
    with patch("triage.pdf_reader.PdfReader") as reader:
        reader.return_value.pages = [type("P", (), {"extract_text": lambda self: ""})()] * 3
        with pytest.raises(PdfError, match="scanned"):
            extract_pages(b"%PDF fake")


def test_decide_retries_once_when_model_breaks_the_rules():
    bad = GOOD_DECISION.model_copy(deep=True)
    bad.fit_score = 9  # factors say 8
    replies = [bad, GOOD_DECISION.model_copy(deep=True)]
    prompts = []

    def fake_generate_json(api_key, prompt, schema, system, on_wait=None):
        prompts.append(prompt)
        return replies.pop(0), "fake-model"

    with patch.object(pipeline, "generate_json", side_effect=fake_generate_json):
        decision, _ = pipeline.decide("key", make_extraction(not_ours=False))
    assert len(prompts) == 2 and "CORRECTION" in prompts[1] and "CORRECTION" not in prompts[0]
    assert decision.fit_score == 8 and pipeline.find_inconsistency(decision) == ""
