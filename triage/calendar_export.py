"""Bonus feature 2: export exact deadlines as an .ics calendar file.

Hand-written iCalendar (RFC 5545) instead of a library: it is about 40 lines and one less
dependency. Only current dates (not superseded by an addendum) with an exact YYYY-MM-DD are
exported, as all-day events. The time and timezone (as written in the RFP) go into the
description, because guessing a timezone would break the "no guessing" rule.
"""

import hashlib
import re
from datetime import datetime, timedelta, timezone


def _escape(text: str) -> str:
    # RFC 5545 section 3.3.11: backslash, semicolon, comma and newlines must be escaped.
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line: str) -> str:
    # RFC 5545 section 3.1: lines longer than 75 octets are folded with CRLF + one space.
    out, current = [], ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            out.append(current)
            current = " " + char
        else:
            current += char
    out.append(current)
    return "\r\n".join(out)


def build_ics(deadlines: list[dict], rfp_title: str) -> str:
    """Return the .ics text for every current deadline with an exact date (others are skipped)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//rfp-triage-agent//EN", "CALSCALE:GREGORIAN"]

    for item in deadlines:
        if not is_exportable(item):
            continue
        day = datetime.strptime(item["date"], "%Y-%m-%d")
        # A stable UID means re-importing the same RFP updates events instead of duplicating them.
        uid = hashlib.sha1(f"{rfp_title}|{item['what']}|{item['date']}".encode()).hexdigest()
        description = f"Time: {item.get('time', 'Not stated')}\nSource (page {item.get('page')}): {item.get('quote', '')}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@rfp-triage",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"DTEND;VALUE=DATE:{(day + timedelta(days=1)):%Y%m%d}",  # all-day: end is exclusive
            f"SUMMARY:{_escape(item['what'] + ' - ' + rfp_title)}",
            f"DESCRIPTION:{_escape(description)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def is_exportable(item: dict) -> bool:
    """Exact date, and not replaced by an addendum: a superseded deadline in a calendar is dangerous."""
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.get("date", ""))) and not item.get("superseded")


def count_exportable(deadlines: list[dict]) -> int:
    return sum(1 for d in deadlines if is_exportable(d))
