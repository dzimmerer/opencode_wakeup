#!/usr/bin/env python3
"""Scan ~/.opencode/schedules/ for due wakeups and re-invoke opencode.

Invoked once per minute (by launchd on macOS, by cron on Linux). The parent
tick peeks at each schedule; for each one whose `next_wakeup` has passed it
spawns a dedicated worker process (multiprocessing, spawn context) that
runs ``opencode run -s <session_id> -- <prompt>``, captures stdout/stderr,
and consumes or re-arms the schedule. The parent tick is non-blocking and
exits as soon as workers are dispatched, so a slow agent turn (up to
``RUNNER_TIMEOUT`` seconds) cannot stall the poll loop and multiple due
wakeups fire in parallel.

One-shot schedules are deleted after a successful run; interval schedules
get their ``next_wakeup`` advanced by ``minutes``. Failed/timeout runs
re-arm ``next_wakeup`` to ``now + RETRY_MINUTES``; after ``MAX_ATTEMPTS``
failures the schedule is moved to ``<id>.json.bad``.

Environment overrides:
  SCHEDULE_DIR   root directory for schedule files
                 (default ~/.opencode/schedules)
   OPENCODE_BIN   absolute path to the opencode binary
                  (default: shutil.which("opencode"),
                  then /opt/homebrew/bin/opencode on macOS,
                  lastly ~/.opencode/bin/opencode)
  RUNNER_LOG     path to append log output to
                 (default ~/.opencode/runner.log)
  RUNNER_LOCK    path to the run lock file
                 (default ~/.opencode/runner.lock)
  RUNNER_TIMEOUT      per-wakeup timeout for the spawned `opencode run`
  RUNNER_RETRY_MINUTES  re-arm interval after failure
  RUNNER_MAX_ATTEMPTS   cap before moving schedule to .bad
  RUNNER_STDOUT_TAIL / RUNNER_STDERR_TAIL  how much of each stream to log
  RUNNER_CLAIM_TIMEOUT  seconds before a stale dispatch marker is reaped
                        (default 3600 = 1h; for crashed workers)
"""
from __future__ import annotations

import fcntl
import json
import multiprocessing
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

RUNNER_TIMEOUT = int(os.environ.get("RUNNER_TIMEOUT", "300"))
RETRY_MINUTES = int(os.environ.get("RUNNER_RETRY_MINUTES", "5"))
MAX_ATTEMPTS = int(os.environ.get("RUNNER_MAX_ATTEMPTS", "5"))
STDOUT_TAIL_CHARS = int(os.environ.get("RUNNER_STDOUT_TAIL", "4000"))
STDERR_TAIL_CHARS = int(os.environ.get("RUNNER_STDERR_TAIL", "4000"))
CLAIM_TIMEOUT = int(os.environ.get("RUNNER_CLAIM_TIMEOUT", "3600"))

_MP_CTX = multiprocessing.get_context("spawn")


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


def _tail_text(s: str, max_chars: int) -> str:
    if not s:
        return ""
    if len(s) <= max_chars:
        return s.rstrip()
    return "...[truncated]...\n" + s[-max_chars:].rstrip()


def _consume_or_rearm(
    file_path: Path,
    data: dict,
    now: datetime,
    kind: str,
    minutes: int,
    succeeded: bool,
) -> None:
    """Post-firing bookkeeping.

    On success: delete one-shot, advance interval (as before).
    On failure: re-arm `next_wakeup` to now+RETRY_MINUTES, increment an
    `attempts` counter in the JSON, and after MAX_ATTEMPTS move the file
    aside as `.bad` so a permanently-broken wakeup can't tight-loop the
    runner. Interval schedules keep retrying the same way (re-arm, not
    advance by `minutes`, until the run succeeds).
    """
    if succeeded:
        if kind == "interval":
            data["next_wakeup"] = (now + timedelta(minutes=minutes)).isoformat()
            data.pop("attempts", None)
            data.pop("last_error", None)
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
        return

    attempts = int(data.get("attempts", 0)) + 1
    data["attempts"] = attempts
    data["last_error"] = f"opencode run failed (attempt {attempts})"

    if attempts >= MAX_ATTEMPTS:
        _move_aside(file_path, f"opencode run failed {attempts} times; giving up")
        return

    data["next_wakeup"] = (now + timedelta(minutes=RETRY_MINUTES)).isoformat()
    try:
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError as e:
        _log(f"could not re-arm {file_path}: {e}")


def _worker_entry(schedule_path_str: str, claim_path_str: str) -> None:
    try:
        _process(Path(schedule_path_str), datetime.now())
    except BaseException as e:
        _log(f"worker crashed on {schedule_path_str}: {type(e).__name__}: {e}")
    finally:
        try:
            Path(claim_path_str).unlink()
        except OSError:
            pass


