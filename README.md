# opencode-wakeup

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Self-instrumentation for opencode chats** — session ID detection and scheduled wakeups that re-invoke your chat with a stored prompt.

Two opencode skills plus a cron-driven runner. The skills let any chat identify itself and schedule future re-invocations; the runner fires those schedules by calling `opencode run` with the stored prompt.

---

## Features

- **Get your own session ID** from inside any chat — no need to ask the user.
- **Schedule one-shot wakeups** ("check back in 30 minutes when the training run finishes").
- **Schedule recurring wakeups** ("poll `bjobs` every 5 minutes until the job is done").
- **Inspect / cancel** scheduled wakeups at any time.
- **Cron/launchd runner** fires due wakeups automatically (one-shot → delete, interval → advance + keep). Linux uses cron; macOS uses launchd. The 60-second tick is non-blocking: each due wakeup is dispatched to a dedicated worker process (multiprocessing, spawn context), so a slow `opencode run` cannot stall the poll loop and multiple wakeups can run in parallel. Per-schedule single-flight is enforced by an atomic dispatch marker (`<id>.json.dispatch`). Failed runs re-arm to `now + RUNNER_RETRY_MINUTES` and retry up to `RUNNER_MAX_ATTEMPTS` times before the schedule is moved aside as `.bad`.
- **Safe from shell injection** — prompt content is passed as positional arguments via subprocess list-form, never via shell string interpolation.
- **No network dependencies** — all scripts are stdlib-only Python 3.

---

## Architecture

```
┌──────────────────────────────┐
│         opencode chat         │
│  (get-session-id skill)       │
│  (schedule-wakeup skill)      │
└────────┬──────────┬───────────┘
         │          │
         ▼          ▼
  ┌──────────┐  ┌───────────────────┐
  │ session  │  │  schedule JSON    │
  │ ID on    │  │  ~/.opencode/     │
  │ stdout   │  │  schedules/<sid>/ │
  └──────────┘  └────────┬──────────┘
                         │
                         ▼
                   ┌──────────────┐
                   │  cron/launchd │  ← runs every 60s
                   │  runner.py    │
                   └───────┬──────┘
                          │
                          ▼
                  ┌────────────────┐
                  │  opencode run  │
                  │  -s <sid> --   │
                  │  "<prompt>"    │
                  └────────────────┘
```

1. The chat (via the `schedule-wakeup` skill) writes a JSON file to `~/.opencode/schedules/<session_id>/<wakeup_id>.json` with the target time, prompt, and type.
2. The runner (`runner.py`) polls these files every 60 seconds (via cron on Linux or launchd on macOS). When `next_wakeup` has passed, it peeks at the file, atomically claims the schedule by creating `<id>.json.dispatch`, and spawns a dedicated worker process (multiprocessing spawn context) that invokes `opencode run -s <session_id> -- "<prompt>"`, waits up to `RUNNER_TIMEOUT` seconds, captures stdout/stderr to the log, and removes the dispatch marker. The parent tick is non-blocking and exits as soon as workers are dispatched. If another tick tries to dispatch the same schedule while a worker is in flight, it sees the marker and skips. On non-zero exit or timeout the schedule is re-armed (not dropped) and retried on a later tick.
3. One-shot files are deleted after firing; interval files get their `next_wakeup` advanced by `minutes` and stay in place.

---

## Repository structure

```
opencode_wakeup/
├── README.md                          ← this file
├── LICENSE
├── .gitignore
├── install.sh                         ← idempotent installer
├── AGENTS.md                          ← global instructions injected into every chat's system prompt
├── skills/
│   ├── get-session-id/
│   │   ├── SKILL.md                   ← skill metadata & usage docs
│   │   └── scripts/
│   │       └── get_session_id.py      ← prints the active chat's session ID
│   └── schedule-wakeup/
│       ├── SKILL.md                   ← skill metadata & usage docs
│       └── scripts/
│           └── schedule_wakeup.py     ← CLI to add/list/show/remove wakeups
├── runner/
│   └── runner.py                      ← tick dispatcher + per-wakeup worker processes (multiprocessing spawn)
└── config/
    ├── opencode.json                  ← snippet to merge into global opencode.json
    ├── crontab.txt                    ← crontab entry template (Linux)
    └── launchd.plist.txt              ← launchd plist template (macOS)
```

