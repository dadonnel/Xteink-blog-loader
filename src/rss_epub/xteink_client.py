"""Client helpers for Xteink device sync/upload operations."""

from __future__ import annotations

import shutil
from pathlib import Path

import requests

from .config import (
    SYNC_DIR,
    XTEINK_HEALTHCHECK_PATH,
    XTEINK_HOST,
    XTEINK_UPLOAD_FIELD_NAME,
    XTEINK_UPLOAD_PATH,
)


class XteinkClient:
    def __init__(self, host: str = XTEINK_HOST):
        self.host = host

    def ping(self, timeout_seconds: int = 3) -> bool:
        """Ping device HTTP endpoint to check if it's reachable."""
        url = f"http://{self.host}{XTEINK_HEALTHCHECK_PATH}"
        try:
            response = requests.get(url, timeout=timeout_seconds)
            return response.ok
        except Exception:
            return False

    def upload_file(self, file_path: Path, timeout_seconds: int = 20) -> bool:
        """Upload EPUB to device endpoint, if available."""
        url = f"http://{self.host}{XTEINK_UPLOAD_PATH}"
        try:
            with Path(file_path).open("rb") as handle:
                response = requests.post(
                    url,
                    files={XTEINK_UPLOAD_FIELD_NAME: (Path(file_path).name, handle)},
                    timeout=timeout_seconds,
                )
            return response.ok
        except Exception:
            return False

    def sync_local_copy(self, epub_path: Path, sync_dir: Path = SYNC_DIR) -> Path:
        """Preserve existing behavior: copy EPUB into local sync folder."""
        sync_dir = Path(sync_dir)
        sync_dir.mkdir(parents=True, exist_ok=True)
        destination = sync_dir / Path(epub_path).name
        shutil.copy2(epub_path, destination)
        return destination
