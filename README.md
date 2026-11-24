# RFP Triage Agent

Drop in a government RFP (PDF). In about a minute, get a **bid / no-bid recommendation for idiaz.io**:
a plain-English summary, every key date, every requirement, a 0–10 fit score with the reason for each
point, the three strongest reasons, deal-breakers, what would change the decision, and questions to ask
the agency. Every date and requirement is traced back to a verbatim quote and page, and **plain code
(not the AI) checks each quote against the PDF** so a reviewer knows exactly which items to recheck.

**Live app:** `<<PASTE STREAMLIT URL HERE>>`

![flow](https://img.shields.io/badge/PDF-%E2%86%92%20extract%20%E2%86%92%20verify%20%E2%86%92%20decide%20%E2%86%92%20report-orange)

---

## What you get

| Report section | Where it comes from |
|---|---|
| 1. Summary + facts (agency, scope, contract type and value, period and place of performance, how to submit) | LLM call 1a |
| 2. Key dates, as `YYYY-MM-DD` only when year, month and day are all explicit, else "Not stated" plus a note. Superseded dates (addenda) are flagged | LLM call 1a, then checked by code |
| 3. Requirements (skills, certifications, set-asides, clearances, past performance, staffing, licences, insurance, submission rules), each marked mandatory or desired | LLM call 1b |
| 4. Fit score 0–10: baseline 5, plus or minus each factor, each with a one-line reason | LLM call 2, arithmetic re-checked by code |
| 5. BID / NO_BID: 3 strongest reasons, deal-breakers, what would change the decision, open questions, ambiguities | LLM call 2, decision rule re-checked by code |
| Evidence check tab ("X of Y quotes verified", riskiest first) | **plain Python** |
| Calendar export (.ics) | **plain Python** |
| JSON tab + download (same data, machine-readable) | — |

The JSON always contains the shape from the brief, plus everything else:

```json
{ "decision": "BID", "fit_score": 8, "deadlines": [{"what": "...", "date": "2027-01-15", ...}],
  "requirements": [...], "reasons": ["...", "...", "..."], "facts": {...}, "score_factors": [...],
  "deal_breakers": [], "what_would_change_decision": [...], "open_questions": [...],
  "ambiguities": [...], "warnings": [], "meta": {...} }
```

## Run it locally

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt            # runtime deps + pytest + reportlab
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then paste your key into it
streamlit run app.py
```

Get a free Gemini API key at <https://aistudio.google.com/apikey> (no payment card needed).
The key is read from the `GEMINI_API_KEY` environment variable first, then from `.streamlit/secrets.toml`
(which is git-ignored). It is never in the code.

Other commands:

```bash
pytest -q                                          # 30 offline tests, no API calls
python scripts/run_samples.py docs/samples/*.pdf   # run the agent from the CLI, timed, JSON to output/
python scripts/ui_smoke.py docs/samples/*.pdf      # drive the real UI with real API calls
python scripts/make_samples.py                     # regenerate the synthetic test RFPs in docs/synthetic/
```

Optional settings (environment variables or secrets): `GEMINI_MODEL` (default `gemini-3.6-flash`),
`GEMINI_FALLBACK_MODEL` (comma-separated, default `gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.8-flash`),
`GEMINI_THINKING_LEVEL` (default `minimal`).

## Which model, and why

**Google Gemini, free tier, via the official `google-genai` SDK.** Gemini's free tier needs no card, has
a long context window (the 17-page sample is about 8k tokens, so even a 100-page RFP at roughly 60–120k
tokens fits in one prompt with no chunking or retrieval; anything over 400k estimated tokens is rejected
with a clear message), and supports **structured output**: I pass a Pydantic class as
`response_schema`, so the model must answer in exactly that JSON shape. Pydantic then validates it.

I checked the free-tier model list on ai.google.dev and then measured the candidates on the real
sample RFPs, rather than trusting memory:

| Model (free tier) | Port Alder sample, one extraction call | Verdict |
|---|---|---|
| `gemini-3.6-flash`, default thinking | 154 s, 33 requirements | thorough but far too slow |
| `gemini-3.6-flash`, `low` thinking | 15 s, 17 requirements | fast but missed items |
| **`gemini-3.6-flash`, `minimal` thinking** | **23 s, 43 requirements** (74 once split into two calls) | **chosen: best recall at a usable speed** |
| `gemini-3.5-flash`, default thinking | 40 s, 11 requirements (missed WCAG, SSO, insurance limits…) | fallback 1 |
| `gemini-3.5-flash-lite` | often 503 "high demand" during testing | fallback 2 |
| `gemini-3.8-flash` | 503 "high demand" for most of the day; rejects `minimal` thinking | fallback 3 |

Why a *list* of fallbacks: **the free tier allows only 20 requests per model per day** (I hit the limit
during testing; the API names the quota `GenerateRequestsPerDayPerProjectPerModel-FreeTier`). One triage
uses 3 requests, or 4 when a retry is needed, so each extra model adds roughly 5 more analyses per day.
When a fallback is used, the report says so in a warning.

`temperature=0` throughout. `minimal` thinking: the task is careful reading, not multi-step reasoning,
and in my tests it was several times faster with no loss of recall.

## Where it's deployed, and why

**Streamlit Community Cloud** (free, no card). It deploys straight from a GitHub repo, reads secrets from
its dashboard (so the key never touches the repo), and runs the same `streamlit run app.py` as local
development. That means there is no separate frontend or backend to build, which suits a small app whose
UI is "upload a PDF, read a report". The trade-offs: the app sleeps after inactivity (first load takes a
few seconds), and it is a single small container. Both are fine for a triage tool.

## How it works

```
PDF ─► read_pdf ───────► extract (2 Gemini calls in parallel) ─► clean_dates ─► check_evidence
       pypdf, [PAGE n]    1a facts + dates + ambiguities + gate    code: YYYY-MM-DD   code: quote on
       scanned/too-long   1b requirements only                      or "Not stated"   cited page?
                                    │
                    clearly_not_our_work? ──yes──► fast_no_bid (no LLM, score 0)
                                    │ no
                                    ▼
                        decide (Gemini call 2) ─► rules broken? ─► ask once more with the correction
                                    │
                        enforce_consistency (code) ─► TriageReport (Pydantic) ─► UI + JSON
```

- **One function per stage**, all in [triage/pipeline.py](triage/pipeline.py); no agent framework. The flow is fixed
  and short, so plain functions are easier to read, test and explain than LangChain-style abstractions.
- **Page tags.** Each page is sent as `[PAGE n]` text, so the model can cite pages and the code can check them.
- **Two parallel extraction calls.** One big call missed requirements: in one run it skipped almost all the
  functional requirements. Splitting "facts and dates" from "requirements" gives each call one job, and
  running them in parallel means the wait is only as long as the slower call.
- **Fast no-bid gate.** If call 1a says the work is clearly not ours (construction, janitorial…), the
  decision step is skipped and the report explains why. The requirements are still listed.
- **The model does not get the last word on rules.** Code forces non-exact dates to "Not stated", recomputes
  `fit_score = 5 + sum(points)`, and applies `BID ⇔ score ≥ 6 and no deal-breakers`. If the model broke a
  rule, it is asked once more with the problem spelled out, so its reasons get rewritten to match. If it
  is still wrong, code corrects it and shows a warning.
- **BID threshold = 6** (just above the neutral baseline of 5). A proposal costs a small team about a week,
  so we bid only when the evidence of fit is clearly net positive. Facts the company profile doesn't cover
  (years in business, references, insurance) are **not** scored as negatives. At most one "eligibility to
  confirm" −1 is allowed, and those facts are listed under *what would change the decision*.
- **Failure handling with clear UI messages:** scanned PDFs (almost no text), documents too long for the
  context window, invalid API key, rate limits (backoff, then the next model), daily quota exhausted
  (skip straight to the next model), and invalid JSON (validated, retried once).
- **Cache by file hash** (`st.cache_data(persist="disk")`, keyed on SHA-256 plus model): re-uploading a PDF
  costs no quota and returns instantly.

## Bonus features

### 1. Evidence check (main bonus feature)

**What it does.** Every date and requirement carries a short verbatim quote and a page number. After
extraction, plain Python (no AI) looks for each quote in the real PDF text and labels it
✅ **verified** (on the cited page), ⚠️ **wrong page** (exists elsewhere; the real page is shown), or
❌ **not found** (the model paraphrased or invented it). For dates it also checks that the parsed
`YYYY-MM-DD` agrees with the quote's own wording. The *Evidence check* tab shows "X of Y quotes verified",
with unverified items listed first.

**Why I chose it.** The brief's hardest rule is "every date matches the document exactly; no requirement
is invented". An LLM can't promise that, but code can check it. This turns "trust the AI" into "recheck
these 2 items out of 90", which is what a bids lead needs on a Monday morning.

**How it works** ([triage/evidence.py](triage/evidence.py)). Both texts are normalised (Unicode NFKC, which turns
ligatures like "ﬀ" into "ff"; lowercased; everything except `a–z0–9` removed). That makes the match immune to
PDF line breaks, hyphenation and curly quotes, while a 5–25 word quote is still far too specific to match by
accident. Then it does a substring search on the cited page (plus its neighbours, for quotes that cross a
page break), then on every page.

**It caught real errors in testing.** For example, in the Larkspur sample the model gave the "public opening"
an exact date that the text only expresses as "the same day" (flagged: date doesn't match quote). It also
caught a quote where the model wrote "efforts statement" for the PDF's "effort statement" (not found),
and a quote cited on the wrong page.

### 2. Calendar export (.ics)

**What it does.** One button downloads every *current* deadline with an exact date as an `.ics` file that
opens in Google Calendar, Outlook or Apple Calendar. Each event's description holds the time, page and
quote. **Why.** Missing the Q&A cut-off is the most common way to lose a bid you wanted. **How**
([triage/calendar_export.py](triage/calendar_export.py)). About 40 lines of hand-written RFC 5545 (no
dependency): all-day events, so no timezone is guessed; escaping and 75-octet line folding; stable UIDs
so re-importing updates rather than duplicates. **Superseded dates are never exported.** Port Alder's
addendum moved the due date from Jan 8 to Jan 15, and putting the old date in a calendar would be harmful.

## Testing and results

- **30 offline pytest tests** ([tests/](tests/)): the evidence checker (line breaks, wrong page, invented quotes,
  quotes across a page break, date-vs-quote), the ICS export (only exact and current dates, escaping, folding),
  the pipeline with the LLM mocked (fast no-bid skips the decision call, JSON shape, non-exact dates forced to
  "Not stated", score and decision correction, the retry with correction), LLM retry and fallback behaviour
  (503 backoff, daily quota skips ahead, invalid JSON retried once, bad key stops), and the real Streamlit UI via
  `AppTest` (banner, tabs, downloads, scanned-PDF error, missing-key error).
- **Real runs** on the two idiaz.io sample RFPs and two synthetic RFPs I wrote with known traps
  ([scripts/make_samples.py](scripts/make_samples.py)). I read both samples myself first and compared
  every date by hand:

| RFP | Result | Dates | Requirements | Quotes verified | Time |
|---|---|---|---|---|---|
| Sample 1: Port Alder permit automation | **BID 8/10** | 15, all correct; Jan 8 → Jan 15 addendum handled | 74 | 89/89 | 26–75 s |
| Sample 2: Larkspur janitorial | **fast NO_BID 0/10** | 12, all correct (1 flagged by the evidence check) | 61–65 | 72/73 | 15–74 s |
| Synthetic: compliance platform | BID 8/10 | conflicting due dates flagged as an ambiguity and an open question; award date with no day = Not stated | 25 (incl. FedRAMP buried in Appendix C) | 35/35 | 21 s |
| Synthetic: fire-station renovation | fast NO_BID 0/10 | the "HVAC controls" line didn't fool the gate | 16 | 20/20 | 8 s |

Times vary a lot on the free tier: the same file took 26 s on one run and 75 s on another, depending on
Google's load and on which fallback model was used. Most runs were under a minute.

## Limitations

- **Free-tier quota:** 20 requests per model per day, and about 3 requests per new PDF. The fallback chain
  and the cache help, but heavy use in one day will hit the limit (the UI says so clearly).
- **Text-based PDFs only.** Scanned PDFs are detected and rejected with a message; there is no OCR.
- **Run-to-run variation.** Even at `temperature=0` the number of requirements varies (for example 61–65
  on the same file), mostly in how finely the model splits items. The evidence check makes this visible
  but cannot detect a requirement the model *skipped*; it only checks the ones it returned.
- **The company profile is four lines.** Anything it doesn't say (years in business, references, insurance)
  is treated as "to confirm", not scored. A richer profile would give sharper scores.
- **The date-vs-quote check is a heuristic** (year, month and day numbers must appear in the quote). For
  example, a day of "2" could falsely match "2:00 PM".
- **Tables in PDFs** are extracted as plain text by pypdf. Most RFP tables survive, but complex layouts
  may lose column alignment.

## Project layout

```
app.py                     Streamlit UI
triage/pdf_reader.py       PDF → per-page text, [PAGE n] tags, scanned/too-long checks
triage/prompts.py          company profile, scoring rules, all prompts
triage/schemas.py          Pydantic shapes for both LLM calls and the final report
triage/llm.py              the only code that calls Gemini: retries, fallbacks, validation
triage/pipeline.py         the agent: one function per stage
triage/evidence.py         bonus 1: quote verification (no AI)
triage/calendar_export.py  bonus 2: .ics export
tests/                     30 offline tests
scripts/                   CLI runner, live UI smoke test, synthetic RFP generator
docs/samples/              the two idiaz.io sample RFPs; docs/synthetic/ my two test RFPs
```

## AI assistance

I built this with an AI coding assistant (Claude Code), as the brief allows. I set the requirements and
the architecture constraints, reviewed every change, read the sample RFPs myself to build an answer key,
and made the design calls from measured results: the model choice, splitting the extraction into two
calls, the scoring rule, and excluding superseded dates from the calendar. [EXPLAIN.md](EXPLAIN.md) walks
through every file and decision.
