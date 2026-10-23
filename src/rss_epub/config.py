"""Shared configuration for the RSS -> EPUB pipeline.

Every entry point imports these resolved paths so environment overrides behave
the same in the CLI, web UI, and scheduler.
"""

import os
from pathlib import Path

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STORAGE_ROOT = PROJECT_ROOT / "storage" / "downloads" / "rss_epub"

# Input/output files
SOURCES_FILE = Path(os.getenv("SOURCES_FILE", str(STORAGE_ROOT / "feeds.opml")))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(STORAGE_ROOT / "output_epubs")))
SYNC_DIR = Path(os.getenv("MORNING_SYNC_SYNC_DIR", str(OUTPUT_DIR / "xteink_sync")))
HISTORY_FILE = Path(os.getenv("EDITION_HISTORY_FILE", str(STORAGE_ROOT / "edition_history.json")))
SETTINGS_FILE = Path(os.getenv("APP_SETTINGS_FILE", str(STORAGE_ROOT / "settings.json")))
JOBS_FILE = Path(os.getenv("JOBS_STATE_FILE", str(STORAGE_ROOT / "jobs.json")))

# Feed + article processing
DAYS_BACK = int(os.getenv("DAYS_BACK", "3"))
REQUEST_TIMEOUT_SECONDS = 15
STRICT_MIN_CONTENT_LENGTH = 500
MIN_CONTENT_LENGTH = 300

# CrossPoint X4 pocket-reader profile.  These intentionally conservative
# limits keep chapter navigation and first-open image indexing responsive.
EPUB_PROFILE = "crosspoint_x4"
MAX_ARTICLES = 40
MAX_TOTAL_WORDS = 75_000
MAX_ARTICLES_PER_FEED = 5
MAX_IMAGES_PER_ARTICLE = 2
MAX_TOTAL_IMAGES = 50
IMAGE_MAX_WIDTH = 440
IMAGE_MAX_HEIGHT = 700
COVER_SIZE = (480, 800)
TOC_TITLE_MAX_CHARS = 55
LONG_ARTICLE_WORDS = 8_000
ARTICLE_QR_CODES = True
QR_SIZE = 180

# Metadata
BOOK_PREFIX = "genai-weekly"
BOOK_AUTHOR = "GenAI Weekly"
BOOK_LANGUAGE = "en"

# Network/device defaults
XTEINK_HOST = "192.168.1.211"
XTEINK_HEALTHCHECK_PATH = "/"
XTEINK_UPLOAD_PATH = "/upload"
XTEINK_UPLOAD_FIELD_NAME = "file"
