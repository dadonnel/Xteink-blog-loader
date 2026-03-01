import os
import subprocess
from dataclasses import dataclass, asdict
from typing import Iterable, List, Optional

import requests

DEFAULT_DEVICE_HOST = "192.168.1.211"
DEFAULT_UPLOAD_PATH = "/api/upload"


@dataclass
class UploadResult:
    file_path: str
    uploaded: bool
    status_code: Optional[int] = None
    reason: Optional[str] = None
    response_text: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def ping_device(host: str = DEFAULT_DEVICE_HOST, timeout_seconds: int = 2) -> tuple[bool, str]:
    """Best-effort ping pre-check for target device reachability."""
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout_seconds), host],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False, "ping command not available"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"ping failed: {exc}"

    if proc.returncode == 0:
        return True, "reachable"
    message = proc.stderr.strip() or proc.stdout.strip() or f"ping exit code {proc.returncode}"
    return False, message


def _build_upload_url(device_host: str, upload_path: str) -> str:
    clean_host = device_host.strip()
    if clean_host.startswith("http://") or clean_host.startswith("https://"):
        base = clean_host.rstrip("/")
    else:
        base = f"http://{clean_host.rstrip('/')}"

    if not upload_path.startswith("/"):
        upload_path = f"/{upload_path}"
    return f"{base}{upload_path}"


def upload_epubs(
    epub_paths: Iterable[str],
    *,
    device_host: str = DEFAULT_DEVICE_HOST,
    upload_path: str = DEFAULT_UPLOAD_PATH,
    timeout_seconds: int = 30,
    ping_before_upload: bool = True,
) -> dict:
    """Upload EPUB files to XTEink endpoint and return per-file outcomes."""
    results: List[UploadResult] = []
    endpoint = _build_upload_url(device_host, upload_path)

    ping_ok = True
    ping_message = "skipped"
    if ping_before_upload:
        ping_ok, ping_message = ping_device(device_host)

    for path in epub_paths:
        if not os.path.exists(path):
            results.append(UploadResult(file_path=path, uploaded=False, error="file not found"))
            continue

        try:
            with open(path, "rb") as handle:
                files = {"file": (os.path.basename(path), handle, "application/epub+zip")}
                response = requests.post(endpoint, files=files, timeout=timeout_seconds)
            results.append(
                UploadResult(
                    file_path=path,
                    uploaded=response.ok,
                    status_code=response.status_code,
                    reason=response.reason,
                    response_text=(response.text or "")[:500],
                    error=None if response.ok else "upload rejected",
                )
            )
        except Exception as exc:
            results.append(UploadResult(file_path=path, uploaded=False, error=str(exc)))

    return {
        "device_host": device_host,
        "endpoint": endpoint,
        "ping": {"ok": ping_ok, "message": ping_message},
        "results": [item.to_dict() for item in results],
    }
