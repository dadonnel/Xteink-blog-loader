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
