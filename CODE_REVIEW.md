# Code Review Report

## Scope
Reviewed all tracked source files in the repository, with focus on correctness, security, maintainability, and operability.

## Findings

### 1) Critical: `3dayblogs.py` is currently broken at module import time
- The file references `os.getenv(...)` before `os` is imported.
- The file mixes two implementations (new package-based flow + older monolithic functions), causing duplicated logic and partially dead code paths.
- There is a `main()` function that is effectively incomplete while most functional logic is defined outside it.

**Impact:** Running the CLI can fail immediately, and the code is difficult to maintain safely.

**Recommendation:** Refactor `3dayblogs.py` into a single entrypoint that only orchestrates `src/rss_epub/*` services, then remove legacy duplicated helpers.

---

### 2) High: TLS certificate verification is globally disabled
- `src/rss_epub/feed_service.py` overrides Python's default HTTPS context to an unverified context.

**Impact:** HTTPS requests can be silently vulnerable to MITM attacks.

**Recommendation:** Remove the global SSL override; if specific feeds fail TLS, handle via targeted exceptions and optional per-request opt-out flags (default secure).

---

### 3) Medium: Flask app is configured to run with `debug=True`
- `web/app.py` starts with `app.run(debug=True)`.

**Impact:** Unsafe in production, can leak internals and enable debug console exposure if misconfigured.

**Recommendation:** Use environment-driven debug mode and default to `False`.

---

### 4) Medium: Test execution is inconsistent across environments
- `python -m pytest` works, while direct `pytest` invocation may fail in some setups due import path behavior.

**Impact:** Confusing CI/local behavior and non-deterministic test invocation.

**Recommendation:** Pin test discovery/import behavior using a repo-level `pytest.ini`.

---

### 5) Low: Multiple duplicate modules exist at root and `src/rss_epub/`
- There are parallel implementations (`feed_service.py`, `opml_store.py`, `xteink_client.py`) both at root and in package form.

**Impact:** Increases maintenance risk and chance of importing the wrong implementation.

**Recommendation:** Consolidate on one implementation path (prefer package under `src/rss_epub`) and remove/alias legacy modules intentionally.

## Positive notes
- OPML store write path uses atomic file replacement (`os.replace`) for safer updates.
- Feed validation in `feed_service.py` preserves input order using indexed futures.
- Systemd unit/timer artifacts exist for scheduled sync operations.
