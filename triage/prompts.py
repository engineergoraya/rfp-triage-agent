"""All text sent to the model lives here, so prompts can be read and tuned in one place."""

COMPANY_PROFILE = """\
idiaz.io: a US-registered, AI-native engineering studio with a small senior team.
What we build:
- Automation platforms
- Compliance tooling
- AI-integrated ops software
- Web and mobile apps
Anything not listed here (years in business, past contracts and references, insurance held,
headcount and revenue, small-business or socio-economic certifications such as 8(a), HUBZone,
SDVOSB or WOSB, security or facility clearances, state registrations) is UNKNOWN."""

# Fit score rules. The code re-checks this arithmetic and the threshold (see pipeline.py).
BASELINE_SCORE = 5
BID_THRESHOLD = 6

SYSTEM_INSTRUCTION = """\
You are an RFP triage analyst for a bids team. You read government RFPs carefully and report
only what the document says.
Ground rules:
- Use only the document text you are given. Never guess or use outside knowledge about the agency.
- If something is missing, write exactly "Not stated".
- If something is ambiguous or contradictory, report it as an ambiguity instead of picking one reading.
- Do not add words that change meaning (e.g. do not write "certified" if the text says "self-certify").
- Quotes must be copied character-for-character from the cited page: 5-25 consecutive words, no ellipses."""

DOCUMENT_HEADER = """\
Read the ENTIRE RFP below, including every appendix, attachment, form and table.
Each page starts with a [PAGE n] marker. Use those numbers when citing pages.
"""

FACTS_PROMPT = DOCUMENT_HEADER + """
Return:

1. facts: agency, solicitation number, title, a 2-4 sentence plain-English summary of what the
   client wants, scope of work, contract type (e.g. firm-fixed-price, T&M, IDIQ; "Not stated" if
   the document does not name one), contract value or ceiling, period of performance, place of
   performance, and how to submit (portal/email/address, format, page limits, copies).

2. dates: EVERY date or deadline: issue date, addenda, pre-proposal conferences and RSVP deadlines,
   site visits, questions/Q&A cut-off, answers posted, proposal due, bid opening, demonstrations,
   award, contract/work start, period of performance end, and any others.
   - "date" is YYYY-MM-DD ONLY if year, month and day are all written explicitly for that event.
     If any part is missing or relative ("10 days after award", "week of ...", "March 3" with no
     year, "the same day"), set date to "Not stated", copy the text into as_written and explain in note.
   - If a date was changed (addendum, struck-through text, "extended from X to Y"), list the old and
     new dates as separate items, set superseded=true on the old one, and say in each note which is current.
   - If the same event has two different dates with no explanation, list both and add an ambiguity.

3. ambiguities: anything a bidder would need to clarify. Check at least for: changed or conflicting
   dates, conflicting numbers or page limits, relative or vague dates, mandatory vs desired items
   that are unclear, undefined terms, and requirements that seem to contradict each other.
   Empty list only if there are truly none.

4. clearly_not_our_work: true ONLY if the core work is plainly outside the company profile below
   (for example construction, renovation, janitorial, facilities maintenance, vehicle or goods
   supply, medical services) with no meaningful software or automation deliverable. A minor IT item
   inside a non-software job (e.g. HVAC controls in a renovation) does not count. If true, give up to
   3 short reasons grounded in the document in not_our_work_reasons; otherwise leave it empty.

COMPANY PROFILE:
{profile}

RFP TEXT:
{document}
"""

REQUIREMENTS_PROMPT = DOCUMENT_HEADER + """
List EVERY requirement in the document. Work page by page; be exhaustive. Include both:
  (a) what the BIDDER must be or hold: minimum qualifications, set-asides, certifications,
      clearances, background checks, past performance and references, key personnel and staffing,
      licenses and registrations, insurance and bonds, mandatory meetings or site visits,
      required forms and submission rules (format, page limits, copies, delivery method);
  (b) what the WORK or SOLUTION must do: every functional requirement (each numbered or [M]/[D]
      row is its own item), integrations, data migration, security and hosting, accessibility,
      service levels and availability, acceptance criteria, support, training, reporting.

Categories (pick the best fit):
- skill: technical capabilities the work needs (features, integrations, AI, security controls like SSO/MFA/encryption, accessibility, SLAs)
- certification: formal certifications or attestations (SOC 2, FedRAMP, ISO, CMMI, Green Seal, ...)
- set_aside: small business, 8(a), HUBZone, SDVOSB, WOSB, MBE/WBE goals, local preference
- security_clearance: ONLY government security clearances and personnel background checks
- past_performance: years in business, prior projects, references
- staffing: key personnel, minimum labor hours, on-site presence, local supervisors, wages
- license_or_registration: business registration, SAM, state licenses
- insurance_or_bonding: insurance limits, bid/performance/fidelity bonds
- other: submission rules, forms, pricing caps, contract terms a bidder must accept

Rules:
- One requirement per item. Do not merge unrelated requirements; do not invent any.
- mandatory = true for must / shall / required / [M] / "will be rejected"; false for should / desired / [D].
- requirement: plain English, faithful to the text.

RFP TEXT:
{document}
"""

DECIDE_PROMPT = """\
Decide whether the company below should bid on this RFP. Use ONLY the extracted facts (JSON below)
and the company profile. Do not invent facts about the RFP or the company.

COMPANY PROFILE:
{profile}

SCORING RULES:
- Start from a baseline of {baseline} out of 10.
- List each factor that moves the score in score_factors with its points (+ or -) and a one-line
  reason citing the extracted facts. Score what the RFP shows:
  + match with our service lines, technical work we clearly do, remote-friendly delivery,
    a budget and scope that suit a small senior team;
  - work outside our service lines, large on-site or headcount demands, clearances, socio-economic
    set-asides, unrealistic budget for the scope, very short timeline, heavy non-software scope.
- Things the profile does not tell us (years in business, references, insurance, size
  thresholds) are NOT negative points. Put them in what_would_change_decision as items the team
  must confirm. You may use at most ONE factor of -1 for "eligibility to confirm".
- fit_score = {baseline} + sum of all points, clamped to 0..10. Double-check the arithmetic.
- decision is "BID" only if fit_score >= {threshold} AND deal_breakers is empty; otherwise "NO_BID".
  (Threshold rationale: a proposal costs a small team about a week, so we bid only when the
  evidence of fit is clearly net positive.)

ALSO RETURN:
- reasons: exactly 3, the strongest reasons FOR THE DECISION YOU MADE, most important first.
- deal_breakers: mandatory requirements that would disqualify us outright: a socio-economic
  set-aside (8(a), HUBZone, SDVOSB, WOSB), a required government security or facility clearance,
  or core work that is not software. A plain "small business" set-aside is NOT a deal-breaker
  (a small team likely qualifies); unknown company facts are NOT deal-breakers. Empty list if none.
- what_would_change_decision: concrete facts that would flip the decision, including the
  company facts the team must confirm (e.g. "we have fewer than 3 years in business").
- open_questions: specific questions to send the agency, based on ambiguities, missing info or
  unclear requirements. Do not ask about things the document already answers.
{correction}
EXTRACTED FACTS:
{extraction_json}
"""

# Sent back to the model, once, if its score or decision broke the rules above.
CORRECTION_NOTE = """
CORRECTION: in your previous answer {problem}. Recompute fit_score from your factors, apply the
decision rule, and make sure the three reasons support the decision.
"""
