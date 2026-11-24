"""Live end-to-end check of the UI: uploads real PDFs through app.py and calls Gemini for real.

Usage:  python scripts/ui_smoke.py docs/samples/*.pdf      (needs GEMINI_API_KEY or secrets.toml)
Each PDF is uploaded twice in the same session; the second run should be instant (cache hit).
"""

import os
import sys
import time
import tomllib
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
if not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = tomllib.loads((ROOT / ".streamlit" / "secrets.toml").read_text())["GEMINI_API_KEY"]

at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=240)
at.run()
for path in sys.argv[1:]:
    for attempt in ("first upload", "repeat upload"):
        started = time.time()
        at.file_uploader[0].set_value((Path(path).name, Path(path).read_bytes(), "application/pdf"))
        at.run()
        seconds = time.time() - started
        banner = [e.value for e in at.success] + [e.value for e in at.error]
        evidence = next((m.value for m in at.markdown if "quotes verified" in m.value), "missing")
        print(f"{Path(path).name} [{attempt}] {seconds:.1f}s | exception={bool(at.exception)} | "
              f"banner={banner[:1]} | {evidence.strip('# ')} | tabs={[t.label for t in at.tabs]}")
        if at.exception:
            print(at.exception[0].value)
