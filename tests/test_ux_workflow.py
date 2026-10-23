import json
import threading
import time
from http import HTTPStatus

import app
from rss_epub.history import HistoryStore, article_key, normalize_article_url
from rss_epub.selection import balanced_select


def test_no_content_exit_never_reports_created(monkeypatch):
    class Result:
        returncode = 0
        stderr = ""
        stdout = 'No recent articles.\nXTEINK_RESULT_JSON={"status":"no_new_articles","reason":"Nothing new","warnings":[]}'

    monkeypatch.setattr(app.subprocess, "run", lambda *args, **kwargs: Result())
    payload, status = app.build_generate_epub_payload(3)
    assert status == HTTPStatus.OK
    assert payload["status"] == "no_new_articles"
    assert "edition_id" not in payload
    assert "output_file" not in payload


def test_send_uses_exact_edition_file(monkeypatch, tmp_path):
    first = tmp_path / "first.epub"; first.write_bytes(b"first")
    newer = tmp_path / "newer.epub"; newer.write_bytes(b"newer")
    history_path = tmp_path / "history.json"
    history = HistoryStore(history_path)
    history.save({"version": 1, "articles": {}, "editions": {
        "wanted": {"edition_id": "wanted", "sync_file": str(first), "created_at": "2026-01-01"},
        "newer": {"edition_id": "newer", "sync_file": str(newer), "created_at": "2026-01-02"},
    }})
    monkeypatch.setattr(app, "HISTORY_FILE", history_path)
    monkeypatch.setattr(app, "UPLOAD_STATE_FILE", tmp_path / "upload-state.json")
    monkeypatch.setattr(app, "host_reachable", lambda *args: True)
    sent = []
    monkeypatch.setattr(app, "upload_file", lambda path, *args: (sent.append(path) is None, "accepted"))

    payload, status = app.build_upload_edition_payload("wanted")
    assert status == HTTPStatus.OK
    assert payload["edition_id"] == "wanted"
    assert sent == [first]


def test_history_survives_local_file_cleanup(tmp_path):
    path = tmp_path / "history.json"
    store = HistoryStore(path)
    article = {"title": "One", "url": "https://EXAMPLE.com/post/?utm_source=x", "source": "A"}
    store.record_edition({"edition_id": "edition-1", "created_at": "2026-01-01", "output_file": str(tmp_path / "gone.epub")}, [article])
    assert article_key(article) in store.collected_keys()
    assert store.latest_edition()["edition_id"] == "edition-1"
    assert normalize_article_url(article["url"]) == "https://example.com/post"


def test_balanced_selection_does_not_exhaust_first_source():
    candidates = [
        *[{"title": f"A{i}", "source": "A", "word_count": 100} for i in range(5)],
        *[{"title": f"B{i}", "source": "B", "word_count": 100} for i in range(2)],
        *[{"title": f"C{i}", "source": "C", "word_count": 100} for i in range(2)],
    ]
    selected, omitted = balanced_select(candidates, max_articles=4, max_words=10_000)
    assert [item["source"] for item in selected[:3]] == ["A", "B", "C"]
    assert len(selected) == 4
    assert all(item["omitted_reason"] == "budget_limit" for item in omitted)


def test_duplicate_background_request_reuses_active_job(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "JOBS_STATE_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(app, "JOBS", {})
    monkeypatch.setattr(app, "_save_jobs_locked", lambda: None)
    started = threading.Event(); release = threading.Event()

    def worker():
        started.set(); release.wait(2)
        return {"status": "ok"}, HTTPStatus.OK

    first = app._start_background_job("generate-epub", worker)
    assert started.wait(1)
    second = app._start_background_job("generate-epub", worker)
    release.set()
    assert second == first
    deadline = time.time() + 1
    while app.JOBS[first]["status"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert app.JOBS[first]["status"] == "succeeded"

