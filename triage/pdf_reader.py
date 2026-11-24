"""Turn an uploaded PDF into per-page text the model (and the evidence check) can use."""

import io

from pypdf import PdfReader

# A text-based page almost always has more than this many characters.
# If the whole document averages less, it is probably a scanned image with no text layer.
MIN_CHARS_PER_PAGE = 50

# Rough rule of thumb: 1 token is about 4 characters of English text.
# 100 pages of dense RFP text is roughly 60k-120k tokens, far below this cap.
# The cap protects the free-tier per-minute token quota and the context window.
MAX_INPUT_TOKENS = 400_000


class PdfError(Exception):
    """A problem with the PDF itself. The message is shown to the user as-is."""


def extract_pages(pdf_bytes: bytes) -> list[str]:
    """Return a list with the text of every page (index 0 = page 1)."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # pypdf raises many different error types for broken files
        raise PdfError(f"Could not read this PDF ({exc}). Is it a valid, unencrypted PDF?") from exc

    if not pages:
        raise PdfError("This PDF has no pages.")

    total_chars = sum(len(p.strip()) for p in pages)
    if total_chars / len(pages) < MIN_CHARS_PER_PAGE:
        raise PdfError(
            "This PDF has almost no extractable text. It is probably a scanned image. "
            "Please upload a text-based PDF (or run OCR on it first)."
        )
    return pages


def tag_pages(pages: list[str]) -> str:
    """Join pages into one string with a [PAGE n] marker before each page.

    The markers let the model cite the page a date or requirement came from,
    which the evidence check then verifies.
    """
    return "\n\n".join(f"[PAGE {i}]\n{text}" for i, text in enumerate(pages, start=1))


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def check_length(tagged_text: str) -> None:
    tokens = estimate_tokens(tagged_text)
    if tokens > MAX_INPUT_TOKENS:
        raise PdfError(
            f"This document is too long to analyse in one pass (about {tokens:,} tokens; "
            f"the limit is {MAX_INPUT_TOKENS:,}). Try uploading the main RFP without large attachments."
        )