---

## Prerequisites

- **opencode** ≥ 1.17 (tested on 1.17.9)
- **Python 3** ≥ 3.8 (stdlib only)
- **Linux:** cron (typically pre-installed), `/proc` filesystem (for session ID detection)
- **macOS:** launchd (built-in), `lsof` (pre-installed) for session ID fallback

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/<your>/opencode-wakeup.git
cd opencode-wakeup

# 2. Install skills, AGENTS.md, and runner
./install.sh

# 3. Add "instructions" to your global opencode.json
#    (the installer tells you how — follow the prompt)

# 4. Install the scheduler (optional — the runner needs it for
#    automatic wakeup firing)
#    Linux:
./install.sh --crontab
#    macOS:
./install.sh --launchd

# 5. Quit opencode and restart it for the config change to take effect.
```

After restart, open a new chat and try:

```bash
# Get your session ID
SID=$(~/.config/opencode/skills/get-session-id/scripts/get_session_id.py)
echo "session=$SID"

# Schedule a one-shot wakeup in 2 minutes
SW=~/.config/opencode/skills/schedule-wakeup/scripts/schedule_wakeup.py
$SW add --id smoke_test --prompt "echo hello from the wakeup" --minutes 2

# Inspect
$SW list
```

If you have the cron runner installed, it will fire the wakeup automatically after 2 minutes and you'll see the result in `~/.opencode/runner.log`.

---

## Detailed setup

### 1. Install skills

The two skills (`get-session-id` and `schedule-wakeup`) must be placed under `~/.config/opencode/skills/` so opencode auto-loads them. The installer does this:

```bash
./install.sh
```

This copies:
- `skills/get-session-id/` → `~/.config/opencode/skills/get-session-id/`
- `skills/schedule-wakeup/` → `~/.config/opencode/skills/schedule-wakeup/`
- `AGENTS.md` → `~/.config/opencode/AGENTS.md`
- `runner/runner.py` → `~/.opencode/runner.py`

### 2. Configure opencode

The global `AGENTS.md` file (which tells the agent about these skills) must be referenced in your `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["~/.config/opencode/AGENTS.md"]
}
```

Merge this with your existing global config. The `instructions` field adds the file into every chat's system prompt, so agents proactively use the skills without being reminded.

**After editing, quit and restart opencode.** Config is loaded only at startup.

### 3. Install the runner scheduler

The runner (`runner.py`) scans `~/.opencode/schedules/` every 60 seconds for due wakeups. Install it with:

**Linux (cron):**
```bash
./install.sh --crontab
```

Or manually (`crontab -e`):
```
PATH=/home/<user>/.opencode/bin:/home/<user>/.local/bin:/usr/bin:/bin
* * * * * /usr/bin/python3 /home/<user>/.opencode/runner.py >> /home/<user>/.opencode/runner.log 2>&1
```

The `PATH=` is critical: cron's default `PATH` is `/usr/bin:/bin`, but the opencode binary lives at `~/.opencode/bin/opencode`. Without the override, the runner can't find opencode.

**macOS (launchd):**
```bash
./install.sh --launchd
```

Or manually place the matching plist at `~/Library/LaunchAgents/com.opencode.wakeup.runner.plist` (see `config/launchd.plist.txt`), then:
```bash
launchctl load ~/Library/LaunchAgents/com.opencode.wakeup.runner.plist
```

The plist sets `PATH` and runs the runner every 60 seconds via `StartInterval`. Check status with `launchctl list | grep opencode`.

> **Note:** On macOS, `cron` is deprecated. This project uses `launchd` instead, which is the native Apple-approved job scheduler. The plist template includes the necessary `PATH` and environment overrides.

### 4. Verify

```bash
# Skills loaded?
ls ~/.config/opencode/skills/
# → get-session-id  schedule-wakeup

# AGENTS.md loaded?
grep instructions ~/.config/opencode/opencode.json
# → "instructions": ["~/.config/opencode/AGENTS.md"]

