"""The agent: PDF bytes in, triage report (a plain dict) out.

    read_pdf -> extract (LLM calls 1a + 1b, in parallel) -> check_evidence -> fast no-bid gate
                                                          |-> fast_no_bid (no LLM)
                                                          |-> decide (LLM call 2) -> enforce_consistency
             -> assemble report

Each stage is one plain function, so it is easy to test, time and explain.
"""

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import evidence
from .llm import generate_json, models_to_try
from .pdf_reader import check_length, extract_pages, tag_pages
from .prompts import (BASELINE_SCORE, BID_THRESHOLD, COMPANY_PROFILE, CORRECTION_NOTE, DECIDE_PROMPT,
                      FACTS_PROMPT, REQUIREMENTS_PROMPT, SYSTEM_INSTRUCTION)
from .schemas import (NOT_STATED, Decision, Extraction, FactsAndDates, RequirementList, ScoreFactor,
                      TriageReport)


def read_pdf(pdf_bytes: bytes) -> tuple[list[str], str]:
    pages = extract_pages(pdf_bytes)
    tagged = tag_pages(pages)
    check_length(tagged)
    return pages, tagged


def extract(api_key: str, tagged_text: str, on_wait=None) -> tuple[Extraction, str]:
    """Two focused LLM calls at the same time: (a) facts + dates + gate, (b) requirements only.

    One big call missed requirements and its output was long (slow). Two smaller jobs gave better
    recall, and because they run in parallel the wait is only as long as the slower one.
    """
    facts_prompt = FACTS_PROMPT.format(profile=COMPANY_PROFILE, document=tagged_text)
    requirements_prompt = REQUIREMENTS_PROMPT.format(document=tagged_text)
    with ThreadPoolExecutor(max_workers=2) as pool:
        facts_job = pool.submit(generate_json, api_key, facts_prompt, FactsAndDates, SYSTEM_INSTRUCTION,
                                on_wait)
        requirements_job = pool.submit(generate_json, api_key, requirements_prompt, RequirementList,
                                       SYSTEM_INSTRUCTION, on_wait)
        facts, facts_model = facts_job.result()
        requirements, requirements_model = requirements_job.result()

    extraction = Extraction(**facts.model_dump(), requirements=requirements.requirements)
    model = facts_model if facts_model == requirements_model else f"{facts_model} + {requirements_model}"
    return extraction, model


def clean_dates(extraction: Extraction) -> list[str]:
    """Enforce the date rule in code: anything that is not a real YYYY-MM-DD becomes 'Not stated'."""
    warnings = []
    for item in extraction.dates:
        value = item.date.strip()
        if value == NOT_STATED:
            continue
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)  # rejects impossible dates like 2026-02-30
        except ValueError:
            warnings.append(f"'{item.what}': model gave '{value}', which is not an exact date; set to Not stated.")
            item.note = (item.note + " " if item.note else "") + f"Model output '{value}' was not an exact date."
            item.date = NOT_STATED
    return warnings


def check_evidence(extraction: Extraction, pages: list[str]) -> tuple[list[dict], list[dict], list[str]]:
    """Attach an evidence label to every date and requirement (bonus feature 1)."""
    warnings = []
    dates = []
    for item in extraction.dates:
        row = item.model_dump()
        row["evidence"] = evidence.check_quote(item.quote, item.page, pages)
        row["evidence"]["date_matches_quote"] = evidence.date_matches_quote(item.date, item.quote)
        if row["evidence"]["date_matches_quote"] is False:
            warnings.append(f"Date for '{item.what}' ({item.date}) does not match its quote; check it by hand.")
        dates.append(row)

    requirements = []
    for item in extraction.requirements:
        row = item.model_dump()
        row["evidence"] = evidence.check_quote(item.quote, item.page, pages)
        requirements.append(row)
    return dates, requirements, warnings


def fast_no_bid(extraction: Extraction) -> Decision:
    """Skip the second LLM call when the work is clearly not ours (e.g. construction)."""
    reasons = list(extraction.not_our_work_reasons[:3])
    filler = [
        "The core work is outside idiaz.io's service lines (automation platforms, compliance tooling, "
        "AI-integrated ops software, web and mobile apps).",
        "There is no meaningful software or automation component for a small senior engineering team to deliver.",
        "Writing a proposal would cost about a week with no realistic path to winning or delivering.",
    ]
    for line in filler:
        if len(reasons) >= 3:
            break
        if line not in reasons:
            reasons.append(line)
    return Decision(
        score_factors=[ScoreFactor(factor="Type of work", points=-BASELINE_SCORE,
                                   reason=reasons[0])],
        fit_score=0,
        decision="NO_BID",
        reasons=reasons,
        deal_breakers=["The work is clearly not software: " + reasons[0]],
        what_would_change_decision=[
            "A substantial software, automation or compliance-tooling component in the scope that "
            "idiaz.io could deliver as a prime or subcontractor."
        ],
        open_questions=[],
    )


def computed_score(decision: Decision) -> int:
    return max(0, min(10, BASELINE_SCORE + sum(f.points for f in decision.score_factors)))


