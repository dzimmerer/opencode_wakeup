#!/usr/bin/env bash
# install.sh — Idempotent installer for opencode-wakeup.
#
# Usage:
#   ./install.sh              # copy skills + AGENTS.md + runner, show instructions
#   ./install.sh --crontab    # also install the crontab entry (interactive)
#   ./install.sh --help       # show this message
#
# Everything goes under:
#   ~/.config/opencode/skills/    ← skills
#   ~/.config/opencode/AGENTS.md  ← global instructions
#   ~/.opencode/runner.py         ← cron runner
#
# Safe to run multiple times — existing files are overwritten, crontab is
# updated in place (old runner entry is removed, new one appended).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_TARGET="$HOME/.config/opencode/skills"
AGENTS_TARGET="$HOME/.config/opencode/AGENTS.md"
RUNNER_TARGET="$HOME/.opencode/runner.py"
CONFIG_TARGET="$HOME/.config/opencode/opencode.json"
CRONTAB_TEMPLATE="$REPO_DIR/config/crontab.txt"

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# //; s/^#//'
  exit 0
}

info()  { printf '\e[36m[INFO]\e[m  %s\n' "$*"; }
warn()  { printf '\e[33m[WARN]\e[m  %s\n' "$*"; }
ok()    { printf '\e[32m[OK]\e[m    %s\n' "$*"; }
err()   { printf '\e[31m[FAIL]\e[m  %s\n' "$*" >&2; }
header(){ printf '\n\e[1m%s\e[m\n' "$*"; }

for arg; do
  case "$arg" in
    --help|-h) usage ;;
    --crontab) CRONTAB=1 ;;
    *) warn "unknown flag: $arg"; usage ;;
  esac
done

header "═══ opencode-wakeup installer ═══"

# ── Skills ──────────────────────────────────────────────────────────
header "· skills → $SKILLS_TARGET/"
mkdir -p "$SKILLS_TARGET"
for skill in get-session-id schedule-wakeup; do
  src="$REPO_DIR/skills/$skill"
  dst="$SKILLS_TARGET/$skill"
  mkdir -p "$(dirname "$dst")"
  cp -r "$src" "$(dirname "$dst")"
  # make scripts executable
  find "$dst" -name '*.py' -exec chmod +x {} \;
  ok "installed skill: $skill"
done

# ── AGENTS.md ────────────────────────────────────────────────────────
header "· global instructions → $AGENTS_TARGET"
cp "$REPO_DIR/AGENTS.md" "$AGENTS_TARGET"
ok "installed AGENTS.md"

# ── Runner ──────────────────────────────────────────────────────────
header "· cron runner → $RUNNER_TARGET"
mkdir -p "$HOME/.opencode"
cp "$REPO_DIR/runner/runner.py" "$RUNNER_TARGET"
chmod +x "$RUNNER_TARGET"
ok "installed runner.py"

# ── opencode.json config note ───────────────────────────────────────
header "· opencode.json"
if [ -f "$CONFIG_TARGET" ]; then
  if grep -q '"instructions"' "$CONFIG_TARGET" 2>/dev/null; then
    ok "instructions already present in $CONFIG_TARGET"
  else
    warn "add this to $CONFIG_TARGET:"
    echo ""
    cat "$REPO_DIR/config/opencode.json"
    echo ""
    warn "then quit and restart opencode."
  fi
else
  warn "no global opencode.json found — create $CONFIG_TARGET with:"
  echo ""
  cat "$REPO_DIR/config/opencode.json"
  echo ""
fi

# ── Crontab ──────────────────────────────────────────────────────────
header "· crontab"
if [ "${CRONTAB:-0}" = "1" ]; then
  echo ""
  info "The following entry will be added to your crontab:"
  TMP_ENTRY=$(mktemp)
  sed "s|<USER_HOME>|$USER|g" "$CRONTAB_TEMPLATE" > "$TMP_ENTRY"
  echo ""
  grep -v '^\s*#' "$TMP_ENTRY"
  echo ""
  read -rp "Proceed? [y/N] " ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    (
      crontab -l 2>/dev/null \
        | grep -v "$RUNNER_TARGET" \
        | grep -v '^PATH=' \
        || true
      cat "$TMP_ENTRY"
    ) | crontab -
    ok "crontab updated"
  else
    info "skipped"
  fi
  rm -f "$TMP_ENTRY"
else
  echo ""
  info "pass --crontab to install the cron entry (or do it manually):"
  echo ""
  sed "s|<USER_HOME>|$USER|g" "$CRONTAB_TEMPLATE"
  echo ""
fi

# ── Summary ──────────────────────────────────────────────────────────
header "═══ Done ═══"
info "Next steps:"
echo "  1. If opencode.json was edited, quit and restart opencode."
echo "  2. Verify:"
echo "       ~/.config/opencode/skills/schedule-wakeup/scripts/schedule_wakeup.py list"
echo "       ~/.config/opencode/skills/get-session-id/scripts/get_session_id.py"
echo "       /usr/bin/python3 $RUNNER_TARGET && tail ~/.opencode/runner.log"
echo ""
