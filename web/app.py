import importlib.util
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)
_run_lock = threading.Lock()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from xteink_client import upload_epubs

PIPELINE_SCRIPT = PROJECT_ROOT / "3dayblogs.py"

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>XTEink Blog Runner</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; }
    button { padding: 0.6rem 1rem; font-size: 1rem; }
    pre { background: #f5f5f5; padding: 1rem; border-radius: 8px; overflow: auto; }
  </style>
</head>
<body>
  <h1>Run Blog Generation + Upload</h1>
  <button id="runBtn">Run now</button>
  <pre id="output">Idle.</pre>
  <script>
    const output = document.getElementById("output");
    const runBtn = document.getElementById("runBtn");

    function renderResult(payload) {
      const lines = [];
      lines.push(`Generation started: ${payload.generation_started_at}`);
      lines.push(`Generation finished: ${payload.generation_finished_at}`);
      lines.push("");
      lines.push("Generated file path(s):");
      (payload.generated_paths || []).forEach((p) => lines.push(`- ${p}`));
      if (!payload.generated_paths || payload.generated_paths.length === 0) {
        lines.push("- none");
      }
      lines.push("");
      lines.push("Upload outcome per file:");
      const results = payload.upload?.results || [];
      results.forEach((r) => {
        const status = r.uploaded ? "SUCCESS" : "FAILED";
        lines.push(`- ${r.file_path}: ${status}`);
        if (r.status_code) lines.push(`    status_code=${r.status_code} (${r.reason || ""})`);
        if (r.error) lines.push(`    error=${r.error}`);
      });
      if (results.length === 0) {
        lines.push("- no uploads attempted");
      }
      if (payload.error) {
        lines.push("");
        lines.push(`Pipeline error: ${payload.error}`);
      }
      output.textContent = lines.join("\n");
    }

    runBtn.addEventListener("click", async () => {
      runBtn.disabled = true;
      output.textContent = "Generation started...";
      try {
        const res = await fetch("/run-now", { method: "POST" });
        const payload = await res.json();
        if (!res.ok) {
          output.textContent = payload.error || `Request failed with status ${res.status}`;
        } else {
          renderResult(payload);
        }
      } catch (err) {
        output.textContent = `Error: ${err.message}`;
      } finally {
        runBtn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location("blog_pipeline", PIPELINE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


@app.post("/run-now")
def run_now():
    if not _run_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A generation run is already in progress."}), 409

    started_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "ok": False,
        "generation_started_at": started_at,
        "generation_finished_at": None,
        "generated_paths": [],
        "upload": {"results": []},
        "error": None,
    }

    try:
        pipeline_module = _load_pipeline_module()
        generation = pipeline_module.run_generation_pipeline()
        payload["generated_paths"] = generation.get("generated_paths", [])
        payload["error"] = generation.get("error")

        if generation.get("ok") and payload["generated_paths"]:
            payload["upload"] = upload_epubs(
                payload["generated_paths"],
                device_host="192.168.1.211",
                upload_path="/api/upload",
                ping_before_upload=True,
            )
        else:
            payload["upload"] = {
                "device_host": "192.168.1.211",
                "endpoint": "http://192.168.1.211/api/upload",
                "ping": {"ok": None, "message": "skipped"},
                "results": [],
            }

        payload["ok"] = generation.get("ok", False)
        payload["generation_finished_at"] = datetime.now(timezone.utc).isoformat()
        return jsonify(payload)
    except Exception as exc:
        payload["error"] = str(exc)
        payload["generation_finished_at"] = datetime.now(timezone.utc).isoformat()
        return jsonify(payload), 500
    finally:
        _run_lock.release()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
