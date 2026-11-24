"""Generate two SYNTHETIC, text-based RFP PDFs with known answers, for testing.

The agencies are fictional. Each RFP hides traps the agent must handle:
  - software RFP: a proposal due date that differs between sections (must be flagged as ambiguous),
    an award date without a day (must be "Not stated"), a start date relative to award, and a
    FedRAMP requirement buried in an appendix (must not be missed).
  - construction RFP: should trigger the fast no-bid gate, despite one small "building automation
    controls" line item that could tempt a model into calling it software work.

Run:  python scripts/make_samples.py     (writes docs/synthetic/synthetic_*.pdf)
"""

from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

OUT = Path(__file__).resolve().parent.parent / "docs" / "synthetic"

SOFTWARE_RFP = [
    [
        "# COMMONWEALTH OF WESTBROOK - DEPARTMENT OF LICENSING AND COMPLIANCE",
        "# Request for Proposals No. DLC-2026-114",
        "## Compliance Case Management and Automation Platform",
        "Issue date: November 2, 2026",
        "Proposals are due January 15, 2027 at 2:00 PM Eastern Time.",
        "Procurement contact: Office of Contracts, Department of Licensing and Compliance, 400 State Street, Capital City, Westbrook.",
        "This procurement is a Total Small Business Set-Aside under NAICS code 541511 (Custom Computer Programming Services).",
    ],
    [
        "## Section 1. Background",
        "The Department of Licensing and Compliance (DLC) regulates more than 40,000 licensed businesses. "
        "Case files, inspections and enforcement actions are tracked today in spreadsheets and a 2009 desktop database. "
        "Staff spend most of their time re-keying data and chasing paper forms.",
        "The Department seeks a modern, cloud-hosted platform that automates the compliance case lifecycle, from complaint intake "
        "through investigation, enforcement and closure.",
        "## Section 2. Scope of Work",
        "2.1 The Contractor shall design, build, deploy and support a web-based case management application for DLC staff.",
        "2.2 The Contractor shall provide configurable workflow automation, including rules-based routing, deadline tracking and automated notices.",
        "2.3 The Contractor shall provide AI-assisted document review that extracts key fields from uploaded complaints and evidence, "
        "with a human reviewer approving every extraction.",
        "2.4 The Contractor shall deliver a mobile application (iOS and Android) for field inspectors that works offline.",
        "2.5 The Contractor shall migrate approximately 250,000 legacy records and provide a complete audit trail for every case action.",
    ],
    [
        "## Section 3. Schedule of Events",
        "Pre-proposal conference (virtual): November 20, 2026 at 10:00 AM Eastern Time.",
        "Deadline for written questions: December 4, 2026 at 5:00 PM Eastern Time. Questions must be emailed to contracts@dlc.westbrook.example.",
        "Answers to questions posted: December 11, 2026.",
        "Proposal due date: January 15, 2027 at 2:00 PM Eastern Time.",
        "Anticipated contract award: February 2027.",
        "Anticipated contract start: March 1, 2027.",
        "Phase 1 go-live: no later than 180 days after contract start.",
    ],
    [
        "## Section 4. Contract Terms",
        "4.1 Contract type: firm-fixed-price for the implementation, with time-and-materials for approved change requests.",
        "4.2 The total contract value shall not exceed $1,850,000 across the base and option periods.",
        "4.3 Period of performance: a base period of two (2) years with two (2) one-year option periods.",
        "4.4 Place of performance: the Contractor's own facilities. The Contractor shall attend up to four (4) on-site working "
        "sessions per year at DLC headquarters in Capital City, Westbrook.",
    ],
    [
        "## Section 5. Offeror Qualifications",
        "5.1 Offerors must be registered in SAM.gov with an active Unique Entity ID at the time of proposal submission.",
        "5.2 The Offeror shall provide a current SOC 2 Type II report covering the proposed hosting environment.",
        "5.3 All user interfaces must conform to WCAG 2.1 Level AA.",
        "5.4 Key personnel: the Offeror shall name a Project Manager holding an active PMP certification and a Lead Engineer "
        "with at least eight (8) years of software development experience.",
        "5.5 Past performance: Offerors must describe at least two (2) similar projects completed within the last five (5) years, "
        "each with a value of at least $500,000.",
        "5.6 No security clearance is required. All Contractor staff with access to production data shall pass a state background check.",
        "5.7 The Contractor shall maintain cyber liability insurance of at least $2,000,000 per occurrence.",
    ],
    [
        "## Section 6. Instructions for Submission (Section L)",
        "6.1 Proposals must be submitted electronically through the Westbrook eProcurement Portal. Email submissions will be rejected.",
        "6.2 Proposals must be received no later than January 16, 2027 at 2:00 PM Eastern Time.",
        "6.3 The technical volume is limited to 30 pages. The price volume shall be submitted as a separate PDF file.",
        "## Section 7. Evaluation (Section M)",
        "Technical approach 40%, past performance 25%, key personnel 15%, price 20%. Award will be made on a best-value basis.",
    ],
    [
        "## Appendix A. Pricing Form",
        "Offerors shall complete the pricing form for Phase 1 implementation, Phase 2 implementation, annual support "
        "for each option year, and a blended hourly rate for change requests.",
        "## Appendix B. Legacy System Overview",
        "The legacy system is a Microsoft Access 2010 database with 38 tables. Data quality is inconsistent; duplicate business "
        "records are common.",
    ],
    [
        "## Appendix C. Security and Hosting Requirements",
        "C.1 The Contractor shall host all Department data within the continental United States in a FedRAMP Moderate authorized cloud environment.",
        "C.2 The system shall enforce multi-factor authentication for all staff accounts.",
        "C.3 The Contractor shall report any security incident to the Department within 24 hours of discovery.",
    ],
]

