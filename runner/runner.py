#!/usr/bin/env python3
"""Scan ~/.opencode/schedules/ for due wakeups and re-invoke opencode.

Invoked once per minute by cron. For each schedule whose `next_wakeup` has
passed, runs ``opencode run -s <session_id> -- <prompt>``. One-shot schedules
are deleted after firing; interval schedules get their ``next_wakeup``
advanced by ``minutes``.

Environment overrides:
  SCHEDULE_DIR   root directory for schedule files
                 (default ~/.opencode/schedules)
   OPENCODE_BIN   absolute path to the opencode binary
                  (default: shutil.which(\"opencode\"), then
                  /opt/homebrew/bin/opencode on macOS,
                  lastly ~/.opencode/bin/opencode)
  RUNNER_LOG     path to append log output to
                 (default ~/.opencode/runner.log)
  RUNNER_LOCK    path to the run lock file
                 (default ~/.opencode/runner.lock)
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _default_opencode_bin() -> str:
    exe = shutil.which("opencode")
    if exe:
        return exe
    if sys.platform == "darwin":
        for p in ["/opt/homebrew/bin/opencode", "/usr/local/bin/opencode"]:
            if os.path.isfile(p):
                return p
    return os.path.expanduser("~/.opencode/bin/opencode")


SCHEDULE_DIR = Path(os.environ.get("SCHEDULE_DIR", "~/.opencode/schedules")).expanduser()
OPENCODE_BIN = os.environ.get("OPENCODE_BIN") or _default_opencode_bin()
LOG_PATH = Path(os.environ.get("RUNNER_LOG", "~/.opencode/runner.log")).expanduser()
LOCK_PATH = Path(os.environ.get("RUNNER_LOCK", "~/.opencode/runner.lock")).expanduser()


def _log(msg: str) -> None:
    """Append a timestamped line to the log file. Silently no-op on error."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _move_aside(path: Path, reason: str) -> None:
    bad = path.with_suffix(path.suffix + ".bad")
    i = 0
    while bad.exists():
        i += 1
        bad = path.with_suffix(f"{path.suffix}.bad.{i}")
    try:
        path.rename(bad)
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
    _log(f"moved bad schedule {path} -> {bad}: {reason}")


def _process(file_path: Path, now: datetime) -> None:
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _move_aside(file_path, f"unreadable JSON: {e}")
        return

    try:
        next_wakeup = datetime.fromisoformat(data["next_wakeup"])
        session_id = data["session_id"]
        prompt = data["prompt"]
        wakeup_id = data.get("wakeup_id", file_path.stem)
        kind = data.get("type", "once")
        minutes = int(data["minutes"])
    except (KeyError, TypeError, ValueError) as e:
        _move_aside(file_path, f"missing/invalid field: {e}")
        return

    if now < next_wakeup:
        return

    _log(f"triggering wakeup {wakeup_id!r} for session {session_id} (type={kind})")

    # List-form subprocess + shell=False -> no shell injection via prompt content.
    cmd = [OPENCODE_BIN, "run", "-s", session_id, "--", prompt]
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            check=False,
            env={**os.environ, "OPENCODE_YOLO": "true"},
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        _log(f"opencode run timed out after 300s; leaving schedule in place")
        return
    except FileNotFoundError:
        _log(f"FATAL: opencode binary not found at {OPENCODE_BIN}; check OPENCODE_BIN / PATH")
        return
    except OSError as e:
        _log(f"opencode launch failed: {e}")
        return

    if result.returncode != 0:
        _log(
            f"opencode run failed (rc={result.returncode}); leaving schedule in place. "
            f"stderr={(result.stderr or '').strip()[:500]}"
        )
        return

    if kind == "interval":
        data["next_wakeup"] = (now + timedelta(minutes=minutes)).isoformat()
        try:
            with file_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError as e:
            _log(f"could not update interval file {file_path}: {e}")
    else:
        try:
            file_path.unlink()
        except OSError as e:
            _log(f"could not remove one-shot file {file_path}: {e}")


def _cleanup_empty_dirs() -> None:
    """Remove empty per-session subdirs under SCHEDULE_DIR (bottom-up)."""
    for root, dirs, _files in os.walk(SCHEDULE_DIR, topdown=False):
        for name in dirs:
            p = Path(root) / name
            try:
                p.rmdir()
            except OSError:
                pass


def main() -> int:
    if not SCHEDULE_DIR.is_dir():
        return 0

    # Single-flight: if a previous run is still going, bail.
    try:
        lock_fd = LOCK_PATH.open("w", encoding="utf-8")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("another runner is active; skipping this tick")
        return 0

    try:
        now = datetime.now()
        for json_path in sorted(SCHEDULE_DIR.glob("*/*.json")):
            _process(json_path, now)
        _cleanup_empty_dirs()
    finally:
        lock_fd.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
