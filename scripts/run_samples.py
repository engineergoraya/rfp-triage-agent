"""Run the agent on PDFs from the command line, time each run, and save the JSON reports.

Usage:  python scripts/run_samples.py docs/samples/*.pdf
Reads GEMINI_API_KEY from the environment or .streamlit/secrets.toml. Reports go to output/.
"""

import json
import os
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from triage.pipeline import run_triage  # noqa: E402


def api_key() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    with open(Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml", "rb") as f:
        return tomllib.load(f)["GEMINI_API_KEY"]


if __name__ == "__main__":
    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)
    for path in sys.argv[1:]:
        started = time.time()
        report = run_triage(Path(path).read_bytes(), api_key(), progress=lambda m: print("  ", m))
        seconds = time.time() - started
        verified = sum(1 for x in report["deadlines"] + report["requirements"] if x["evidence"]["status"] == "verified")
        total = len(report["deadlines"]) + len(report["requirements"])
        print(f"{Path(path).name}: {report['decision']} {report['fit_score']}/10 | "
              f"{len(report['deadlines'])} dates, {len(report['requirements'])} requirements | "
              f"evidence {verified}/{total} | {seconds:.1f}s | warnings: {len(report['warnings'])}")
        (out_dir / (Path(path).stem + ".json")).write_text(json.dumps(report, indent=2))
