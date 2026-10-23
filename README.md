# Xteink Blog Loader

`Xteink-blog-loader` is a Python utility that turns recent RSS/Atom posts into a single EPUB optimized for e-ink reading.

## Features

- Reads feed subscriptions from an OPML file.
- Pulls entries from each feed and filters to recent posts (default: last 3 days).
- Deduplicates article links.
- Extracts and cleans article content for a reading-friendly format.
- Builds one compiled EPUB per run.
- Copies the EPUB into an `xteink_sync` folder for downstream sync workflows.

## Repository contents

- `3dayblogs.py` — main script.
- `feeds.opml` — default feed list included in this repository.

## Requirements

- Python 3.10+ (the application uses modern type-union syntax)
- Packages:
  - `feedparser`
  - `requests`
  - `ebooklib`
  - `beautifulsoup4`
  - `pillow` (for CrossPoint image normalization)
  - `qrcode` (optional original-article QR codes)

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Quick start

From the repository root:

```bash
python3 3dayblogs.py
```

The script will:

1. Load feeds from `SOURCES_FILE`.
2. Fetch and filter recent feed entries.
3. Extract readable article content.
4. Generate `genai-weekly-YYYY-MM-DD.epub` in `OUTPUT_DIR`.
5. Copy the EPUB to `OUTPUT_DIR/xteink_sync/`.

## Run the morning sync process

`morning_sync.py` is a long-running scheduler that:

1. Runs the generator once at 06:00 local time.
2. From 06:00–07:30, checks every minute whether your e-reader host is reachable.
3. Uploads pending EPUB files from the sync folder when the host responds.

Start it manually from the repo root:

```bash
python3 morning_sync.py
```

Useful options:

```bash
python3 morning_sync.py \
  --host 192.168.1.211 \
  --sync-dir storage/downloads/rss_epub/output_epubs/xteink_sync \
  --state-file storage/downloads/rss_epub/upload_state.json \
  --generator-cmd "python3 3dayblogs.py" \
  --upload-path /upload \
  --upload-field-name file
```

Environment variable overrides are also supported:

- `MORNING_SYNC_HOST`
- `MORNING_SYNC_UPLOAD_PATH`
- `MORNING_SYNC_UPLOAD_FIELD_NAME`
- `MORNING_SYNC_GENERATOR_CMD`
- `MORNING_SYNC_REACHABILITY_METHOD` (`auto`, `ping`, or `tcp`; default `auto`)
- `MORNING_SYNC_TCP_PORT` (used for `tcp`/`auto`, default `80`)
- `MORNING_SYNC_CONNECT_TIMEOUT` (seconds, default `1.0`)

### Run morning sync as a systemd service

The repository includes a service and timer in `deploy/systemd/`.

Install and enable:

```bash
sudo cp deploy/systemd/morning-sync.service /etc/systemd/system/
sudo cp deploy/systemd/morning-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now morning-sync.timer
```

Check status/logs:

```bash
systemctl status morning-sync.service
journalctl -u morning-sync.service -f
```

## Recommended launch

The control panel is the recommended entry point. From the repository root:

```bash
python app.py
```

Open `http://127.0.0.1:5001/`. Xteink Reader is organized around four destinations:
**Today** for the current reading and delivery state, **Editions** for preparation
and saved copies, **Sources** for subscriptions, and **Settings** for the reader and
morning schedule. The legacy `/dashboard`, `/epubs`, `/files`, and `/feeds` URLs
redirect to their new destinations.

Preparing and sending are separate choices. The interface shows the exact article
count and reading estimate for a built edition, and sends that same edition. If the
reader is offline, the built edition remains pending and can be retried without
rebuilding.

### Interface previews

- [Today: first-use state](artifacts/screenshots/today-empty-desktop.png)
- [Today: preparing an edition](artifacts/screenshots/today-preparing-desktop.png)
- [Today: edition ready](artifacts/screenshots/today-ready-desktop.png)
- [Today: reader offline](artifacts/screenshots/today-offline-desktop.png)
- [Editions](artifacts/screenshots/editions-desktop.png)
- [Sources](artifacts/screenshots/sources-desktop.png)
- [Settings](artifacts/screenshots/settings-desktop.png)
- [Today at 390 px](artifacts/screenshots/today-empty-mobile.png)

These previews use inert local fixtures. They do not fetch live feeds or contact a
reader.

The CLI (`python 3dayblogs.py`) remains available for automation. `web/app.py` is
a legacy feed-only entry point on port 5000; it is retained for compatibility but
does not provide edition history or delivery status.

