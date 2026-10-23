"""Run the UI against inert screenshot fixtures; never contacts feeds or devices."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = os.getenv("UI_FIXTURE_STATE", "empty")
FIXTURES = ROOT / "tests" / "fixtures"
RUNTIME = Path(tempfile.mkdtemp(prefix="xteink-ui-fixture-"))

os.environ["EDITION_HISTORY_FILE"] = str(FIXTURES / f"history-{STATE if STATE in {'ready', 'offline'} else 'empty'}.json")
os.environ["JOBS_STATE_FILE"] = str(RUNTIME / "jobs.json")
os.environ["MORNING_SYNC_STATE_FILE"] = str(RUNTIME / "upload-state.json")
os.environ["APP_SETTINGS_FILE"] = str(RUNTIME / "settings.json")
os.environ["MORNING_SYNC_SYNC_DIR"] = str(FIXTURES)
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

if STATE == "preparing":
    now = time.time()
    app.JOBS = {
        "fixture-preparing": {
            "job_id": "fixture-preparing",
            "action": "generate-epub",
            "status": "running",
            "stage": "fetching_and_building",
            "created_at": now,
            "updated_at": now,
            "logs": ["Fixture: fetching feeds", "Fixture: extracting readable articles"],
            "result": None,
            "http_status": 200
        }
    }

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001)
