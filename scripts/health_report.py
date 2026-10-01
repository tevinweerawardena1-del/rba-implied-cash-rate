"""
Prints a Markdown health report and exits with status 0 if there is a problem
to report (the workflow then opens or updates a GitHub issue), or 1 if all is
well. Problems: the update step failed, or docs/data/health.json has warnings.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
health_path = ROOT / "docs" / "data" / "health.json"
outcome = os.environ.get("UPDATE_OUTCOME", "success")
run_url = os.environ.get("RUN_URL", "")

health = json.loads(health_path.read_text()) if health_path.exists() else {}
warnings = list(health.get("warnings", []))
failed = health.get("failed_steps", [])
if outcome == "failure":
    warnings.insert(0, "The update run failed. See docs/data/run_log.txt for the error.")

if not warnings:
    sys.exit(1)

lines = ["The daily rates monitor update found something that needs attention.", ""]
lines += [f"- {w}" for w in warnings]
if failed:
    lines += ["", "Steps that failed today: " + ", ".join(failed)]
lines += ["", f"Checked {health.get('checked', 'unknown')}. Run: {run_url}",
          "", "Most likely causes: a source changed its page or file layout, or a site "
          "blocked the request. The log is in `docs/data/run_log.txt`.",
          "", "This issue updates itself each run and closes automatically once the data is current."]
print("\n".join(lines))
sys.exit(0)