# Runner present?
python3 ~/.opencode/runner.py && head -3 ~/.opencode/runner.log
# → no error, log may show "triggering wakeup"

# Crontab installed?
crontab -l | grep runner
# → * * * * * /usr/bin/python3 /home/.../runner.py >> ...runner.log 2>&1

# Test the get-session-id skill
~/.config/opencode/skills/get-session-id/scripts/get_session_id.py
# → ses_xxxxxxxxxxxxxxxxxxxxxxxx

# Test the schedule-wakeup skill
SW=~/.config/opencode/skills/schedule-wakeup/scripts/schedule_wakeup.py
$SW add --id test --prompt "hello" --minutes 1 && $SW list
# → Scheduled 'test' ... Active schedules for session ses_xxx (1)
```

---

## Usage

### Getting your session ID

```bash
# Bare ID (for scripts)
SID=$(~/.config/opencode/skills/get-session-id/scripts/get_session_id.py)

# With metadata
~/.config/opencode/skills/get-session-id/scripts/get_session_id.py --json
# {
#   "cwd": "/home/user/project",
#   "id": "ses_xxxxxxxxxxxxxxxxxxxx",
#   "opencode_pid": 12345,
#   "source": "opencode-db",
#   "title": "Debug training pipeline",
#   "updated": "2026-06-25T12:00:00"
# }
```

### Scheduling wakeups

```bash
SW=~/.config/opencode/skills/schedule-wakeup/scripts/schedule_wakeup.py

# One-shot: check back in 30 minutes
$SW add --id train_03 --prompt "cat train.log | tail -20; report last seg_dice" --minutes 30

# Recurring: poll every 5 minutes
$SW add --id poll_bjobs --prompt "bjobs | grep my_job" --minutes 5 --recurring

# List all schedules
$SW list

# List as JSON (for programmatic use)
$SW list --json

# Inspect one schedule
$SW show --id train_03

# Cancel a schedule
$SW remove --id train_03

# Cancel all schedules for the current session
$SW remove --all
```

All commands auto-detect the active session ID. Override with `--session ses_xxx` to act on a different session's schedules.

### Writing a prompt that fires reliably

When a wakeup fires, the cron runner calls:

```
opencode run -s <session_id> -- <prompt>
```

The `--prompt` text becomes the **entire context** of the re-invoked chat. Make it self-contained:

```
Bad:    "check the run"
Good:   "cat /home/user/project/outputs/run_03/train.log | tail -20; "
        "report last seg_dice EMA, last I→T R@5, and whether the run is "
        "still RUNNING via bjobs; if FINISHED, remove this schedule."
```

Include:
- Absolute file paths, commands, and queries.
- What metrics to report.
- What "done" looks like so the wakeup can clean up after itself.

### Agent-driven usage (via AGENTS.md)

Once `AGENTS.md` is wired into your opencode config, the agent will **proactively** reach for these skills when:

- You say "remind me in 30 minutes", "check back in N", "ping me at X".
- It kicks off a long job (`bsub`, training run, build, download).
- It needs to poll a status on an interval.
- It needs to name output files with the session ID.

The agent handles `add`, `list`, `remove`, and cleanup automatically per the AGENTS.md instructions.

---

## Cron runner reference

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHEDULE_DIR` | `~/.opencode/schedules` | Root directory for schedule JSON files |
| `OPENCODE_BIN` | `~/.opencode/bin/opencode` | Absolute path to the opencode binary |
| `RUNNER_LOG` | `~/.opencode/runner.log` | Append-only log file |
| `RUNNER_LOCK` | `~/.opencode/runner.lock` | Lock file for single-flight protection |
| `RUNNER_TIMEOUT` | `300` | Per-wakeup timeout for the spawned `opencode run` (seconds) |
| `RUNNER_RETRY_MINUTES` | `5` | Minutes to re-arm `next_wakeup` after a failed or timed-out run |
| `RUNNER_MAX_ATTEMPTS` | `5` | After N failed attempts, the schedule is moved to `<id>.json.bad` |
| `RUNNER_STDOUT_TAIL` | `4000` | How many trailing chars of stdout to capture in the log |
| `RUNNER_STDERR_TAIL` | `4000` | How many trailing chars of stderr to capture in the log |
| `RUNNER_CLAIM_TIMEOUT` | `3600` | Seconds before a stale `<id>.json.dispatch` marker is reaped (for crashed workers) |

