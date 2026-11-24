"""Monday queue: run several RFPs one at a time and rank them with plain code (no LLM).

Named ranking.py, not queue.py: a local queue.py would shadow Python's standard `queue` module.
Everything takes `today` as a parameter, so tests are deterministic.
"""

import csv
import hashlib
import io
import time
from datetime import date

from .llm import LLMError
from .pdf_reader import PdfError

NOT_STATED = "Not stated"
MAX_FILES = 5
LIVE_BID, LIVE_NO_BID, NOT_LIVE = 0, 1, 2  # ranking groups, best first


def _exact(value: str):
    """Return a date for an exact YYYY-MM-DD string, else None."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def proposal_due(deadlines: list[dict]) -> tuple[str, bool]:
    """Pick the proposal deadline by its `kind` field. Returns (date or "Not stated", several_found).
    Superseded dates are ignored if a current one exists; if several remain, take the latest."""
    candidates = [d for d in deadlines if d.get("kind") == "proposal_due"]
    effective = [d for d in candidates if not d.get("superseded")] or candidates
    exact = sorted({d["date"] for d in effective if _exact(d["date"])})
    if not exact:
        return NOT_STATED, False
    return exact[-1], len(exact) > 1  # more than one distinct date: flag it for a human


def next_must_attend(deadlines: list[dict], today: date) -> str:
    """Earliest upcoming event the RFP says bidders must attend (e.g. a site walk). Deadlines
    (proposal, questions) and the start date are never "events", even if the model flags them."""
    upcoming = [d for d in deadlines
                if d.get("mandatory") and not d.get("superseded") and d.get("kind") in ("site_visit", "other")
                and _exact(d["date"]) and _exact(d["date"]) >= today]
    if not upcoming:
        return "None found"
    first = min(upcoming, key=lambda d: d["date"])
    return f"{first['what']} ({first['date']})"


def build_row(report: dict | None, filename: str, today: date, error: str = "") -> dict:
    """One table row. `report` is None when the file failed; `error` then says why.
    Keys starting with "_" are for sorting only and are not shown or exported."""
    if report is None:
        return {"File": filename, "Agency": "-", "Decision": "Failed", "Fit score": None,
                "Proposal due": NOT_STATED, "Days left": None, "Status": f"Failed: {error}",
                "Next must-attend event": "-", "Biggest deal-breaker": "-", "_group": NOT_LIVE, "_due": None}

    due, several = proposal_due(report["deadlines"])
    due_date = _exact(due)
    days_left = (due_date - today).days if due_date else None
    expired = days_left is not None and days_left < 0
    if expired:
        group = NOT_LIVE  # a missed deadline ranks last, whatever the score
    else:
        group = LIVE_BID if report["decision"] == "BID" else LIVE_NO_BID
    return {
        "File": filename,
        "Agency": report["facts"]["agency"],
        "Decision": report["decision"],
        "Fit score": report["fit_score"],
        "Proposal due": due + (" ⚠️ several dates found" if several else ""),
        "Days left": days_left,
        "Status": "Expired" if expired else "Live",
        "Next must-attend event": next_must_attend(report["deadlines"], today),
        "Biggest deal-breaker": report["deal_breakers"][0] if report["deal_breakers"] else "None found",
        "_group": group,
        "_due": due_date,
    }


def sort_rows(rows: list[dict]) -> list[dict]:
    """Live BIDs, then live NO_BIDs, then Expired and Failed. Inside a group: higher fit score,
    then earlier proposal due date ("Not stated" last), then filename. Adds a Rank column."""
    def key(row):
        score = row["Fit score"] if row["Fit score"] is not None else -1
        due = row["_due"] or date.max
        return (row["_group"], -score, due, row["File"])

    ranked = sorted(rows, key=key)
    return [{"Rank": i, **row} for i, row in enumerate(ranked, start=1)]


def recommendation_line(rows: list[dict]) -> str:
    """One plain-code sentence for the top of the table. `rows` must already be sorted."""
    best = next((r for r in rows if r["_group"] == LIVE_BID), None)
    if best is None:
        return "No BIDs in this batch."
    days = best["Days left"]
    when = f"proposal due in {days} days" if days is not None else "proposal due date not stated"
    return f"Start with: {best['Agency']} ({best['File']}): BID, fit {best['Fit score']}/10, {when}."


def _safe_cell(value):
    # CSV injection guard: spreadsheet apps run cells starting with = + - @ as formulas, and RFP text
    # is untrusted. A leading single quote makes them plain text. Numbers are left as numbers.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return "" if value is None else value


def to_csv(rows: list[dict]) -> bytes:
    columns = [c for c in rows[0] if not c.startswith("_")] if rows else []
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_safe_cell(row[c]) for c in columns])
    return buffer.getvalue().encode("utf-8-sig")  # UTF-8 with a BOM, so Excel shows accents correctly


def run_queue(files: list[tuple[str, bytes]], triage_fn, today: date, progress=lambda message, fraction: None,
              pause_seconds: float = 2.0, sleep=time.sleep) -> dict:
    """Triage each (filename, pdf_bytes) ONE AT A TIME (free-tier rate limits), never stopping on a
    failure. Identical files (same SHA-256) are processed once. Returns rows, reports and skipped names."""
    if len(files) > MAX_FILES:
        raise ValueError(f"At most {MAX_FILES} files per queue.")
    rows, reports, skipped, seen = [], {}, [], {}
    for i, (name, data) in enumerate(files, start=1):
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            skipped.append(f"{name} (same file as {seen[digest]})")
            continue
        # Two different PDFs can share a name (e.g. two downloads called RFP.pdf). Names are the keys
        # for the reports, so make each one unique: "RFP.pdf", "RFP.pdf (2)", ...
        base, n = name, 2
        while name in seen.values():
            name, n = f"{base} ({n})", n + 1
        seen[digest] = name
        if len(seen) > 1 and pause_seconds:
            sleep(pause_seconds)  # small gap between files, kinder to the per-minute limit
        progress(f"Reading {i} of {len(files)}: {name}", (i - 1) / len(files))
        try:
            report = triage_fn(data)
        except (PdfError, LLMError) as exc:
            rows.append(build_row(None, name, today, str(exc)))
            continue
        except Exception as exc:  # one bad file must never kill the queue
            rows.append(build_row(None, name, today, f"Unexpected error: {exc}"))
            continue
        reports[name] = report
        rows.append(build_row(report, name, today))
    return {"rows": sort_rows(rows), "reports": reports, "skipped": skipped}
