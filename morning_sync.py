#!/usr/bin/env python3
"""Daily generator + upload orchestrator.

Behavior:
1. Runs the generation pipeline once at 06:00 local time.
2. From 06:00 to 07:30 local time, checks every minute:
   - ping 192.168.1.211
   - if reachable, uploads pending EPUB files.
3. Persists upload state to a durable JSON file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any

DEFAULT_HOST = os.getenv("MORNING_SYNC_HOST", "192.168.1.211")
DEFAULT_SYNC_DIR = Path("storage/downloads/rss_epub/output_epubs/xteink_sync")
DEFAULT_STATE_FILE = Path("storage/downloads/rss_epub/upload_state.json")
DEFAULT_GENERATOR_CMD = os.getenv("MORNING_SYNC_GENERATOR_CMD", "python3 3dayblogs.py")
DEFAULT_UPLOAD_CMD_TEMPLATE = os.getenv(
    "MORNING_SYNC_UPLOAD_CMD", 'scp "{file}" "root@{host}:/mnt/onboard/"'
)


class UploadState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"records": {}, "meta": {}}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if "records" not in self.data:
                    self.data["records"] = {}
                if "meta" not in self.data:
                    self.data["meta"] = {}
            except json.JSONDecodeError:
                print(f"WARN: state file is invalid JSON, starting fresh: {self.path}")
                self.data = {"records": {}, "meta": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def records(self) -> dict[str, Any]:
        return self.data.setdefault("records", {})

    @property
    def meta(self) -> dict[str, Any]:
        return self.data.setdefault("meta", {})


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def time_today(hour: int, minute: int) -> dt.datetime:
    n = now_local()
    return n.replace(hour=hour, minute=minute, second=0, microsecond=0)


def epoch_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_record_key(path: Path) -> str:
    stat = path.stat()
    digest = file_sha256(path)
    return f"{path.resolve()}|sha256:{digest}|mtime_ns:{stat.st_mtime_ns}"


def run_shell_command(command: str) -> tuple[bool, str]:
    proc = subprocess.run(command, shell=True, text=True, capture_output=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def ping_host(host: str) -> bool:
    proc = subprocess.run(["ping", "-c", "1", "-W", "1", host], capture_output=True)
    return proc.returncode == 0


def run_generator(command: str) -> bool:
    print(f"[{epoch_iso()}] Running generation pipeline: {command}")
    ok, output = run_shell_command(command)
    if output:
        print(output)
    if ok:
        print(f"[{epoch_iso()}] Generation finished successfully")
    else:
        print(f"[{epoch_iso()}] Generation failed")
    return ok


def list_epubs(sync_dir: Path) -> list[Path]:
    if not sync_dir.exists():
        return []
    return sorted(p for p in sync_dir.glob("*.epub") if p.is_file())


def upload_file(path: Path, host: str, cmd_template: str) -> tuple[bool, str]:
    cmd = cmd_template.format(file=shlex.quote(str(path.resolve())), host=host)
    return run_shell_command(cmd)


def ensure_records_for_files(state: UploadState, epub_files: list[Path]) -> list[tuple[str, Path]]:
    pending: list[tuple[str, Path]] = []
    for path in epub_files:
        key = make_record_key(path)
        rec = state.records.get(key)
        if rec is None:
            state.records[key] = {
                "filepath": str(path.resolve()),
                "created_at": epoch_iso(),
                "last_attempt_at": None,
                "attempt_count": 0,
                "uploaded_successfully": False,
                "uploaded_at": None,
                "error": None,
            }
            rec = state.records[key]
        if not rec.get("uploaded_successfully", False):
            pending.append((key, path))
    return pending


def try_upload_pending(state: UploadState, sync_dir: Path, host: str, cmd_template: str) -> None:
    epub_files = list_epubs(sync_dir)
    if not epub_files:
        print(f"[{epoch_iso()}] No EPUB files found in {sync_dir}")
        return

    pending = ensure_records_for_files(state, epub_files)
    if not pending:
        print(f"[{epoch_iso()}] No pending EPUB uploads")
        return

    print(f"[{epoch_iso()}] Attempting {len(pending)} pending upload(s)")
    for key, path in pending:
        rec = state.records[key]
        rec["last_attempt_at"] = epoch_iso()
        rec["attempt_count"] = int(rec.get("attempt_count", 0)) + 1

        ok, out = upload_file(path, host, cmd_template)
        if ok:
            rec["uploaded_successfully"] = True
            rec["uploaded_at"] = epoch_iso()
            rec["error"] = None
            print(f"  OK: {path.name}")
        else:
            rec["uploaded_successfully"] = False
            rec["error"] = out or "unknown upload failure"
            print(f"  FAIL: {path.name}: {rec['error']}")


def cleanup_stale_records(state: UploadState, keep_days: int) -> int:
    cutoff = now_local() - dt.timedelta(days=keep_days)
    removed = 0
    for key in list(state.records.keys()):
        rec = state.records[key]
        created_raw = rec.get("created_at")
        try:
            created = dt.datetime.fromisoformat(created_raw) if created_raw else cutoff
        except ValueError:
            created = cutoff
        if created < cutoff:
            del state.records[key]
            removed += 1
    if removed:
        print(f"[{epoch_iso()}] Cleanup removed {removed} stale record(s)")
    return removed


def sleep_until(target: dt.datetime) -> None:
    while True:
        now = now_local()
        seconds = (target - now).total_seconds()
        if seconds <= 0:
            return
        time.sleep(min(seconds, 30))


def minute_tick_sleep() -> None:
    now = now_local()
    next_minute = (now + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    sleep_until(next_minute)


def run_daily_loop(args: argparse.Namespace) -> None:
    state = UploadState(args.state_file)

    while True:
        now = now_local()
        today_0600 = now.replace(hour=6, minute=0, second=0, microsecond=0)
        today_0730 = now.replace(hour=7, minute=30, second=0, microsecond=0)
        today_str = now.date().isoformat()
        last_gen_date = state.meta.get("last_generation_date")

        if now < today_0600:
            print(f"[{epoch_iso()}] Sleeping until 06:00")
            sleep_until(today_0600)
            continue

        if last_gen_date != today_str and now >= today_0600:
            ok = run_generator(args.generator_cmd)
            if ok:
                state.meta["last_generation_date"] = today_str
            state.save()

        if today_0600 <= now <= today_0730:
            if ping_host(args.host):
                print(f"[{epoch_iso()}] Host {args.host} reachable")
                try_upload_pending(state, args.sync_dir, args.host, args.upload_cmd_template)
                cleanup_stale_records(state, args.cleanup_days)
                state.save()
            else:
                print(f"[{epoch_iso()}] Host {args.host} unreachable; skipping upload")
            minute_tick_sleep()
            continue

        # outside window and after today's work
        tomorrow_0600 = (today_0600 + dt.timedelta(days=1)).replace(hour=6, minute=0)
        print(f"[{epoch_iso()}] Outside upload window; sleeping until next day 06:00")
        cleanup_stale_records(state, args.cleanup_days)
        state.save()
        sleep_until(tomorrow_0600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily generation and morning uploads")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Device host/IP to ping before upload")
    parser.add_argument("--sync-dir", type=Path, default=DEFAULT_SYNC_DIR, help="Directory containing EPUB files to upload")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE, help="Durable JSON state file")
    parser.add_argument("--generator-cmd", default=DEFAULT_GENERATOR_CMD, help="Command used to run generation pipeline")
    parser.add_argument(
        "--upload-cmd-template",
        default=DEFAULT_UPLOAD_CMD_TEMPLATE,
        help='Upload command template. Use {file} and {host} placeholders.',
    )
    parser.add_argument("--cleanup-days", type=int, default=30, help="Keep upload records this many days")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"[{epoch_iso()}] morning_sync started with host={args.host}, sync_dir={args.sync_dir}")
    try:
        run_daily_loop(args)
    except KeyboardInterrupt:
        print("Interrupted, exiting")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