CONSTRUCTION_RFP = [
    [
        "# CITY OF RIVERTON - DEPARTMENT OF PUBLIC WORKS",
        "# Invitation for Bids No. PW-26-031",
        "## Renovation of Fire Station No. 3",
        "Sealed bids will be opened publicly on December 18, 2026 at 11:00 AM local time at Riverton City Hall, Room 210.",
        "Engineer's estimate: $3,400,000.",
    ],
    [
        "## Part 1. Scope of Work",
        "The work consists of the interior and exterior renovation of Fire Station No. 3, 112 Mill Road, Riverton, including: "
        "demolition of the existing apparatus bay floor, a new reinforced concrete slab, replacement of the roof membrane, "
        "new HVAC equipment, electrical service upgrade to 800 amps, plumbing fixtures, and new overhead bay doors.",
        "The work also includes replacement of the building automation system controls for the HVAC equipment, furnished and "
        "installed by the mechanical subcontractor.",
        "Contract type: lump-sum construction contract. Time for completion: 300 calendar days from the Notice to Proceed.",
        "The Notice to Proceed will be issued within 30 days of contract award.",
    ],
    [
        "## Part 2. Schedule",
        "A mandatory pre-bid site visit will be held on December 3, 2026 at 9:00 AM at Fire Station No. 3. Bids from firms "
        "that did not attend will not be accepted.",
        "Written questions are due by December 10, 2026 at 4:00 PM.",
        "## Part 3. Bidder Requirements",
        "3.1 Bidders must hold a valid State of Riverton Class A General Contractor license.",
        "3.2 Each bid shall be accompanied by a bid bond equal to five percent (5%) of the bid amount.",
        "3.3 The successful bidder shall furnish performance and payment bonds each equal to 100% of the contract price.",
        "3.4 Prevailing wage rates apply to all labor on this project.",
        "3.5 The site superintendent shall hold an OSHA 30-hour construction safety card.",
        "3.6 Bidders shall list at least three public building renovation projects of $2,000,000 or more completed in the last seven years.",
    ],
    [
        "## Part 4. Bid Submission",
        "Bids shall be submitted in a sealed envelope marked 'IFB PW-26-031' and delivered to the City Clerk, Riverton City Hall, "
        "500 Main Street, before the bid opening time. Faxed or emailed bids will not be accepted.",
    ],
]


def build(pages: list[list[str]], path: Path) -> None:
    styles = getSampleStyleSheet()
    story = []
    for i, page in enumerate(pages):
        for line in page:
            if line.startswith("# "):
                story.append(Paragraph(line[2:], styles["Title"]))
            elif line.startswith("## "):
                story.append(Paragraph(line[3:], styles["Heading2"]))
            else:
                story.append(Paragraph(line, styles["BodyText"]))
            story.append(Spacer(1, 6))
        if i < len(pages) - 1:
            story.append(PageBreak())
    SimpleDocTemplate(str(path), pagesize=LETTER).build(story)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    build(SOFTWARE_RFP, OUT / "synthetic_software_rfp.pdf")
    build(CONSTRUCTION_RFP, OUT / "synthetic_construction_rfp.pdf")
    print(f"Wrote 2 synthetic PDFs to {OUT}")
