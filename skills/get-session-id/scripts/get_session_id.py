#!/usr/bin/env python3
"""Print the active opencode chat's session ID.

Detection order (first hit wins):

  1. Environment variable $OPENCODE_SESSION_ID (set by callers that know it).
  2. Walk the parent process tree to find an `opencode` process, then read its
     command line for `--session <id>` / `-s <id>`. Works for `opencode run`
     invocations and for attached sessions that pass the ID explicitly.
  3. Same parent process, but read its open file descriptors (Linux /proc or
     `lsof`) for the storage path `.../storage/message/<id>/...`. Catches
     long-running `opencode serve` daemons holding the active session.
  4. Query the opencode SQLite DB (`$XDG_DATA_HOME/opencode/opencode.db`,
     default `~/.local/share/opencode/opencode.db`) for the most-recently
     updated row in `session` whose `directory` matches the current working
     directory. Reliable for TUI chats (opencode attach) and for sessions that
     were just created.
  5. Recency fallback: most-recently-modified directory under
     `.../storage/message/`. Use only as a last resort because it ignores the
     cwd match.

Outputs the bare session ID (no decoration, no trailing newline is OK) on
stdout, and exits 0. On failure, prints a short error to stderr and exits 1.

Usage:

    get_session_id.py                 # default: stdout = session id
    get_session_id.py --json          # JSON object with id, source, extras
    get_session_id.py --cwd DIR       # override cwd for strategy 4/5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from typing import Optional, Tuple

SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9_-]{10,}$")


def _default_data_dir() -> str:
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = xdg if xdg else os.path.expanduser("~/.local/share")
    return os.path.join(base, "opencode")


def _default_storage_dir() -> str:
    return os.path.join(_default_data_dir(), "storage", "message")


def _default_db_path() -> str:
    return os.path.join(_default_data_dir(), "opencode.db")


def _looks_like_session_id(s: str) -> bool:
    return bool(s) and bool(SESSION_ID_RE.match(s))


def from_env() -> Optional[str]:
    v = os.environ.get("OPENCODE_SESSION_ID", "").strip()
    return v if _looks_like_session_id(v) else None


def _read_proc_cmdline(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
        return data.replace("\x00", " ").strip()
    except OSError:
        return None


def _ps_field(pid: int, field: str) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out or None
    except Exception:
        return None


def _walk_parents_for_opencode(start_pid: int) -> Optional[int]:
    """Return the first ancestor PID whose comm contains 'opencode'."""
    seen = set()
    pid = start_pid
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        cmdline = _read_proc_cmdline(pid) or ""
        comm = (_ps_field(pid, "comm") or "").lower()
        if "opencode" in comm or "opencode" in cmdline.lower():
            return pid
        ppid = _ps_field(pid, "ppid")
        try:
            nxt = int(ppid) if ppid else 0
        except ValueError:
            nxt = 0
        if nxt == pid or nxt <= 1:
            return None
        pid = nxt
    return None


def from_cmdline(pid: int) -> Optional[str]:
    """Look for --session <id> or -s <id> on the opencode process cmdline."""
    cmdline = _read_proc_cmdline(pid)
    if not cmdline:
        # Fall back to `ps` (works on macOS where /proc is absent).
        cmdline = _ps_field(pid, "command") or ""
    if not cmdline:
        return None
    tokens = cmdline.split()
    for i, tok in enumerate(tokens):
        if tok in ("--session", "-s") and i + 1 < len(tokens):
            cand = tokens[i + 1]
            if _looks_like_session_id(cand):
                return cand
        if "=" in tok and tok.split("=", 1)[0] in ("--session", "-s"):
            cand = tok.split("=", 1)[1]
            if _looks_like_session_id(cand):
                return cand
    return None


def _read_open_files_linux(pid: int) -> list[str]:
    """Return open file targets via /proc/<pid>/fd/*."""
    fd_dir = f"/proc/{pid}/fd"
    if not os.path.isdir(fd_dir):
        return []
    out: list[str] = []
    try:
        for name in os.listdir(fd_dir):
            try:
                tgt = os.readlink(os.path.join(fd_dir, name))
            except OSError:
                continue
            if tgt.startswith("/") or tgt.startswith("./") or tgt.startswith("../"):
                out.append(tgt)
    except OSError:
        return []
    return out


def _read_open_files_lsof(pid: int) -> list[str]:
    try:
        out = subprocess.check_output(
            ["lsof", "-p", str(pid), "-Fn", "-Fn"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    paths: list[str] = []
    for line in out.splitlines():
        if line.startswith("n/") or line.startswith("n."):
            paths.append(line[1:])
    return paths


def from_open_files(pid: int) -> Optional[str]:
    for raw in _read_open_files_linux(pid) + _read_open_files_lsof(pid):
        norm = raw.replace("\\", "/")
        m = re.search(r"/storage/message/(ses_[A-Za-z0-9_-]{10,})(?:/|$)", norm)
        if m:
            return m.group(1)
    return None


def from_db(cwd: str, db_path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (session_id, title, updated_iso) for the most-recent session
    whose directory equals cwd or is a prefix of cwd."""
    if not os.path.exists(db_path):
        return (None, None, None)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, directory, title, time_updated "
            "FROM session ORDER BY time_updated DESC LIMIT 200"
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return (None, None, None)
    needle = cwd.rstrip("/")
    for sid, directory, title, tu in rows:
        if not directory:
            continue
        d = directory.rstrip("/")
        if d == needle or needle.startswith(d + "/"):
            ts = (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(tu / 1000.0))
                if tu
                else None
            )
            return (sid if _looks_like_session_id(sid) else None, title, ts)
    return (None, None, None)


def from_recent_storage(storage_dir: str, limit: int = 25) -> Tuple[Optional[str], float]:
    """Most-recently-modified session directory, with mtime."""
    if not os.path.isdir(storage_dir):
        return (None, 0.0)
    candidates: list[Tuple[float, str]] = []
    try:
        for name in os.listdir(storage_dir):
            p = os.path.join(storage_dir, name)
            if not (os.path.isdir(p) and _looks_like_session_id(name)):
                continue
            try:
                mtime = max(
                    os.path.getmtime(p),
                    os.path.getctime(p),
                )
            except OSError:
                continue
            candidates.append((mtime, name))
    except OSError:
        return (None, 0.0)
    if not candidates:
        return (None, 0.0)
    candidates.sort(reverse=True)
    return (candidates[0][1], candidates[0][0])


def detect(cwd: Optional[str] = None) -> dict:
    cwd = cwd or os.getcwd()
    info: dict = {"cwd": cwd}

    sid = from_env()
    if sid:
        info["id"] = sid
        info["source"] = "env:OPENCODE_SESSION_ID"
        return info

    opencode_pid = None
    parent_chain: list[int] = []
    try:
        parent_chain.append(os.getpid())
        parent_chain.append(os.getppid())
    except OSError:
        pass

    start = parent_chain[-1] if parent_chain else os.getppid()
    opencode_pid = _walk_parents_for_opencode(start)
    info["opencode_pid"] = opencode_pid

    if opencode_pid:
        sid = from_cmdline(opencode_pid)
        if sid:
            info["id"] = sid
            info["source"] = "opencode-cmdline"
            return info
        sid = from_open_files(opencode_pid)
        if sid:
            info["id"] = sid
            info["source"] = "opencode-open-files"
            return info

    sid, title, ts = from_db(cwd, _default_db_path())
    if sid:
        info["id"] = sid
        info["source"] = "opencode-db"
        info["title"] = title
        info["updated"] = ts
        return info

    sid, mtime = from_recent_storage(_default_storage_dir())
    if sid:
        info["id"] = sid
        info["source"] = "recent-storage"
        info["mtime"] = mtime
        return info

    return info


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--json", action="store_true", help="emit JSON object")
    ap.add_argument("--cwd", default=None, help="override cwd for db lookup")
    ap.add_argument("--storage-dir", default=None, help="override storage dir")
    args = ap.parse_args(argv)

    info = detect(cwd=args.cwd)
    sid = info.get("id")

    if args.json:
        print(json.dumps(info, indent=2, sort_keys=True))
    else:
        if sid:
            print(sid)
        else:
            print("Error: could not determine opencode session id.", file=sys.stderr)
            for k, v in info.items():
                print(f"  {k}: {v}", file=sys.stderr)
    return 0 if sid else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
