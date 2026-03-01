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
