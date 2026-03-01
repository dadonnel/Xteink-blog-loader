"""Shared configuration constants for RSS -> EPUB pipeline."""

from pathlib import Path

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STORAGE_ROOT = PROJECT_ROOT / "storage" / "downloads" / "rss_epub"

# Input/output files
SOURCES_FILE = STORAGE_ROOT / "feeds.opml"
OUTPUT_DIR = STORAGE_ROOT / "output_epubs"
SYNC_DIR = OUTPUT_DIR / "xteink_sync"

# Feed + article processing
DAYS_BACK = 3
REQUEST_TIMEOUT_SECONDS = 15
STRICT_MIN_CONTENT_LENGTH = 500
MIN_CONTENT_LENGTH = 300

# Metadata
BOOK_PREFIX = "genai-weekly"
BOOK_AUTHOR = "GenAI Weekly"
BOOK_LANGUAGE = "en"

# Network/device defaults
XTEINK_HOST = "192.168.0.10"
XTEINK_HEALTHCHECK_PATH = "/"
XTEINK_UPLOAD_PATH = "/upload"
XTEINK_UPLOAD_FIELD_NAME = "file"
