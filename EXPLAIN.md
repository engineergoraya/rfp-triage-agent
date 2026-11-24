# EXPLAIN: how the RFP Triage Agent works, file by file

This is a plain-English walkthrough so anyone (including me in the demo call) can explain every part.
Read it top to bottom once and you will know the whole codebase. It is about 1,300 lines of app code.

---

## 1. The big picture in 30 seconds

1. The user uploads a PDF in the Streamlit page (`app.py`).
2. `pypdf` pulls out the text of each page, and each page is labelled `[PAGE 1]`, `[PAGE 2]`, and so on.
3. Two Gemini calls run **at the same time**:
   - **1a** returns facts, dates, ambiguities, and "is this clearly not our kind of work?"
   - **1b** returns every requirement.
   Each date and requirement comes with a short verbatim quote and its page number.
4. **Plain code** checks every quote against the real PDF text (the evidence check), and forces any date
   that isn't a complete calendar date to "Not stated".
5. If the work is clearly not ours (janitorial, construction), we stop: **fast NO_BID**, no third call.
6. Otherwise a third Gemini call scores the fit and makes the bid decision. Code re-checks the score
   arithmetic and the BID rule. If the model broke them, it is asked once more with the mistake spelled out.
7. Everything is packed into one validated `TriageReport`, shown in the UI and offered as JSON, `.ics`
   and Word (`.docx`).
8. **Monday queue mode** runs steps 1–7 for 2–5 PDFs, one after another, then plain code ranks them.

---

## 2. File by file, function by function

### `app.py`: the web page (Streamlit)

Streamlit reruns this script from top to bottom every time the user interacts. No HTML or JavaScript is needed.