### Log file

Every tick writes a timestamped line to `RUNNER_LOG`:

```
[2026-06-25T12:00:00] triggering wakeup 'train_03' for session ses_xxx (type=once)
[2026-06-25T12:00:00] FATAL: opencode binary not found at ... ; check OPENCODE_BIN / PATH
[2026-06-25T12:00:00] dispatched worker pid 48253 for train_03.json
[2026-06-25T12:00:00] skip train_03.json: another worker in flight (dispatch marker 17s old)
[2026-06-25T12:00:00] stale dispatch marker for train_03.json (3700s old, threshold 3600s); removing
[2026-06-25T12:00:00] opencode run ok for wakeup 'train_03' (rc=0, 1234B stdout, 5678B stderr)
[2026-06-25T12:00:00]   stdout (tail):
... [captured stdout] ...
[2026-06-25T12:00:00] opencode run failed for wakeup 'train_03' (rc=1); will retry in 5min
[2026-06-25T12:00:00]   stderr (tail):
... [captured stderr] ...
[2026-06-25T12:00:00] opencode run timed out after 300s for wakeup 'train_03'; will retry in 5min
[2026-06-25T12:00:00] worker crashed on .../train_03.json: ValueError: bad prompt
[2026-06-25T12:00:00] moved bad schedule ... -> ... .bad: opencode run failed 5 times; giving up
[2026-06-25T12:00:00] moved bad schedule ... -> ... .bad: unreadable JSON: ...
[2026-06-25T12:00:00] another runner is active; skipping this tick
```

### Lock file

`runner.py` uses `fcntl.flock` on `RUNNER_LOCK` so that two parent ticks cannot run at the same time. Because the tick is non-blocking (it just dispatches workers and exits, usually in well under a second), the lock is rarely contended; the next tick acquires it as soon as the previous one releases. The lock is released on normal exit or `finally`.

### Dispatch marker (per-wakeup single-flight)

When the parent tick decides a schedule is due, it atomically creates `<id>.json.dispatch` next to the schedule JSON (using `O_CREAT | O_EXCL`) and spawns a dedicated worker. The worker removes the marker when it exits, success or failure. Any other tick that sees the marker within `RUNNER_CLAIM_TIMEOUT` seconds skips that schedule, ensuring at most one worker per schedule at a time even when the 60s tick and the worker are slow. Markers older than `RUNNER_CLAIM_TIMEOUT` (default 1h) are treated as stale (the previous worker is assumed to have crashed) and reaped before re-claiming.

### Schedule file format

```json
{
  "session_id":  "ses_xxxxxxxxxxxxxxxxxxxx",
  "wakeup_id":   "train_03",
  "prompt":      "cat train.log | tail -20",
  "type":        "once",
  "minutes":     30,
  "next_wakeup": "2026-06-25T12:30:00",
  "attempts":    0,
  "last_error":  null,
  "created_at":  "2026-06-25T12:00:00"
}
```

- `type: "once"` → runner deletes the file after firing.
- `type: "interval"` → runner advances `next_wakeup` to `now + minutes` after a successful run. Failed interval runs re-arm to `now + RUNNER_RETRY_MINUTES` (not `minutes`) and stay on the retry interval until the run succeeds.
- On failure, the runner writes `attempts` (int) and `last_error` (str) into the schedule JSON and re-arms `next_wakeup`. On success it clears both. After `attempts >= RUNNER_MAX_ATTEMPTS` the file is moved to `<id>.json.bad`.
- `created_at` is informational (set by `schedule_wakeup.py add`, ignored by the runner).
- Times are naive local time (no timezone). The runner and cron must run in the same timezone.
- A sibling `<id>.json.dispatch` (empty file) is created by the dispatcher and removed by the worker; it is not part of the schedule contract and should not be edited by hand.

---

## Troubleshooting

