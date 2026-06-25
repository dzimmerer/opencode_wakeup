#!/usr/bin/env python3
"""Schedule, list, inspect, and remove opencode chat wakeups.

Writes JSON files under ``$SCHEDULE_DIR/<session_id>/<wakeup_id>.json``. A
separate cron-driven backend reads those files and triggers the chat with the
embedded prompt when ``next_wakeup`` has passed. For ``type: "once"`` the
backend deletes the file after firing; for ``type: "interval"`` it advances
``next_wakeup`` to ``now + minutes`` and keeps the file.

Environment overrides:
  SCHEDULE_DIR      root directory for schedule files (default
                    ``~/.opencode/schedules``)
  OPENCODE_SESSION_ID  explicit session id, used when set (skips auto-detect)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import Optional, Tuple

SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9_-]{10,}$")
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
MAX_PROMPT_CHARS = 8192
MAX_MINUTES = 60 * 24 * 365  # 1 year
DEFAULT_SCHEDULE_DIR = os.path.expanduser("~/.opencode/schedules")


# ---------------------------------------------------------------------------
# session id detection (mirrors the get-session-id skill; kept inline so this
# skill is self-sufficient even if get-session-id is not installed)
# ---------------------------------------------------------------------------


def _default_data_dir() -> str:
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = xdg if xdg else os.path.expanduser("~/.local/share")
    return os.path.join(base, "opencode")


def _read_proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode("utf-8", errors="replace").replace("\x00", " ")
    except OSError:
        return ""


def _ps_field(pid: int, field: str) -> str:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out
    except Exception:
        return ""


def _walk_parents_for_opencode(start_pid: int) -> Optional[int]:
    seen: set[int] = set()
    pid = start_pid
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        cmdline = _read_proc_cmdline(pid).lower()
        comm = _ps_field(pid, "comm").lower()
        if "opencode" in comm or "opencode" in cmdline:
            return pid
        try:
            nxt = int(_ps_field(pid, "ppid") or 0)
        except ValueError:
            nxt = 0
        if nxt == pid or nxt <= 1:
            return None
        pid = nxt
    return None


def _find_session_via_cmdline(pid: int) -> Optional[str]:
    cmdline = _read_proc_cmdline(pid) or _ps_field(pid, "command")
    if not cmdline:
        return None
    tokens = cmdline.split()
    for i, tok in enumerate(tokens):
        if tok in ("--session", "-s") and i + 1 < len(tokens):
            cand = tokens[i + 1]
            if SESSION_ID_RE.match(cand):
                return cand
        if "=" in tok and tok.split("=", 1)[0] in ("--session", "-s"):
            cand = tok.split("=", 1)[1]
            if SESSION_ID_RE.match(cand):
                return cand
    return None


def _find_session_via_open_files(pid: int) -> Optional[str]:
    fd_dir = f"/proc/{pid}/fd"
    targets: list[str] = []
    if os.path.isdir(fd_dir):
        try:
            for name in os.listdir(fd_dir):
                try:
                    targets.append(os.readlink(os.path.join(fd_dir, name)))
                except OSError:
                    continue
        except OSError:
            pass
    if not targets:
        try:
            out = subprocess.check_output(
                ["lsof", "-p", str(pid), "-Fn"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            targets = [line[1:] for line in out.splitlines() if line.startswith("n/")]
        except Exception:
            return None
    for raw in targets:
        norm = raw.replace("\\", "/")
        m = re.search(r"/storage/message/(ses_[A-Za-z0-9_-]{10,})(?:/|$)", norm)
        if m:
            return m.group(1)
    return None


def _find_session_via_db(cwd: str) -> Optional[str]:
    import sqlite3
    db_path = os.path.join(_default_data_dir(), "opencode.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, directory FROM session ORDER BY time_updated DESC LIMIT 200"
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return None
    needle = cwd.rstrip("/")
    for sid, directory in rows:
        if not directory:
            continue
        d = directory.rstrip("/")
        if d == needle or needle.startswith(d + "/"):
            return sid if SESSION_ID_RE.match(sid or "") else None
    return None


def detect_session_id(cwd: Optional[str] = None) -> Optional[str]:
    env = os.environ.get("OPENCODE_SESSION_ID", "").strip()
    if SESSION_ID_RE.match(env):
        return env

    start_pid = os.getppid()
    pid = _walk_parents_for_opencode(start_pid)
    if pid:
        sid = _find_session_via_cmdline(pid)
        if sid:
            return sid
        sid = _find_session_via_open_files(pid)
        if sid:
            return sid

    sid = _find_session_via_db(cwd or os.getcwd())
    return sid


# ---------------------------------------------------------------------------
# schedule file io
# ---------------------------------------------------------------------------


def schedule_dir() -> str:
    return os.environ.get("SCHEDULE_DIR", DEFAULT_SCHEDULE_DIR)


def session_dir(session_id: str) -> str:
    return os.path.join(schedule_dir(), session_id)


def safe_id(raw: str) -> str:
    cleaned = SAFE_ID_RE.sub("", raw or "")
    return cleaned.strip("-_")


def validate_session_id(session_id: str) -> Tuple[bool, str]:
    if not session_id or not SESSION_ID_RE.match(session_id):
        return False, f"invalid session id: {session_id!r}"
    return True, ""


def schedule_path(session_id: str, wakeup_id: str) -> str:
    return os.path.join(session_dir(session_id), f"{wakeup_id}.json")


def cmd_add(args: argparse.Namespace) -> int:
    sid = args.session or detect_session_id()
    ok, err = validate_session_id(sid or "")
    if not ok:
        print(f"Error: {err}. Pass --session or install the get-session-id skill.", file=sys.stderr)
        return 2

    safe = safe_id(args.wakeup_id)
    if not safe:
        print("Error: --id must contain at least one [A-Za-z0-9_-] character.", file=sys.stderr)
        return 2

    try:
        minutes = int(args.minutes)
    except (TypeError, ValueError):
        print(f"Error: --minutes must be an integer (got {args.minutes!r}).", file=sys.stderr)
        return 2
    if minutes <= 0 or minutes > MAX_MINUTES:
        print(f"Error: --minutes must be between 1 and {MAX_MINUTES}.", file=sys.stderr)
        return 2

    prompt = args.prompt
    if prompt is None or not prompt.strip():
        print("Error: --prompt must be non-empty.", file=sys.stderr)
        return 2
    if len(prompt) > MAX_PROMPT_CHARS:
        print(f"Warning: prompt truncated from {len(prompt)} to {MAX_PROMPT_CHARS} chars.", file=sys.stderr)
        prompt = prompt[:MAX_PROMPT_CHARS]

    now = dt.datetime.now()
    next_wakeup = now + dt.timedelta(minutes=minutes)
    payload = {
        "session_id": sid,
        "wakeup_id": safe,
        "prompt": prompt,
        "type": "interval" if args.recurring else "once",
        "minutes": minutes,
        "next_wakeup": next_wakeup.isoformat(),
        "created_at": now.isoformat(),
    }

    sd = session_dir(sid)
    os.makedirs(sd, exist_ok=True)
    path = schedule_path(sid, safe)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    kind = "recurring every" if args.recurring else "one-shot in"
    print(
        f"Scheduled {safe!r} ({kind} {minutes} min) for session {sid} "
        f"-> next run {next_wakeup.strftime('%Y-%m-%d %H:%M:%S')} ({path})"
    )
    return 0


def _read_one(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _resolve_session(args: argparse.Namespace) -> Optional[str]:
    if args.session:
        return args.session
    env = os.environ.get("OPENCODE_SESSION_ID", "").strip()
    if SESSION_ID_RE.match(env):
        return env
    return detect_session_id()


def cmd_list(args: argparse.Namespace) -> int:
    sid = _resolve_session(args)
    if not sid:
        print("Error: cannot determine session id (pass --session).", file=sys.stderr)
        return 2
    sd = session_dir(sid)
    if not os.path.isdir(sd):
        print(f"No scheduled wakeups for session {sid}.")
        return 0
    rows: list[Tuple[str, dict]] = []
    for name in sorted(os.listdir(sd)):
        if not name.endswith(".json"):
            continue
        data = _read_one(os.path.join(sd, name))
        if data is None:
            continue
        rows.append((name[:-5], data))
    if not rows:
        print(f"No scheduled wakeups for session {sid}.")
        return 0
    if args.json:
        print(json.dumps({sid: [d for _, d in rows]}, indent=2, ensure_ascii=False))
        return 0
    print(f"Active schedules for session {sid} ({len(rows)}):")
    now = dt.datetime.now()
    for wid, d in rows:
        nxt = d.get("next_wakeup", "?")
        try:
            nxt_dt = dt.datetime.fromisoformat(nxt)
            delta = nxt_dt - now
            rel = f"in {int(delta.total_seconds() // 60)}m" if delta.total_seconds() > 0 else "OVERDUE"
        except ValueError:
            rel = "?"
        print(
            f"  - {wid:24s}  type={d.get('type','?'):8s}  every={d.get('minutes','?')}m  "
            f"next={nxt} ({rel})"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    sid = _resolve_session(args)
    if not sid:
        print("Error: cannot determine session id (pass --session).", file=sys.stderr)
        return 2
    wid = safe_id(args.wakeup_id)
    if not wid:
        print("Error: --id required.", file=sys.stderr)
        return 2
    path = schedule_path(sid, wid)
    data = _read_one(path)
    if data is None:
        print(f"Error: wakeup {wid!r} not found for session {sid}.", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    sid = _resolve_session(args)
    if not sid:
        print("Error: cannot determine session id (pass --session).", file=sys.stderr)
        return 2
    wid = safe_id(args.wakeup_id)
    if not wid and not args.all:
        print("Error: --id required (or pass --all to purge the session).", file=sys.stderr)
        return 2

    sd = session_dir(sid)
    if not os.path.isdir(sd):
        print(f"No schedules for session {sid}.")
        return 0

    removed = 0
    if args.all:
        for name in os.listdir(sd):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(sd, name))
                    removed += 1
                except OSError as e:
                    print(f"Warning: could not remove {name}: {e}", file=sys.stderr)
        try:
            os.rmdir(sd)
        except OSError:
            pass
        print(f"Removed {removed} schedule(s) for session {sid}.")
        return 0

    path = schedule_path(sid, wid)
    if os.path.exists(path):
        os.remove(path)
        print(f"Removed wakeup {wid!r} for session {sid}.")
        try:
            if not os.listdir(sd):
                os.rmdir(sd)
        except OSError:
            pass
        return 0
    print(f"Error: wakeup {wid!r} not found for session {sid}.", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    SESSION_HELP = "explicit session id (else auto-detect)"
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _add_session(p: argparse.ArgumentParser) -> None:
        # Repeated on each subparser so it can appear before OR after the subcommand
        p.add_argument("--session", default=None, help=SESSION_HELP)

    p_add = sub.add_parser("add", help="schedule a wakeup")
    _add_session(p_add)
    p_add.add_argument("--id", dest="wakeup_id", required=True, help="short wakeup id, e.g. check_logs")
    p_add.add_argument("--prompt", required=True, help="instruction sent to the chat on wake")
    p_add.add_argument("--minutes", required=True, help="delay (one-shot) or interval (recurring) in minutes")
    p_add.add_argument("--recurring", action="store_true", help="repeat every N minutes instead of one-shot")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list active wakeups for this session")
    _add_session(p_list)
    p_list.add_argument("--json", action="store_true", help="emit JSON")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show one wakeup as JSON")
    _add_session(p_show)
    p_show.add_argument("--id", dest="wakeup_id", required=True)
    p_show.set_defaults(func=cmd_show)

    p_rm = sub.add_parser("remove", help="remove one wakeup (or --all)")
    _add_session(p_rm)
    p_rm.add_argument("--id", dest="wakeup_id", default="")
    p_rm.add_argument("--all", action="store_true", help="remove every wakeup for the session")
    p_rm.set_defaults(func=cmd_remove)

    return ap


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
