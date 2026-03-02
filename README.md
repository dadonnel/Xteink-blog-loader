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

- Python 3.9+
- Packages:
  - `feedparser`
  - `requests`
  - `ebooklib`
  - `beautifulsoup4`

Install dependencies:

```bash
pip install feedparser requests ebooklib beautifulsoup4
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
  --generator-cmd "python3 3dayblogs.py"
```

Environment variable overrides are also supported:

- `MORNING_SYNC_HOST`
- `MORNING_SYNC_UPLOAD_CMD`
- `MORNING_SYNC_GENERATOR_CMD`
- `MORNING_SYNC_REACHABILITY_METHOD` (`auto`, `ping`, or `tcp`; default `auto`)
- `MORNING_SYNC_TCP_PORT` (used for `tcp`/`auto`, default `22`)
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

## Use the feed update web apps

This repository has two web apps for feed operations:

### 1) Feed manager (add/delete feeds in `feeds.opml`)

Start:

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

Then open `http://127.0.0.1:5000/` and click **Validate feeds** to:

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

> Note: both apps default to port `5000`, so run them one at a time (or change one app's port).

## Configuration

Configuration is controlled by environment variables (or defaults in code):

- `OUTPUT_DIR` (default: `storage/downloads/rss_epub/output_epubs`)
- `SOURCES_FILE` (default: `storage/downloads/rss_epub/feeds.opml`)
- `DAYS_BACK` (default: `3`)

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