### "Session ID not found"
- If your opencode setup does NOT use `~/.local/share/opencode/opencode.db` (e.g. custom XDG paths), the DB lookup will fail. Pass `--session ses_xxx` explicitly or set `$OPENCODE_SESSION_ID`.
- On macOS, `/proc` is unavailable; the script falls back to `lsof`. Install `lsof` if not already present (`brew install lsof` or it is usually pre-installed).

### Schedule file is written but wakeup never fires
- Check the runner log: `tail -20 ~/.opencode/runner.log`
- **Linux:** Verify the crontab is active: `crontab -l | grep runner`
- **macOS:** Verify launchd is loaded: `launchctl list | grep opencode`. Check logs: `log show --predicate 'process == "runner.py"' --last 1h`.
- Verify `PATH` includes `~/.opencode/bin` (or wherever `opencode` is installed). On macOS, the launchd plist template already sets `PATH`.
- Manually run the runner: `python3 ~/.opencode/runner.py` and check the log.

### `opencode run` fails in the runner
- The runner passes `OPENCODE_YOLO=true` to the spawned `opencode run` to skip permission prompts. If your config has explicit `deny` rules that block parts of the prompt, the run may fail. The runner waits for the run to complete and logs the captured stdout (up to `RUNNER_STDOUT_TAIL` chars) and stderr (up to `RUNNER_STDERR_TAIL` chars) right after the `opencode run failed (rc=N)` line.
- On failure, timeout, or launch error the schedule is **re-armed** to `now + RUNNER_RETRY_MINUTES` and the `attempts` counter in the schedule JSON is incremented. After `RUNNER_MAX_ATTEMPTS` failed tries the schedule is moved to `<id>.json.bad` so a permanently broken wakeup can't tight-loop the runner.
- If the binary is not at the default path, set `OPENCODE_BIN` in the runner's environment.
- A captured `stdout` of several KB is normal: `opencode run -s <session>` resumes the session, so the model sees the full conversation context and may produce a long response. If you don't want that in `RUNNER_LOG`, set `RUNNER_STDOUT_TAIL=0` (the line is still written; the tail is just empty).

### Schedule is stuck (worker crashed or was killed)
- The dispatcher leaves `<id>.json.dispatch` while a worker is in flight, and removes it when the worker exits. If the worker is killed (e.g. `SIGKILL` from a manual `pkill` or a process-group teardown), the marker is left behind and subsequent ticks will log `skip <file>: another worker in flight`.
- After `RUNNER_CLAIM_TIMEOUT` (default 3600s = 1h) the dispatcher treats the marker as stale, removes it, and re-dispatches.
- To unstick the schedule immediately, remove the marker by hand: `rm ~/.opencode/schedules/<sid>/<id>.json.dispatch`. The next tick (or `python3 ~/.opencode/runner.py`) will re-dispatch.

### AGENTS.md is loaded but the agent doesn't use the skills
- The skills must be under `~/.config/opencode/skills/` (or another path in `skills.paths`). Confirm with:
  ```bash
  ls ~/.config/opencode/skills/get-session-id/
  ```
- Restart opencode completely after adding/changing skills or `instructions`.
- The agent only discovers skills at startup. A hot-reload is not supported.

### Stale schedules re-fire forever
- Remove them: `schedule_wakeup.py remove --id <id>` or `--all`.
- The AGENTS.md instructions include a cleanup mandate — if the agent scheduled it, it should remove it when done. If it didn't, remove it manually.
- A schedule file moved to `<name>.json.bad` by the runner (corrupt/missing fields) will be ignored. Either delete the `.bad` file or fix and rename it.

---

## How the two skills complement each other

- **`get-session-id`** answers "who am I?" — the script traces the parent process tree and opencode SQLite DB to find the active session ID. No user interaction needed.
- **`schedule-wakeup`** answers "how do I set an alarm?" — writes a JSON file to a convention-driven directory that a cron job reads.
- Both skills share the same session ID detection logic (kept inline so neither depends on the other).
- Neither skill makes network calls. Both are pure Python stdlib.

---

## License

MIT — see [LICENSE](LICENSE). Free to use, modify, and share.
