"""Live eval of the Monday queue, plus a regression diff against saved single-RFP reports.

Usage:
  python scripts/eval_queue.py docs/samples/*.pdf      # rank the PDFs through the queue path
  python scripts/eval_queue.py --diff output           # compare reports in output/ to eval/baseline/

The queue run calls the real pipeline (no cached or hard-coded answers) and saves each report to
eval/latest/. The diff checks what must not change: decision, fit score (within 1 point) and the
set of exact dates. Needs GEMINI_API_KEY or .streamlit/secrets.toml for the queue run.
"""

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.run_samples import api_key  # noqa: E402
from triage.pipeline import run_triage  # noqa: E402
from triage.ranking import recommendation_line, run_queue  # noqa: E402

BASELINE = ROOT / "eval" / "baseline"
LATEST = ROOT / "eval" / "latest"


def exact_dates(report: dict) -> set[str]:
    return {d["date"] for d in report["deadlines"] if d["date"][:1].isdigit()}


def diff(new_dir: Path) -> bool:
    """Print one line per sample; return True only if nothing important changed."""
    ok = True
    for base_file in sorted(BASELINE.glob("*.json")):
        new_file = new_dir / base_file.name
        if not new_file.exists():
            print(f"MISSING  {base_file.name}: no new report in {new_dir}")
            ok = False
            continue
        old, new = json.loads(base_file.read_text()), json.loads(new_file.read_text())
        problems = []
        if old["decision"] != new["decision"]:
            problems.append(f"decision {old['decision']} -> {new['decision']}")
        if abs(old["fit_score"] - new["fit_score"]) > 1:
            problems.append(f"fit score {old['fit_score']} -> {new['fit_score']}")
        if exact_dates(old) != exact_dates(new):
            problems.append(f"dates removed {sorted(exact_dates(old) - exact_dates(new))}, "
                            f"added {sorted(exact_dates(new) - exact_dates(old))}")
        print(f"{'CHANGED' if problems else 'SAME   '}  {base_file.name}: {new['decision']} "
              f"{new['fit_score']}/10, {len(exact_dates(new))} exact dates. {'; '.join(problems)}")
        ok = ok and not problems
    return ok


def main(paths: list[str]) -> None:
    key = api_key()
    files = [(Path(p).name, Path(p).read_bytes()) for p in paths]
    result = run_queue(files, lambda data: run_triage(data, key), date.today(), progress=lambda m, f=None: print(" ", m))
    print("\n" + recommendation_line(result["rows"]))
    columns = ["Rank", "File", "Agency", "Decision", "Fit score", "Proposal due", "Days left", "Status",
               "Next must-attend event", "Biggest deal-breaker"]
    for row in result["rows"]:
        print(" | ".join(f"{c}: {row[c]}" for c in columns))
    LATEST.mkdir(parents=True, exist_ok=True)
    for name, report in result["reports"].items():
        (LATEST / (Path(name).stem + ".json")).write_text(json.dumps(report, indent=2))
    print(f"\nReports saved to {LATEST}. Regression diff against {BASELINE}:")
    diff(LATEST)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--diff"]:
        sys.exit(0 if diff(Path(sys.argv[2])) else 1)
    main(sys.argv[1:])
