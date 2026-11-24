"""Download the triage report as a Word (.docx) file, using only the standard library.

A .docx is a zip of XML files. Three are enough for Word, Google Docs and LibreOffice to open it:
[Content_Types].xml (what each part is), _rels/.rels (where the document is), word/document.xml
(the text). Headings are bold, larger runs, so no styles.xml is needed. Save as PDF from Word
if a PDF is needed.
"""

import io
import re
import zipfile
from xml.sax.saxutils import escape

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml"
 ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Target="word/document.xml"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>
</Relationships>"""


def _para(text, size: int = 22, bold: bool = False) -> str:
    """One paragraph. `size` is in half-points (22 = 11 pt). Text is untrusted, so it is cleaned:
    control characters (common in PDF text) are not allowed in XML and would corrupt the file."""
    clean = escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text)))
    style = f"<w:rPr>{'<w:b/>' if bold else ''}<w:sz w:val=\"{size}\"/></w:rPr>"
    return f'<w:p><w:r>{style}<w:t xml:space="preserve">{clean}</w:t></w:r></w:p>'


def build_docx(report: dict) -> bytes:
    facts = report["facts"]
    label = "BID" if report["decision"] == "BID" else "NO BID"
    body = [_para(f"RFP triage: {facts['title']}", 36, True),
            _para(f"Decision: {label} | Fit score: {report['fit_score']}/10", 26, True)]

    def section(title, lines):
        body.append(_para(title, 28, True))
        body.extend(_para(f"• {line}") for line in (lines or ["None"]))

    body.append(_para("Summary", 28, True))
    body.append(_para(facts["summary"]))
    section("Key facts", [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in facts.items()
                          if k not in ("title", "summary")])
    def date_line(d):
        superseded = " (superseded)" if d["superseded"] and "superseded" not in d["what"].lower() else ""
        return f"{d['date']}: {d['what']}{superseded} (p. {d['page']})"
    section("Key dates", [date_line(d) for d in report["deadlines"]])
    section("Requirements", [f"[{r['category'].replace('_', ' ')}] {r['requirement']}"
                             f"{' (mandatory)' if r['mandatory'] else ''} (p. {r['page']})"
                             for r in report["requirements"]])
    section("Fit score factors", [f"{f['points']:+d} {f['factor']}: {f['reason']}" for f in report["score_factors"]])
    section("Three strongest reasons", report["reasons"])
    section("Deal-breakers", report["deal_breakers"])
    section("What would change this decision", report["what_would_change_decision"])
    section("Open questions for the agency", report["open_questions"])
    section("Ambiguities", [f"{a['issue']} (p. {a['page']})" for a in report["ambiguities"]])
    section("Warnings", report["warnings"])

    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(body)}</w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", CONTENT_TYPES)
        docx.writestr("_rels/.rels", RELS)
        docx.writestr("word/document.xml", document)
    return buffer.getvalue()
