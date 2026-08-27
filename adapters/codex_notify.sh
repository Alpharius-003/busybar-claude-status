#!/bin/bash
# Codex `notify` hook wrapper: auto-starts the busybar pipeline whenever
# Codex is used, while preserving whatever notifier you already had.
#
# Installed into ~/.codex/config.toml by install_codex_autostart.py as:
#     notify = ["bash", "<repo>/adapters/codex_notify.sh"]
# Codex appends the notification JSON as the final argument.
#
# Chain: if adapters/codex_notify_chain.sh exists (created by the
# installer from your previous notify setting, gitignored), it is called
# first with the same arguments — your original notifier keeps working.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$HOME/.claude/busybar-daemon.log"

# 1. Original notifier (if any) — never let it block or fail us.
if [ -f "$DIR/adapters/codex_notify_chain.sh" ]; then
  bash "$DIR/adapters/codex_notify_chain.sh" "$@" >/dev/null 2>&1
fi

# 2. Ensure the daemon is up (report.sh handles env.sh + spawn race).
bash "$DIR/report.sh" ensure >/dev/null 2>&1

# 3. Ensure the codex adapter loop is running.
if ! pgrep -f "adapters/codex_status.py" >/dev/null 2>&1; then
  nohup /usr/bin/env python3 "$DIR/adapters/codex_status.py" >>"$LOG" 2>&1 &
  disown 2>/dev/null
fi

# 4. Push a fresh probe right now so the turn's end state lands instantly
#    (the loop would catch it within 2s anyway; this removes even that).
( /usr/bin/env python3 "$DIR/adapters/codex_status.py" --once >/dev/null 2>&1 & )

exit 0
