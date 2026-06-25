---
name: get-session-id
description: Resolve the active opencode chat's session ID from inside a running session. Use when the chat needs its own session ID — e.g. to name output files, look up its own DB row, tag artifacts with `session=<id>`, or pass `--session` back to a follow-up `opencode run`. Trigger on phrases like "my session id", "current session id", "session id of this chat", "tag this with my session id". Prints just the bare ID (e.g. `ses_xxx`) on stdout, or a JSON object with `--json`.
---

# get-session-id

A chat can ask for its own opencode session ID. There is no API call to do this from inside a session, so detection is best-effort and OS-dependent. The bundled helper script tries five strategies in order and returns the first hit.

## When to use

- Naming artifacts with the session ID (checkpoints, audit logs, output dirs).
- Looking up the current session's row in `~/.local/share/opencode/opencode.db`.
- Passing `--session <id>` to a follow-up `opencode run ...` invocation.
- Resuming / sharing / exporting the current chat.

## When NOT to use

- You only need a unique tag, not the real session ID — generate your own UUID instead.
- You want a *different* session's ID — query the DB directly with `sqlite3 ~/.local/share/opencode/opencode.db "SELECT id,title FROM session ORDER BY time_updated DESC"`.

## Usage

The script is at `~/.config/opencode/skills/get-session-id/scripts/get_session_id.py`. Run it with the `bash` tool:

```bash
# bare ID on stdout (suitable for $(...))
~/.config/opencode/skills/get-session-id/scripts/get_session_id.py

# or via python3
python3 ~/.config/opencode/skills/get-session-id/scripts/get_session_id.py
```

Capture the ID into a variable:

```bash
SID=$(~/.config/opencode/skills/get-session-id/scripts/get_session_id.py)
echo "session=$SID"
```

Or get richer context:

```bash
~/.config/opencode/skills/get-session-id/scripts/get_session_id.py --json
```

```json
{
  "cwd": "/home/zimmerer",
  "id": "ses_101f49f6effevpFstOIJ1Cx2jf",
  "opencode_pid": 1153824,
  "source": "opencode-db",
  "title": "Opencode skill to retrieve session ID",
  "updated": "2026-06-25T11:12:40"
}
```

Exit code is `0` on success, `1` on failure.

## Detection strategies (in order)

1. `$OPENCODE_SESSION_ID` env var — fastest, only set if a caller already knew.
2. Parent process tree — find the ancestor `opencode` process, read its `/proc/<pid>/cmdline` for `--session <id>` / `-s <id>`. Catches `opencode run --session <id>` and explicit attach invocations.
3. Same parent process, open file descriptors — `/proc/<pid>/fd/*` on Linux, `lsof -p <pid>` on macOS. Looks for a path matching `.../storage/message/<id>/...`. Catches long-running `opencode serve` daemons that hold the active session file open.
4. SQLite DB lookup — `~/.local/share/opencode/opencode.db` (or `$XDG_DATA_HOME/opencode/opencode.db`). Selects the most-recently-updated row in `session` whose `directory` equals the current working directory. Reliable for `opencode attach` TUIs and freshly-created sessions.
5. Recency fallback — most-recently-modified directory under `~/.local/share/opencode/storage/message/`. Last resort only; ignores cwd.

`--json` reports which strategy won via the `source` field.

## Limitations

- Strategy 4 requires `sqlite3` Python stdlib (always available). Strategy 2/3 require `/proc` (Linux) or `lsof` / `ps` (macOS, BSD). Strategy 5 needs read access to the storage directory.
- On a host with multiple opencode instances and overlapping cwds, strategy 4 may pick the wrong session if the cwd of a more-recently-updated unrelated session is a prefix of the current cwd. If that happens, look at `source` in `--json` output and pick strategy 2/3 instead, or query the DB explicitly by title.
- The script only inspects ancestors reachable from `os.getppid()`. If opencode spawned a deeply-nested subshell that itself spawned the agent, the walk still walks upward through `ppid` from `/proc/<pid>/status` until it finds an `opencode` comm — usually one or two hops.

## Bundled files

- `scripts/get_session_id.py` — the helper. Reads no external state besides `/proc`, `ps`, optional `lsof`, and the opencode DB. No network. Safe to run anywhere `python3` exists.