def _dispatch_due(file_path: Path, now: datetime) -> None:
    """Peek at next_wakeup; if due, claim the schedule (atomic O_EXCL
    create of `<id>.json.dispatch`) and spawn a dedicated worker process
    that runs `_process` (load, invoke `opencode run`, log, consume/rearm).

    The dispatch marker is the per-schedule single-flight guard: if it
    already exists, another worker is in flight and this dispatcher
    skips. If the marker is older than CLAIM_TIMEOUT, the previous
    worker is assumed to have crashed and the marker is reaped before
    re-claiming.

    The parent tick is non-blocking: it does not wait for the spawned
    worker. Each wakeup runs in its own OS process (multiprocessing
    spawn context) so a slow agent turn (up to RUNNER_TIMEOUT) cannot
    stall the 60s poll loop, and multiple due wakeups fire in parallel."""
    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _move_aside(file_path, f"unreadable JSON: {e}")
        return
    try:
        next_wakeup = datetime.fromisoformat(data["next_wakeup"])
    except (KeyError, TypeError, ValueError) as e:
        _move_aside(file_path, f"missing/invalid field: {e}")
        return
    if now < next_wakeup:
        return

    claim_path = file_path.with_suffix(file_path.suffix + ".dispatch")
    if claim_path.exists():
        try:
            claim_age = now.timestamp() - claim_path.stat().st_mtime
        except OSError:
            claim_age = 0
        if claim_age < CLAIM_TIMEOUT:
            _log(
                f"skip {file_path.name}: another worker in flight "
                f"(dispatch marker {claim_age:.0f}s old)"
            )
            return
        _log(
            f"stale dispatch marker for {file_path.name} "
            f"({claim_age:.0f}s old, threshold {CLAIM_TIMEOUT}s); removing"
        )
        try:
            claim_path.unlink()
        except OSError as e:
            _log(f"could not remove stale marker {claim_path}: {e}; skipping")
            return

    try:
        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        _log(f"race: another dispatcher claimed {file_path.name} just now; skipping")
        return

    proc = _MP_CTX.Process(
        target=_worker_entry,
        args=(str(file_path), str(claim_path)),
        name=f"opencode-wakeup-{file_path.stem}",
        daemon=False,
    )
    proc.start()
    _log(f"dispatched worker pid {proc.pid} for {file_path.name}")


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

    cmd = [OPENCODE_BIN, "run", "-s", session_id, "--", prompt]
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            env={**os.environ, "OPENCODE_YOLO": "true"},
            capture_output=True,
            text=True,
            check=False,
            timeout=RUNNER_TIMEOUT,
        )
    except FileNotFoundError:
        _log(f"FATAL: opencode binary not found at {OPENCODE_BIN}; check OPENCODE_BIN / PATH")
        return
    except subprocess.TimeoutExpired as e:
        _log(
            f"opencode run timed out after {RUNNER_TIMEOUT}s for wakeup {wakeup_id!r}; "
            f"will retry in {RETRY_MINUTES}min"
        )
        if e.stdout:
            _log(f"  stdout (tail):\n{_tail_text(e.stdout, STDOUT_TAIL_CHARS)}")
        if e.stderr:
            _log(f"  stderr (tail):\n{_tail_text(e.stderr, STDERR_TAIL_CHARS)}")
        _consume_or_rearm(file_path, data, now, kind, minutes, succeeded=False)
        return
    except OSError as e:
        _log(f"opencode launch failed for wakeup {wakeup_id!r}: {e}")
        _consume_or_rearm(file_path, data, now, kind, minutes, succeeded=False)
        return

    if result.returncode == 0:
        _log(
            f"opencode run ok for wakeup {wakeup_id!r} (rc=0, "
            f"{len(result.stdout or '')}B stdout, {len(result.stderr or '')}B stderr)"
        )
        if result.stdout:
            _log(f"  stdout (tail):\n{_tail_text(result.stdout, STDOUT_TAIL_CHARS)}")
        _consume_or_rearm(file_path, data, now, kind, minutes, succeeded=True)
    else:
        _log(
            f"opencode run failed for wakeup {wakeup_id!r} (rc={result.returncode}); "
            f"will retry in {RETRY_MINUTES}min"
        )
        if result.stdout:
            _log(f"  stdout (tail):\n{_tail_text(result.stdout, STDOUT_TAIL_CHARS)}")
        if result.stderr:
            _log(f"  stderr (tail):\n{_tail_text(result.stderr, STDERR_TAIL_CHARS)}")
        _consume_or_rearm(file_path, data, now, kind, minutes, succeeded=False)


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
            _dispatch_due(json_path, now)
        _cleanup_empty_dirs()
    finally:
        lock_fd.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
