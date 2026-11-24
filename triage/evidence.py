"""Bonus feature 1: the evidence check.

LLMs can misquote or invent. So after extraction, plain Python (no LLM) looks for every quote
the model gave, on the page it cited, in the real PDF text. Each date and requirement is labelled:

  verified   - the quote is on the cited page
  wrong_page - the quote exists, but on a different page (we record which)
  not_found  - the quote is nowhere in the document: a human must check this item

For dates it also checks that the parsed YYYY-MM-DD agrees with the quote's own wording,
which catches "right quote, wrong date" mistakes.
"""

import re
import unicodedata

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


def normalize(text: str) -> str:
    """Lowercase and keep only letters and digits.

    PDF text extraction adds line breaks, hyphenation, odd spaces and curly quotes that the
    model does not reproduce. Dropping everything except [a-z0-9] makes the comparison immune
    to all of that, while a 5-25 word quote is still far too specific to match by accident.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^a-z0-9]", "", text)


def _quote_in(quote: str, page_text: str) -> bool:
    """True if every fragment of the quote appears, in order, in the page text.

    Fragments exist only if the model used an ellipsis despite being told not to.
    """
    haystack = normalize(page_text)
    fragments = [normalize(f) for f in re.split(r"\.\.\.|…", quote)]
    fragments = [f for f in fragments if f]
    if not fragments:
        return False
    position = 0
    for fragment in fragments:
        position = haystack.find(fragment, position)
        if position == -1:
            return False
        position += len(fragment)
    return True


def check_quote(quote: str, cited_page: int, pages: list[str]) -> dict:
    """Return {"status": ..., "found_on_page": ...} for one quote. Pages are 1-indexed."""
    n = len(pages)
    if 1 <= cited_page <= n:
        # A quote can run across a page break, so also try the cited page joined to its neighbours.
        candidates = [pages[cited_page - 1]]
        if cited_page < n:
            candidates.append(pages[cited_page - 1] + " " + pages[cited_page])
        if cited_page > 1:
            candidates.append(pages[cited_page - 2] + " " + pages[cited_page - 1])
        if any(_quote_in(quote, text) for text in candidates):
            return {"status": "verified", "found_on_page": cited_page}

    for page_number, text in enumerate(pages, start=1):
        if _quote_in(quote, text):
            return {"status": "wrong_page", "found_on_page": page_number}
    return {"status": "not_found", "found_on_page": None}


def date_matches_quote(iso_date: str, quote: str):
    """Does the quote contain this date's year, month and day? None if the date is not exact."""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso_date.strip())
    if not match:
        return None
    year, month, day = (int(x) for x in match.groups())
    q = quote.lower()

    has_year = str(year) in q or re.search(rf"/{year % 100:02d}\b", q) is not None
    month_name = MONTHS[month - 1]
    has_month = (
        re.search(rf"\b{month_name[:3]}", q) is not None           # "Dec" or "December"
        or re.search(rf"(?<!\d)0?{month}[/.-]\d", q) is not None    # "12/05" or "12-5"
        or f"{year}-{month:02d}" in q                               # "2026-12-05"
    )
    has_day = re.search(rf"(?<!\d)0?{day}(?!\d)", q) is not None
    return has_year and has_month and has_day


def summarize(items: list[dict]) -> tuple[int, int]:
    """(verified count, total) over items that carry an "evidence" dict."""
    verified = sum(1 for item in items if item["evidence"]["status"] == "verified")
    return verified, len(items)
