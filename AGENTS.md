# Self-instrumentation: session id & scheduled wakeups

Two globally-installed skills live under `~/.config/opencode/skills/`. Use them
proactively — they exist so this chat can identify itself and re-invoke itself
later. Both are stdlib-only Python, no network, no side effects beyond what is
described below.

## `get-session-id` — print this chat's session id

```bash
SID=$(~/.config/opencode/skills/get-session-id/scripts/get_session_id.py)
```

Use it whenever you need to identify the running chat:

- Tag output files, checkpoints, audit logs, W&B run names with the session id.
- Look up your own row in `~/.local/share/opencode/opencode.db`.
- Pass `--session $SID` to a follow-up `opencode run ...`.
- Embed `<id>` in commit messages, worktree names, or experiment folders.

Add `--json` for richer output (`source`, `title`, `updated`, `opencode_pid`).
The script auto-detects the active session id (parent process → SQLite DB);
do not ask the user for it.

## `schedule-wakeup` — schedule a future re-invocation of yourself

```bash
SW=~/.config/opencode/skills/schedule-wakeup/scripts/schedule_wakeup.py

# one-shot reminder in N minutes
$SW add --id <short_id> --prompt "<what to do on wake>" --minutes <N>

# recurring poll every N minutes
$SW add --id <short_id> --prompt "<what to check>" --minutes <N> --recurring

# inspect / cancel
$SW list
$SW show --id <short_id>
$SW remove --id <short_id>
$SW remove --all
```

### When to reach for it

Reach for `schedule-wakeup` proactively — do not wait for the user to ask:

- You just kicked off a long-running job (`bsub`, training run, build, large
  download) and want to come back to check it. Schedule a one-shot wakeup
  sized to the job's expected walltime (or slightly less).
- You need to poll an external state on an interval — `bjobs`, a CI run, a
  logfile's tail, a queue's depth — schedule a recurring wakeup.
- The user said "remind me in N minutes", "ping me when X", "check back in N",
  "let me know when …", or any variant of "wake me up / notify me / come back
  to this".
- You want to hand a follow-up task off to a later session of yourself with a
  self-contained prompt (e.g. "in 2h, run the eval suite and report numbers").

### Prompt hygiene

`--prompt` must be **self-contained**: when the cronjob fires and this chat is
re-invoked, the message is the entire context. Spell out:

- What to check (file path, command, URL, query).
- Where to look (absolute paths, project root).
- What to report (which metrics, what format).
- What "done" looks like, so the wakeup can decide to clean up after itself.

Bad: `"check the run"`. Good: `"cat
~/project/outputs/.../run_000003/train.log | tail -80;
report last seg_dice EMA, last I→T R@5, last T→I R@5, and whether the run is
still RUNNING via bjobs; if FINISHED, remove this schedule."`.

### Id hygiene

- Pick a short, stable `--id` per task (`train_03_loss`, `poll_bjobs`,
  `check_pr_142`). Re-using an id overwrites the previous schedule.
- Use `--recurring` only when you want the cronjob to advance `next_wakeup`
  after every fire. Default (one-shot) deletes the file after the first fire.

### Cleanup — non-negotiable

**Always remove schedules when the work is done, the job is finished, or the
task is cancelled.** Stale `--recurring` schedules re-fire forever; stale
one-shots re-fire on every cron tick after their `next_wakeup` passes.

- After the run / poll / wait completes successfully → `$SW remove --id <id>`.
- On user cancellation or branch switch → `$SW remove --all`.
- At the end of a task that scheduled any wakeups → `$SW list` then clean up.

A schedule that outlives its purpose is a leak.

## How the two fit together

- Both skills auto-detect the active session id — never ask the user.
- `schedule-wakeup` writes JSON to
  `~/.opencode/schedules/<session_id>/<wakeup_id>.json`. The user's cronjob
  backend reads those files and re-invokes the chat with the stored prompt.
  **The skill does not fire wakeups itself** — it only writes the file.
- File format the cronjob consumes:
  ```json
  {
    "session_id":   "ses_xxx",
    "wakeup_id":    "check_logs",
    "prompt":       "...",
    "type":         "once" | "interval",
    "minutes":      30,
    "next_wakeup":  "2026-06-25T12:00:00"
  }
  ```
- Override the schedules root via `$SCHEDULE_DIR` if you ever need to
  relocate it.