## Edition and article history

Successful builds are recorded in `storage/downloads/rss_epub/edition_history.json`
by default. Articles are marked collected only after an EPUB is written. Removing a
local EPUB does not remove this history. Existing EPUBs are not used to invent
article history; they remain usable as local files but their contents are not
assumed to have been collected.

Each edition has a unique filename and EPUB identifier, including for multiple
builds on the same day. The normal CLI mode excludes collected articles. Set
`INCLUDE_PREVIOUS_ARTICLES=true` for an intentional repeat edition, or use
`EDITION_PRESET=quick`, `commute`, or `full` (6/9,000 words; 15/25,000; or
40/75,000 respectively).

## Use the feed update web apps

This repository has two web apps for feed operations:

### 1) Feed manager (add/delete feeds in `feeds.opml`)

Legacy start:

```bash
python3 web/app.py
```

Then open `http://127.0.0.1:5000/` and:

- Add feeds with name, URL, and optional category.
- Review current feeds grouped by category.
- Remove feeds with the Delete button.

### 2) Feed validator (check feed freshness)

Start:

```bash
python3 app.py
```

The main control panel now includes validation. The following legacy description is
kept for older integrations; `app.py` actually listens on port **5001** by default.

- Fetch all configured feed URLs from `feeds.opml`.
- Validate each feed response.
- View entry counts within 1, 7, and 30 days.

Manual upload of pending EPUBs is also exposed via POST endpoints:

- `POST /upload-pending`
- `POST /upload-epubs` (alias)

Example:

```bash
curl -X POST http://127.0.0.1:5000/upload-epubs
```

> Note: the legacy feed manager defaults to port `5000`; the main control panel
> defaults to port `5001`, so they can run side by side.

## Configuration

Configuration is controlled by environment variables (or defaults in code):

- `OUTPUT_DIR` (default: `storage/downloads/rss_epub/output_epubs`)
- `SOURCES_FILE` (default: `storage/downloads/rss_epub/feeds.opml`)
- `DAYS_BACK` (default: `3`)
- `EDITION_HISTORY_FILE` (durable article/edition history)
- `MORNING_SYNC_SYNC_DIR` and `MORNING_SYNC_STATE_FILE`
- `JOBS_STATE_FILE` (durable web-job explanations across restarts)
- `MORNING_SYNC_GENERATION_TIME` (default `06:00`)
- `MORNING_SYNC_DELIVERY_WINDOW_END` (default `07:30`)
- `MORNING_SYNC_TIMEZONE` (default `America/New_York`)

Environment variables override defaults for every entry point. Keep output, sync,
history, and upload-state files on writable storage. On first run, an empty OPML is
valid: add a subscription in the UI before preparing an edition.

## Delivery and cleanup safety

A successful HTTP upload means only that the configured endpoint accepted the
request; it does not prove that firmware indexed or displayed the book. Local-copy
cleanup preserves the newest edition and moves eligible delivered files into the
sync folder's `.trash` directory. Review that directory before permanent removal.

For an unreachable reader, verify the configured address, that both devices are on
the same network, and that the reader is in the firmware's transfer/upload mode.
Do not assume an upload endpoint exists on every firmware version.

### Manual check with an actual Xteink

1. Back up the reader and confirm its firmware documentation supports the configured HTTP upload endpoint.
2. Open Settings, enter the device address, and use **Test connection**; remember this checks only network reachability.
3. Prepare a small **Quick read** edition and verify its title/article count before selecting **Send this edition**.
4. Confirm the same filename appears on the reader and opens correctly; the web app cannot verify on-screen indexing.
5. Turn the reader offline, prepare another edition, confirm it remains queued, then retry after reconnecting without rebuilding.
6. Preview local cleanup, confirm the newest edition is preserved, and verify moved files are recoverable from `.trash`.

### Path fallback behavior

If `SOURCES_FILE` does not exist, the script automatically falls back to `./feeds.opml` when that file is present.

This lets the repo run out-of-the-box without extra file moves.

### Example custom run

```bash
OUTPUT_DIR=./output SOURCES_FILE=./feeds.opml DAYS_BACK=7 python3 3dayblogs.py
```

## Notes

- A browser-like `User-Agent` is set for better compatibility with feeds and article pages.
- The extractor first tries a strict content cleanup pass, then falls back to a looser extraction mode if needed.
- Very short extracted content is skipped to avoid empty/low-value chapters.
