"""Shared persisted settings with explicit environment-variable precedence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import SETTINGS_FILE

DEFAULTS: dict[str, Any] = {
    "device_address": "192.168.1.211",
    "generation_time": "06:00",
    "delivery_window_end": "07:30",
    "timezone": "America/New_York",
}
ENV_KEYS = {
    "device_address": "MORNING_SYNC_HOST",
    "generation_time": "MORNING_SYNC_GENERATION_TIME",
    "delivery_window_end": "MORNING_SYNC_DELIVERY_WINDOW_END",
    "timezone": "MORNING_SYNC_TIMEZONE",
}


class SettingsStore:
    def __init__(self, path: Path | str = SETTINGS_FILE):
        self.path = Path(path)

    def persisted(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def effective(self) -> dict[str, Any]:
        values = {**DEFAULTS, **self.persisted()}
        overridden: dict[str, str] = {}
        for key, env_name in ENV_KEYS.items():
            if env_name in os.environ:
                values[key] = os.environ[env_name]
                overridden[key] = env_name
        values["overridden_by_environment"] = overridden
        return values

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULTS)
        data = self.persisted()
        for key in allowed:
            if key in changes:
                value = str(changes[key]).strip()
                if not value:
                    raise ValueError(f"{key} cannot be empty")
                data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False, suffix=".tmp") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            temp = Path(handle.name)
        temp.replace(self.path)
        return self.effective()
