#!/usr/bin/env python3
"""Legacy entrypoint that now forwards to the unified root app."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import run


if __name__ == "__main__":
    run(port=5002)
