"""Data shapes for the pipeline.

The two LLM schemas (Extraction, Decision) are passed to Gemini as `response_schema`,
so the model is forced to answer in exactly this JSON shape. Pydantic then validates
the answer. They deliberately avoid defaults and numeric constraints, because Gemini's
schema support is a subset of JSON Schema; extra rules are checked in plain code.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

NOT_STATED = "Not stated"

# ---------- Stage 1: extraction (what the document says) ----------


class Facts(BaseModel):
    agency: str
    solicitation_number: str
    title: str
    summary: str = Field(description="2-4 plain-English sentences: what the client wants and why.")
    scope_of_work: str
    contract_type: str
    contract_value: str
    period_of_performance: str
    place_of_performance: str
    how_to_submit: str


class DateItem(BaseModel):
    what: str = Field(description="e.g. 'Questions due', 'Proposal due', 'Site visit', 'Work start'.")
    date: str = Field(description="YYYY-MM-DD only if year, month and day are all explicit, else 'Not stated'.")
    time: str = Field(description="Time and timezone as written, or 'Not stated'.")
    as_written: str = Field(description="The date exactly as the document writes it.")
    quote: str = Field(description="Short verbatim quote from the page that contains the date.")
    page: int
    note: str = Field(description="Why the date is incomplete or ambiguous; empty string if not needed.")
    superseded: bool = Field(description="True if an addendum or revision replaced this date with a new one.")


RequirementCategory = Literal[
    "skill", "certification", "set_aside", "security_clearance",
    "past_performance", "staffing", "license_or_registration", "insurance_or_bonding", "other",
]


class Requirement(BaseModel):
    category: RequirementCategory
    requirement: str = Field(description="The requirement in plain English.")
    mandatory: bool = Field(description="True if the document says must/shall/required.")
    quote: str = Field(description="Short verbatim quote from the page.")
    page: int


class Ambiguity(BaseModel):
    issue: str
    page: int


# Stage 1 is two LLM calls run in parallel (see pipeline.extract): one for facts and dates,
# one only for requirements. A smaller job per call gave better recall and lower latency.


class FactsAndDates(BaseModel):
    facts: Facts
    dates: list[DateItem]
    ambiguities: list[Ambiguity]
    clearly_not_our_work: bool
    not_our_work_reasons: list[str] = Field(
        description="If clearly_not_our_work is true: up to 3 reasons grounded in the document. Else empty."
    )


class RequirementList(BaseModel):
    requirements: list[Requirement]


class Extraction(FactsAndDates):
    """Both stage-1 answers merged in code. This is what stage 2 and the evidence check use."""
    requirements: list[Requirement]


# ---------- Stage 2: decision (what we should do about it) ----------


class ScoreFactor(BaseModel):
    factor: str
    points: int = Field(description="How much this factor moved the score from the baseline of 5.")
    reason: str = Field(description="One line, citing the extracted facts.")


class Decision(BaseModel):
    score_factors: list[ScoreFactor]
    fit_score: int
    decision: Literal["BID", "NO_BID"]
    reasons: list[str] = Field(description="Exactly the three strongest reasons for the decision.")
    deal_breakers: list[str]
    what_would_change_decision: list[str]
    open_questions: list[str] = Field(description="Questions to ask the agency before deciding.")


# ---------- Final report (what the UI shows and the JSON download contains) ----------


class Evidence(BaseModel):
    status: Literal["verified", "wrong_page", "not_found"]
    found_on_page: Optional[int] = None
    # Only for dates: does the parsed YYYY-MM-DD agree with the quote's text?
    date_matches_quote: Optional[bool] = None


class CheckedDate(DateItem):
    evidence: Evidence


class CheckedRequirement(Requirement):
    evidence: Evidence


class TriageReport(BaseModel):
    # The five fields required by the brief's output shape come first.
    decision: Literal["BID", "NO_BID"]
    fit_score: int
    deadlines: list[CheckedDate]
    requirements: list[CheckedRequirement]
    reasons: list[str]
    # Everything else.
    fast_no_bid: bool
    facts: Facts
    score_factors: list[ScoreFactor]
    deal_breakers: list[str]
    what_would_change_decision: list[str]
    open_questions: list[str]
    ambiguities: list[Ambiguity]
    warnings: list[str]
    meta: dict
