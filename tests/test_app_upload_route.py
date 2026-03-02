import threading
import time
from pathlib import Path

import app


def test_build_manual_upload_payload_returns_unreachable(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "one.epub").write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "host_reachable", lambda *args: False)

    payload, status = app.build_manual_upload_payload()

    assert status == 503
    assert payload["status"] == "unreachable"
    assert payload["pending_before"] == 1
    assert payload["uploaded_now"] == 0


def test_build_manual_upload_payload_uploads_when_reachable(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "one.epub").write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "host_reachable", lambda *args: True)

    def fake_try_upload_pending(state, sync_dir: Path, host: str, cmd_template: str):
        for record in state.records.values():
            record["uploaded_successfully"] = True

    monkeypatch.setattr(app, "try_upload_pending", fake_try_upload_pending)

    payload, status = app.build_manual_upload_payload()

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["pending_before"] == 1
    assert payload["pending_after"] == 0
    assert payload["uploaded_now"] == 1


def test_build_manual_upload_payload_serializes_concurrent_calls(monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "one.epub").write_text("epub", encoding="utf-8")

    monkeypatch.setattr(app, "UPLOAD_SYNC_DIR", sync_dir)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "host_reachable", lambda *args: True)

    concurrent_entries = []
    current_entries = 0
    guard = threading.Lock()

    def fake_try_upload_pending(state, sync_dir: Path, host: str, cmd_template: str):
        nonlocal current_entries
        with guard:
            current_entries += 1
            concurrent_entries.append(current_entries)
        time.sleep(0.05)
        for record in state.records.values():
            record["uploaded_successfully"] = True
        with guard:
            current_entries -= 1

    monkeypatch.setattr(app, "try_upload_pending", fake_try_upload_pending)

    errors = []

    def run_payload():
        try:
            payload, status = app.build_manual_upload_payload()
            assert status == 200
            assert payload["status"] == "ok"
        except Exception as exc:  # pragma: no cover - assertion collection in threads
            errors.append(exc)

    t1 = threading.Thread(target=run_payload)
    t2 = threading.Thread(target=run_payload)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    assert max(concurrent_entries) == 1