def expected_decision(score: int, deal_breakers: list[str]) -> str:
    return "BID" if score >= BID_THRESHOLD and not deal_breakers else "NO_BID"


def find_inconsistency(decision: Decision) -> str:
    """Describe how the model broke the scoring rules, or return "" if it did not."""
    score = computed_score(decision)
    if decision.fit_score != score:
        return f"fit_score was {decision.fit_score} but {BASELINE_SCORE} + your factor points = {score}"
    expected = expected_decision(score, decision.deal_breakers)
    if decision.decision != expected:
        return f"decision was {decision.decision} but the rule gives {expected}"
    if len(decision.reasons) != 3:
        return f"you gave {len(decision.reasons)} reasons instead of exactly 3"
    return ""


def decide(api_key: str, extraction: Extraction, on_wait=None) -> tuple[Decision, str]:
    """LLM call 2. If the answer breaks the scoring rules, ask once more with the problem spelled
    out, so the reasons are rewritten to match the corrected decision (not just relabelled)."""
    def ask(correction: str):
        prompt = DECIDE_PROMPT.format(
            profile=COMPANY_PROFILE, baseline=BASELINE_SCORE, threshold=BID_THRESHOLD,
            correction=correction, extraction_json=extraction.model_dump_json(indent=1),
        )
        return generate_json(api_key, prompt, Decision, SYSTEM_INSTRUCTION, on_wait)

    decision, model = ask("")
    problem = find_inconsistency(decision)
    if problem:
        decision, model = ask(CORRECTION_NOTE.format(problem=problem))
    return decision, model


def enforce_consistency(decision: Decision) -> list[str]:
    """Last plain-code guard, after the retry in decide(): the score must equal baseline + factor
    points, and the decision must follow the threshold rule. Fix and warn if still broken."""
    warnings = []
    score = computed_score(decision)
    if decision.fit_score != score:
        warnings.append(f"Model's fit score {decision.fit_score} did not match its factors; corrected to {score}.")
        decision.fit_score = score

    expected = expected_decision(decision.fit_score, decision.deal_breakers)
    if decision.decision != expected:
        warnings.append(f"Model said {decision.decision}, but the rule (score >= {BID_THRESHOLD} and "
                        f"no deal-breakers) gives {expected}; corrected. Read the reasons with care.")
        decision.decision = expected

    if len(decision.reasons) != 3:
        warnings.append(f"Model gave {len(decision.reasons)} reasons instead of 3.")
        decision.reasons = decision.reasons[:3]
    return warnings


def run_triage(pdf_bytes: bytes, api_key: str, progress=lambda message: None) -> dict:
    """Run the whole agent. `progress` is an optional callback for UI status messages."""
    started = time.time()
    stage_seconds = {}  # shown in the report meta, so slow runs can be explained
    on_wait = lambda seconds: progress(f"Waiting for the free-tier rate limit ({seconds} s)...")  # noqa: E731

    progress("Reading the PDF...")
    pages, tagged = read_pdf(pdf_bytes)

    progress(f"Extracting facts, dates and requirements from {len(pages)} pages (2 parallel calls)...")
    t = time.time()
    extraction, extract_model = extract(api_key, tagged, on_wait)
    stage_seconds["extract"] = round(time.time() - t, 1)
    warnings = clean_dates(extraction)

    if extract_model != models_to_try()[0]:
        warnings.append(f"The primary model was unavailable, so a fallback ({extract_model}) was used. "
                        "Fallback models can be less thorough; check the requirements list with care.")

    progress("Checking every quote against the PDF...")
    dates, requirements, evidence_warnings = check_evidence(extraction, pages)
    warnings += evidence_warnings

    if extraction.clearly_not_our_work:
        progress("Clearly not our type of work: fast no-bid.")
        decision, decide_model = fast_no_bid(extraction), "none (fast no-bid)"
    else:
        progress("Scoring fit and making the bid decision...")
        t = time.time()
        decision, decide_model = decide(api_key, extraction, on_wait)
        stage_seconds["decide"] = round(time.time() - t, 1)
        warnings += enforce_consistency(decision)

    report = TriageReport(
        decision=decision.decision,
        fit_score=decision.fit_score,
        deadlines=dates,
        requirements=requirements,
        reasons=decision.reasons,
        fast_no_bid=extraction.clearly_not_our_work,
        facts=extraction.facts,
        score_factors=decision.score_factors,
        deal_breakers=decision.deal_breakers,
        what_would_change_decision=decision.what_would_change_decision,
        open_questions=decision.open_questions,
        ambiguities=extraction.ambiguities,
        warnings=warnings,
        meta={
            "pages": len(pages),
            "file_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "extract_model": extract_model,
            "decide_model": decide_model,
            "bid_threshold": BID_THRESHOLD,
            "seconds": round(time.time() - started, 1),
            "stage_seconds": stage_seconds,
        },
    )
    # Round-trip through JSON so the UI and the download get exactly the same plain data.
    return json.loads(report.model_dump_json())