- **`get_api_key()`**: reads `GEMINI_API_KEY` from the environment (local development) or from `st.secrets`
  (Streamlit Cloud's secret store, or the git-ignored `.streamlit/secrets.toml`). Returns `""` if
  neither exists. The page then shows an error and stops.
- **`cached_triage(file_hash, model, version, _pdf_bytes, _api_key)`**: wraps `run_triage` in
  `@st.cache_data`. Streamlit builds the cache key from the arguments, but **arguments starting with `_`
  are not hashed**, so the key is only *(SHA-256 of the file, model name, `REPORT_VERSION`)*. The version
  is bumped whenever the report's shape changes (version 2 added the date `kind`), so an old cached report
  missing the new fields is never reused. **Nothing inside it may draw on the page** (`st.*`): Streamlit
  records such calls and replays them on a cache hit, and if they drew on an element created outside the
  function (like the queue's status line) the replay crashes. I hit exactly that bug: the second ranking
  of the same files failed. Version 3 throws away the entries saved while that bug existed. The same PDF uploaded again returns instantly and
  costs no API quota. `persist="disk"` keeps the cache across app restarts.
- **`render_report(report, key)`**: draws the report. `key` makes the widget IDs unique (`single`, or
  `queue-1`, `queue-2`…), because Streamlit refuses two download buttons with the same ID on one page. It shows a green (BID) or red (NO BID) banner, then three
  metrics (decision, score, quotes verified), then any warnings, then three tabs:
  - *Report*: the five required sections, in the brief's order, plus the `.ics` button.
  - *Evidence check*: "X of Y quotes verified" and a table sorted so the riskiest items come first
    (not found → wrong page → verified; a date whose value doesn't match its quote also jumps to the top).
  - *JSON*: a download button and the raw JSON.
  Above the tabs: a *Download report (Word .docx)* button.
- **`render_single(api_key)`**: the original single-RFP flow, unchanged: uploader, spinner, `try/except`.
  Our own errors (`PdfError`, `LLMError`) are shown as friendly `st.error` messages. Anything unexpected
  is still shown as an error rather than a blank page. It `return`s (never `st.stop()`s) when no file is
  uploaded, so it can't block the other view.
- **`file_signature(files)`**: the sorted content hashes of the uploaded files. Saved with a ranking, so
  if the user adds or removes files afterwards the page says "the uploaded files changed".
- **`render_queue(api_key)`**: the Monday queue view. A multi-file uploader (more than 5 → error, no
  button), a caption with the maximum number of model calls, and a *Rank these RFPs* button. The button
  calls `ranking.run_queue` with `triage_one` (the same cached triage as single mode) and stores the
  result in `st.session_state["queue"]`. Everything below the button reads from session state, so later
  reruns (another report picked, the CSV downloaded) never call the model. It shows the recommendation
  line, the table, the CSV button, *Clear results*, and a **Full report** section: a select box
  (default: rank 1) and `render_report` for the chosen RFP, so every tab and download from single mode
  is there. Options are file names (strings), so a new batch can't leave the picker pointing at nothing.
  - **`show(message, fraction)`** (inside it): `run_queue` calls it before each file to update the status
    line ("Reading 2 of 3: file.pdf", plus a note that rate-limit waits can make a file slower) and the
    progress bar. It runs outside the cached function, so it is safe to draw.
- **Bottom of the file (the page itself)**: title, key check, then a **radio button** (not `st.tabs`)
  choosing the view. With a radio only the chosen view runs, and the choice survives reruns.

### `triage/pdf_reader.py`: PDF to text

- **`extract_pages(pdf_bytes)`**: uses `pypdf` to get one string per page. If the average is under 50
  characters per page, the PDF is almost certainly a **scanned image** with no text layer, so it raises
  `PdfError` with a clear message. Broken or encrypted files also become a `PdfError`.
- **`tag_pages(pages)`**: joins the pages with `[PAGE n]` markers. This is what makes page citations possible.
- **`estimate_tokens(text)`**: about 4 characters per token, which is good enough for a size guard.
- **`check_length(text)`**: rejects documents over 400k estimated tokens with a clear message. A real
  100-page RFP is around 60–120k, so this only stops absurd inputs that would blow the context window or
  the free per-minute token quota.

### `triage/schemas.py`: the data shapes (Pydantic)

Pydantic classes do two jobs here. They are passed to Gemini as **`response_schema`**, which forces the
model to answer in exactly that JSON shape. They also **validate** the answer, so a malformed answer is
caught and retried. They deliberately have no defaults and no numeric limits, because Gemini supports
only part of JSON Schema. Rules like "exactly 3 reasons" are checked in plain code instead.

- `Facts`: agency, solicitation number, title, summary, scope, contract type and value, period and place
  of performance, how to submit.
- `DateItem`: what, date (`YYYY-MM-DD` or "Not stated"), time, `as_written`, quote, page, note, `superseded`.
- `Requirement`: category (a fixed list: skill, certification, set_aside…), plain-English requirement,
  mandatory yes/no, quote, page.
- `Ambiguity`: issue and page.
- `FactsAndDates` (answer of call 1a) and `RequirementList` (answer of call 1b). `Extraction` merges the two.
- `ScoreFactor` (factor, points, one-line reason) and `Decision` (answer of call 2).
- `Evidence`, `CheckedDate`, `CheckedRequirement` (the items plus their evidence label) and `TriageReport`,
  the final report. Its first five fields are exactly the output shape the brief asks for.

### `triage/prompts.py`: everything we say to the model

- `COMPANY_PROFILE`: the idiaz.io profile from the brief, plus an explicit list of things it does **not**
  tell us (years in business, references, insurance…), so the model treats them as unknown and doesn't guess.
- `BASELINE_SCORE = 5`, `BID_THRESHOLD = 6`: the scoring rule, shared by the prompt and the code checks.
- `SYSTEM_INSTRUCTION`: the brief's ground rules (document only, "Not stated", flag ambiguity, exact quotes).
- `FACTS_PROMPT` (call 1a): the facts, a checklist of every kind of date, the strict date rule, how to handle
  changed dates (`superseded`), an ambiguity checklist, and the "clearly not our work" gate with examples.
- `REQUIREMENTS_PROMPT` (call 1b): asks for both what the **bidder** must be or hold and what the **work**
  must do (each numbered or [M]/[D] row is its own item), with a definition for every category.
- `DECIDE_PROMPT` (call 2): the scoring rules (what earns + and − points, no penalties for unknowns), the
  decision rule and why, and what to return.
- `CORRECTION_NOTE`: added to the decide prompt on the one retry, describing what the model got wrong.

### `triage/llm.py`: the only code that calls Gemini

- **`models_to_try()`**: `GEMINI_MODEL` (default `gemini-3.6-flash`), then the comma-separated
  `GEMINI_FALLBACK_MODEL` list, with duplicates removed.
- **`_call(...)`**: one API request with `temperature=0`, JSON mode, the Pydantic schema, and `minimal`
  thinking (or the model's default if `set_thinking=False`).
- **`_is_too_long(exc)`, `_is_daily_quota(exc)`**: classify API errors.
- **`generate_json(api_key, prompt, schema, system)`**: tries each model in turn:
  - **success** → validate with Pydantic and return `(object, model_name)`;
  - **invalid JSON** → retry once on the same model, then move to the next model;
  - **400 "thinking level not supported"** → retry the same model with its default thinking;
  - **401/403/bad key** or **too long** → stop immediately with a clear message (another model won't help);
  - **429 per-day quota** → skip straight to the next model (waiting can't help until midnight Pacific);
  - **503 busy / per-minute 429** → wait 2 s, then 5 s, then move to the next model;
  - **404 or any other error** → note it and move to the next model.
  If every model fails, it raises `LLMError` listing what happened to each model, which the UI shows.

### `triage/pipeline.py`: the agent itself (one function per stage)

- **`read_pdf`**: `extract_pages` + `tag_pages` + `check_length`.
- **`extract`**: runs calls 1a and 1b in a `ThreadPoolExecutor` with 2 workers, so they overlap. The API
  calls are network-bound, so threads are fine here (Python's GIL isn't a problem while waiting on the
  network). It merges the results into one `Extraction`.
- **`clean_dates`**: code enforces the date rule. Anything that isn't a real `YYYY-MM-DD` (including
  impossible dates like `2026-02-30`) becomes "Not stated", with a note and a warning.
- **`check_evidence`**: attaches `evidence.check_quote(...)` to every date and requirement, plus
  `date_matches_quote` for dates. Mismatches become warnings.
- **`fast_no_bid`**: builds a `Decision` without any LLM call: score 0, NO_BID, the model's own reasons
  (topped up to 3 from a fixed list if needed), and what would change the decision.
- **`computed_score`, `expected_decision`, `find_inconsistency`**: the scoring rule written as code:
  `score = clamp(5 + sum(points), 0, 10)`, `BID ⇔ score ≥ 6 and no deal-breakers`, exactly 3 reasons.
- **`decide`**: call 2. If `find_inconsistency` finds a problem, it asks **once more** with
  `CORRECTION_NOTE`, so the reasons are rewritten to fit the corrected decision rather than just relabelled.
- **`enforce_consistency`**: the final safety net. If the model is still wrong after the retry, code fixes
  the score and decision and adds a visible warning ("read the reasons with care").
- **`run_triage(pdf_bytes, api_key, progress)`**: runs all of the above, times each stage
  (`meta.stage_seconds`), adds a warning if a fallback model was used, builds the `TriageReport`, and
  returns it as a plain dict. That dict is exactly what the UI shows and the JSON download contains.

### `triage/evidence.py`: bonus feature 1 (no AI)

- **`normalize(text)`**: NFKC Unicode normalisation (turns "ﬀ" ligatures into "ff"), lowercase, then drop
  everything that is not `a–z` or `0–9`. "Regis-\ntered in “SAM.gov”" becomes `registeredinsamgov`. This
  makes the match ignore PDF line breaks, hyphenation, double spaces and quote styles.
- **`_quote_in(quote, page_text)`**: substring search after normalising. If the model used "...", every
  fragment must appear in order.
- **`check_quote(quote, cited_page, pages)`**: checks the cited page (and the cited page joined to its
  neighbours, for quotes that cross a page break) → `verified`. Otherwise it checks every page →
  `wrong_page` (and records where). Otherwise → `not_found`.
- **`date_matches_quote(iso_date, quote)`**: the year, the month (name, abbreviation or number) and the
  day must all appear in the quote. This catches "right quote, wrong date".
- **`summarize(items)`**: returns (verified, total).

### `triage/calendar_export.py`: bonus feature 2

- **`_escape`**: RFC 5545 escaping of `\ ; ,` and newlines.
- **`_fold`**: RFC 5545 says lines over 75 bytes must be folded (CRLF plus a space).
- **`build_ics(deadlines, title)`**: one all-day `VEVENT` per exportable deadline. The UID is a hash of
  title + what + date, so importing again updates events instead of duplicating them.
- **`is_exportable`**: an exact date **and** not superseded. **`count_exportable`**: used for the button label.

### `triage/ranking.py`: bonus feature 2, the Monday queue (no AI)

Not called `queue.py` on purpose: a local `queue.py` would shadow Python's standard `queue` module,
which libraries we use import. Every function takes `today` as a parameter, so tests can fix the date.

- **`_exact(value)`**: a `date` for a real `YYYY-MM-DD`, else `None`.
- **`proposal_due(deadlines)`**: picks dates with `kind == "proposal_due"`, drops superseded ones (unless
  that leaves nothing), and returns the latest exact date plus a flag if more than one distinct date was
  left. Port Alder: Jan 8 is superseded, so Jan 15 wins with no flag.
- **`next_must_attend(deadlines, today)`**: the earliest upcoming date that is `mandatory`, not
  superseded, and an *event* (`site_visit` or `other`). Deadlines are excluded even if the model flagged
  them, because the fallback model over-uses the flag.
- **`build_row(report, filename, today, error)`**: one table row. `report=None` makes a "Failed" row
  with the reason. Days left = due date − today; below zero → status "Expired", group "not live". Keys
  starting with `_` (`_group`, `_due`) are for sorting only and are never shown or exported.
- **`sort_rows(rows)`**: sorts by (group, −score, due date with "Not stated" as `date.max`, filename), then
  numbers the rows. Groups: 0 live BID, 1 live NO_BID, 2 Expired/Failed.
- **`recommendation_line(rows)`**: the first live BID in the sorted rows becomes "Start with: …", or
  "No BIDs in this batch."
- **`_safe_cell` / `to_csv(rows)`**: the CSV-injection guard (prefix `'` before `= + - @`, tab or CR; only
  on text, so `-4` days left stays a number), written with the `csv` module as UTF-8 with a BOM so Excel
  reads accents correctly.
- **`run_queue(files, triage_fn, today, progress, pause_seconds, sleep)`**: loops over the files **one at
  a time**: skip a duplicate SHA-256, give a repeated *name* (different content) a suffix like
  `RFP.pdf (2)` because names key the reports, pause between files, call `triage_fn`, and turn any exception into a
  Failed row instead of stopping. `triage_fn` and `sleep` are parameters, so tests pass fakes and no real
  model call or real wait ever happens in tests.

### `triage/report_docx.py`: bonus feature 4, the Word report

- **`_para(text, size, bold)`**: one Word paragraph in XML. The text is cleaned of control characters (XML
  forbids them; PDF text often has form feeds) and escaped (`&`, `<`, `>`).
- **`build_docx(report)`**: builds the paragraphs for every report section and zips three files:
  `[Content_Types].xml`, `_rels/.rels`, `word/document.xml`. That is the minimum Word needs. No
  dependency. I checked the output opens with macOS `textutil`.

### Rate limits in `triage/llm.py` (added for the queue)

`RATE_LIMIT_BACKOFF_SECONDS = [15, 30, 60]`. A 429 that names a per-*day* quota still skips to the next
model (waiting can't help). Any other 429 / `RESOURCE_EXHAUSTED` is the per-minute limit: call
`on_wait(seconds)` (the command-line scripts print "Waiting for the free-tier rate limit…"; the web
page only says up front that waits can happen, because drawing from inside the cached triage
breaks Streamlit's cache replay), sleep, retry the same model;
after the third wait, raise `LLMError`. That is the only place rate-limit retries happen, so they never
multiply with the model fallback loop. 401/403 and "API key" errors are never retried.

### Date fields added for the queue (`schemas.py`, `prompts.py`)

`DateItem` gained `kind` (`proposal_due`, `questions_due`, `site_visit`, `start_date`, `other`) and
`mandatory` (bidders must attend). The brief's JSON shape is unchanged; these are extra keys.

### `tests/`

- `test_logic.py`: the evidence check, ICS, date cleaning, the fast no-bid gate (the decide call is never
  made), the JSON shape, score and decision correction, the decide retry, and the scanned-PDF error. The
  LLM and the PDF reader are replaced with fakes (`unittest.mock.patch`).
- `test_llm.py`: retry and fallback behaviour with a fake `_call` (503 → retry; daily quota → next model;
  bad JSON → one retry; bad key → stop; thinking level rejected → retry with the default).
- `test_queue.py`: the Monday queue, rate limits and Word export (see section 5).
- `test_ui.py`: runs the real `app.py` with Streamlit's `AppTest`, uploads a file, and checks the banner,
  tabs, download buttons, scanned-PDF error and missing-key error. Two queue tests switch to the queue
  view, upload two files, click *Rank*, rerun, and check that the model was called exactly twice. `sample_report.json` is a real report
  saved from a live run.

### `scripts/`

- `run_samples.py`: runs the agent on PDFs from the command line, prints the decision, counts, evidence
  and time, and saves JSON to `output/`.
- `ui_smoke.py`: uploads real PDFs through the real UI with real API calls, twice each (the second upload
  proves the cache works).
- `eval_queue.py`: ranks real PDFs through `run_queue` with the real pipeline (no cached or hard-coded
  answers), prints the table, saves reports to `eval/latest/`, and diffs them against `eval/baseline/`
  (decision, fit score within 1 point, the set of exact dates). `--diff DIR` only does the diff.
  `eval/` is git-ignored because its files are derived from the sample RFPs.
- `make_samples.py`: generates the two synthetic RFPs with deliberate traps (conflicting due dates, an
  award date with no day, a FedRAMP requirement buried in Appendix C, and a construction job with a small
  "HVAC controls" line).

---

## 3. Main design decisions and trade-offs

| Decision | Why | Trade-off |
|---|---|---|
| **Whole document in one prompt** (no chunking or RAG) | 100 pages fits easily in Gemini's context; retrieval could miss a requirement buried in an appendix | Costs more tokens per call; we guard very long inputs |
| **Plain functions, no LangChain** | The flow is fixed and short; anyone can read it top to bottom | No built-in tracing or tool use, which we don't need |
| **Structured output + Pydantic** | The model *must* return valid JSON in our shape, and we validate it | Gemini supports only part of JSON Schema, so some rules live in code |
| **Two parallel extraction calls** | One call skipped requirements; two focused calls had much better recall at the same wall-clock time | One more request against the 20/day quota |
| **Code checks the LLM** (dates, score maths, BID rule, quotes) | "Trust but verify": rules that can be checked by code should be | More code, but each check is a few lines |
| **Retry the decision on a rule break** instead of just overriding it | Overriding left "BID" reasons under a "NO BID" banner in testing | One extra call, only when needed |
| **Unknown company facts are not penalised** | The profile is four lines; penalising every unknown made a near-perfect fit a NO_BID | They are listed as "what would change the decision" instead |
| **`gemini-3.6-flash`, minimal thinking** | Best recall at a usable speed in measured tests | Slower than 3.5-flash on some runs |
| **Fallback chain of 3 models** | 20 requests per model per day; each model has its own quota | Fallbacks can be less thorough, so the UI warns |
| **Streamlit Community Cloud** | Free, deploys from GitHub, built-in secrets, same code as local | Sleeps when idle; single small container |
| **All-day calendar events** | Never guess a timezone | The time is in the description, not the event |
| **Queue runs files one at a time** | Free-tier per-minute limits; parallel files would multiply 429s | A batch of 5 new files can take several minutes |
| **Ranking in code, not by the LLM** | Same input → same order, and every step can be explained | Can't weigh soft factors the score doesn't capture |
| **Deadline chosen by a `kind` field** | String-matching "Proposal due" in free text is fragile | One more field the model must fill in |
| **Word export written by hand** | No new dependency; three XML files | Plain formatting (no tables or styles) |

---

## 4. Fifteen likely interview questions (with short answers)

1. **Walk me through what happens when I upload a PDF.**
   pypdf → page-tagged text → two parallel Gemini calls (facts and dates; requirements) → code checks
   dates and quotes → fast no-bid gate → decision call → code checks the score and rule → report, JSON, ICS.

2. **How do you stop the model from inventing things?**
   Four layers: a prompt that says "document only, 'Not stated' if missing"; a verbatim quote and page
   required for every date and requirement; code that verifies every quote against the PDF; and code that
   rejects dates that aren't complete. Anything unverified is shown first in the Evidence tab.

3. **Why Gemini and not another model?**
   It has a free tier with no card, a long context so the whole RFP fits in one prompt, and native
   structured JSON output. I compared four free models on the real samples, and 3.6-flash with minimal
   thinking had the best recall at a usable speed.

4. **Why not LangChain or an agent framework?**
   The steps are fixed and few. Plain functions are easier to test, time and explain. A framework would
   add dependencies and indirection without adding capability here.

5. **How is the fit score calculated? Isn't it just the model's opinion?**
   The model lists factors with points and reasons. Code recomputes `5 + sum(points)` clamped to 0–10, and
   applies BID ⇔ score ≥ 6 with no deal-breakers. The score is explainable line by line, and the arithmetic
   can't be wrong in the final report.

6. **Why is the threshold 6?**
   5 is neutral. A proposal costs a small team about a week, so we only bid when the evidence of fit is net
   positive. It's a single constant in `prompts.py` if the bids lead wants it stricter.

7. **What happens with Port Alder's changed due date?**
   The PDF text contains both "January 8, 2027" (struck through) and "January 15, 2027", plus Addendum No. 1.
   The agent lists both, marks Jan 8 as `superseded` with a note, and the calendar export skips it.

8. **What does the evidence check actually prove, and what can't it catch?**
   It proves each returned quote really is on the page cited (or tells you it's on a different page, or
   missing). It can't catch a requirement the model *skipped*. That's why requirements have their own
   dedicated call, and why I compared against my own reading of the samples.

9. **Why normalise down to letters and digits only? Couldn't that cause false matches?**
   PDF extraction adds line breaks, hyphens, ligatures and odd spaces that the model never reproduces.
   A 5–25 word quote reduced to letters and digits is still a long, specific string. A random match is
   practically impossible, and the tests cover both true and false cases.

10. **How do you handle rate limits and failures?**
    503 or per-minute 429: back off 2 s and 5 s, then the next model. Daily-quota 429: skip straight to the
    next model. Invalid JSON: one retry. Bad key or document too long: stop with a clear message.
    Scanned PDF: detected before any API call. Every failure reaches the UI as a readable message.

11. **What's the biggest limitation?**
    The free tier: 20 requests per model per day, and each new PDF uses 3. The cache and the 3-model
    fallback chain help, but heavy use will hit the cap. Second: the 4-line company profile, which forces
    "to confirm" answers on eligibility.

12. **How did you test accuracy?**
    I read both sample RFPs first and wrote down the right dates and decision. Then I ran the agent and
    compared every date by hand, wrote synthetic RFPs with known traps, and wrote offline tests (56 now) for all
    the non-LLM logic and the UI.

13. **Why does the janitorial RFP still list requirements if it's a fast no-bid?**
    The requirements call runs in parallel with the facts call, so it costs no extra time. The gate only
    skips the decision call. A bids lead might still want to see the requirements, for example to pass
    the RFP to a partner.

14. **How does caching work, and why does it matter?**
    `st.cache_data` keyed on the file's SHA-256 plus the model name. Re-uploading the same file is instant
    and free. With a 20-requests-per-day quota, that's the difference between reviewers seeing a report
    and seeing a quota error.

15. **If you had another week, what would you add?**
    A richer company profile (past projects, certifications, headcount) so eligibility can be scored
    rather than just flagged; OCR for scanned
    PDFs; and a small regression set of RFPs with hand-labelled answers to measure recall automatically
    whenever the prompts change.

---

## 5. Monday queue and Word export: likely questions

1. **Why sequential and not parallel?**
   The free tier limits requests per minute per model. Each RFP already makes 2 parallel calls; five
   files in parallel would be 10 at once and trigger 429s. One at a time, with a 2 s pause, is slower but
   predictable, and cached files are instant anyway.

2. **Why is the ranking done by code and not the LLM?**
   It must be repeatable and explainable: same reports in, same order out, and I can say exactly why A
   beats B (group, then score, then deadline, then name). The LLM already did the hard part, scoring
   each RFP; ordering numbers is a job for `sorted()`.

3. **How do you handle rate limits?**
   Per-day quota: skip to the next model, since waiting won't help. Per-minute (429 / RESOURCE_EXHAUSTED):
   wait 15, 30, 60 s on the same model, then fail that file with a clear message. The waits live in one
   place in `llm.py`, so they never multiply with the fallback loop. Bad keys are never retried.

4. **What happens when one file fails?**
   `run_queue` catches the error, adds a "Failed" row with the reason (e.g. "This PDF looks scanned"),
   and carries on. Failed rows rank last. A test feeds a scanned file, a crash and a good file and checks
   that the good one still ranks first.

5. **Why is "days left" approximate?**
   Deadlines are in the agency's time zone (often with a time like "3:00 PM Pacific"), while the count is
   just the date minus today's date on the server. It can be off by a day near midnight. The caption says so.

6. **What is CSV injection and how did you stop it?**
   Excel and Sheets treat a cell starting with `=`, `+`, `-` or `@` as a formula, and RFP text is untrusted,
   so an agency name like `=HYPERLINK(...)` could run when the CSV is opened. `_safe_cell` adds a leading
   `'`, which makes the spreadsheet show it as text. Numbers (like −4 days) are left alone.

7. **How did you avoid breaking the single-RFP flow?**
   The single view's code moved into `render_single` unchanged; the pipeline only gained an optional
   `on_wait` callback and two extra date fields. I saved both samples' reports as a baseline before
   changing anything and diffed after: same decisions (Port Alder BID, Larkspur fast NO_BID), scores
   within a point, and the same proposal, questions, site-visit and start dates. The only differences: the
   new run also lists Addendum No. 1's issue date (correct, and missing before), and Larkspur's
   contract-end date (2029-01-31) now appears only in the "period of performance" fact rather than as a
   separate date row. All 30 old tests still pass.

8. **How do you pick the proposal deadline when there are several?**
   By the `kind` field, never by text. Superseded dates are ignored; if more than one current date is
   left, the latest is used and the row gets a ⚠️ so a human checks it.

9. **Why does `st.session_state` matter here?**
   Streamlit reruns the whole script on every click. Without session state, picking another report or
   downloading the CSV would lose the results, or worse, call the model again.

10. **How is the Word file made without a library?**
    A `.docx` is a zip. Three XML files (content types, a relationships file, the document) are the minimum
    Word, Google Docs and LibreOffice accept. Text is escaped and stripped of control characters.

11. **What was the hardest bug?**
    Ranking the same files twice crashed. Streamlit's `st.cache_data` records any `st.*` call made inside
    a cached function and replays it on a cache hit. My progress callback drew on the queue's status line,
    which was created outside the function, so the replay had nothing to draw on. The fix: never draw
    inside the cached function. A test now ranks, clears and ranks again.
