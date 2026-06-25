---
name: schedule-wakeup
description: Schedule one-shot or recurring wakeups that re-invoke the current opencode chat with a stored prompt. Writes a JSON file under `~/.opencode/schedules/<session_id>/<wakeup_id>.json`; a cronjob backend reads those files and triggers the chat when `next_wakeup` has passed. Use when the chat needs to "remind me later", "check back in N minutes", "ping me every hour", "wake me up when X", or hand off work to a later session of itself. Trigger on phrases like "schedule a wakeup", "remind me", "ping me in", "check back", "re-invoke me".
---

# schedule-wakeup

Lets the chat schedule future invocations of itself. Each schedule is a JSON file the user-side cronjob reads and fires. The skill does NOT trigger wakeups itself — only writes the file.

## When to use

- "Remind me in 30 minutes to check the training logs."
- "Ping me every 10 minutes until the job is done."
- "Schedule a wakeup in 2 hours to run the next experiment step."
- Listing / inspecting / cancelling a previously-scheduled wakeup.

## When NOT to use

- The user wants a wakeup *right now* — just respond; no schedule needed.
- The work needs to happen in a *different* session — pass `--session <id>` explicitly.
- You're running inside a one-shot `opencode run` and want to chain commands — use shell `&&` or a sub-shell, not this skill.

## The five-second rule

```bash
SW=~/.config/opencode/skills/schedule-wakeup/scripts/schedule_wakeup.py

# schedule a one-shot wakeup in 30 minutes
$SW add --id check_logs --prompt "Check the training run; report seg_dice EMA" --minutes 30

# schedule a recurring ping every 15 minutes
$SW add --id poll_job --prompt "Is bjobs done?" --minutes 15 --recurring

# show all wakeups for this session
$SW list

# inspect one
$SW show --id check_logs

# cancel one
$SW remove --id check_logs

# cancel everything for this session
$SW remove --all
```

The script auto-detects the active session id (parent-process walk → SQLite DB lookup). Pass `--session <ses_xxx>` to schedule for a different session.

## Subcommands

| Subcommand | Purpose | Key flags |
| --- | --- | --- |
| `add` | Create or overwrite a schedule file | `--id`, `--prompt`, `--minutes`, `--recurring` |
| `list` | List schedules for the current (or `--session`) session | `--json` |
| `show` | Print one schedule as JSON | `--id` |
| `remove` | Delete one schedule (or `--all`) | `--id`, `--all` |

Run `$SW <subcommand> --help` for full flag docs.

## Inputs the agent must always provide

1. **`--id`** — short snake_case / kebab-case name. Sanitized to `[A-Za-z0-9_-]`; rejected if empty after sanitization. Re-using an id *overwrites* the existing schedule for that id.
2. **`--prompt`** — the literal instruction the cronjob will hand back to the chat on wake. Keep it self-contained: the chat has no other context when re-invoked, so spell out what to do, where to look, and what to report. Truncated at 8192 chars.
3. **`--minutes`** — positive integer ≤ 525600 (one year). For `--recurring`, the repeat interval; for one-shot, the delay from "now".
4. **`--recurring`** — flag, no value. Without it, the schedule is one-shot and the cronjob deletes the file after firing.

## File format the cronjob consumes

```json
{
  "session_id": "ses_xxxxxxxxxxxxxxxxxxxx",
  "wakeup_id":  "check_logs",
  "prompt":     "Check the training run; report seg_dice EMA",
  "type":       "once" | "interval",
  "minutes":    30,
  "next_wakeup": "2026-06-25T12:00:00",
  "created_at":  "2026-06-25T11:30:00"
}
```

- Path: `~/.opencode/schedules/<session_id>/<wakeup_id>.json` (override with `SCHEDULE_DIR`).
- `next_wakeup` is local-time ISO format without timezone (the example's format). The cronjob is responsible for interpreting it.
- `type: "once"` → cronjob deletes the file after firing.
- `type: "interval"` → cronjob advances `next_wakeup` to `now + minutes` and keeps the file.
- `created_at` is informational (added by the skill, not consumed by the example cronjob).
- Times use the user's local clock (`datetime.now()`). Do NOT pass `tzinfo` — keep the format naive to match the cronjob's parser.

## Detecting the session id

The script tries, in order:

1. `--session ses_xxx` (explicit override) or `$OPENCODE_SESSION_ID` env var.
2. Walk `os.getppid()` up to the first ancestor whose `comm` or cmdline contains `opencode`, then read its `--session` / `-s` flag, or fall back to its open `/storage/message/<id>/` file descriptor.
3. SQLite lookup of `~/.local/share/opencode/opencode.db` for the most-recently-updated session whose `directory` matches the current working directory.
4. Recency fallback: newest directory under `~/.local/share/opencode/storage/message/`.

This is the same logic as the `get-session-id` skill, kept inline so `schedule-wakeup` is self-sufficient. If detection returns `None`, the script errors out and asks for `--session`.

## Things to avoid

- **Don't write the schedule file directly with `edit` / `write`.** Always go through `schedule_wakeup.py add` so timestamps, sanitization, and validation happen. If you must hand-craft a file (e.g. fixing a corrupt schedule), copy the JSON shape above exactly.
- **Don't put secrets in `--prompt`.** Prompts land in plaintext JSON on disk under the user's home directory.
- **Don't reuse ids carelessly.** Adding the same `--id` overwrites the previous schedule — including any change from `--recurring` to a one-shot (or vice versa). Read the existing file first if unsure (`$SW show --id <id>`).
- **Don't schedule into the past.** The cronjob fires as soon as it sees an overdue `next_wakeup`, so `--minutes 0` is a "fire on next cron tick", not "fire never".

## Bundled files

- `scripts/schedule_wakeup.py` — stdlib-only Python 3 helper. Reads no network. Touches only `$SCHEDULE_DIR` and the opencode DB.
