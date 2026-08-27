#!/bin/bash
# Forward Claude Code status to the BusyBar daemon (starting it if needed).
#
# Usage (both modes read JSON on stdin):
#   report.sh state <STATE>   # from settings.json hooks; STATE e.g. WORKING
#   report.sh statusline      # from statusline-command.sh, forwards the payload
#
# Must never slow Claude Code down: short curl timeouts, failures ignored.

PORT=8765
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/.claude/busybar-daemon.log"

# "ensure" mode only guarantees the daemon is up (no stdin, no report).
[ "$1" = "ensure" ] || body=$(cat)

# Spawn the daemon if the port is not answering. A race here is harmless:
# the loser of the bind exits immediately.
if ! curl -m 0.3 -s -o /dev/null "http://127.0.0.1:$PORT/health"; then
  [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 1048576 ] && : >"$LOG"
  # Optional persistent config (BUSYBAR_STYLE / _TRANSPORT / _RENDER_MODE ...)
  [ -f "$DIR/env.sh" ] && . "$DIR/env.sh"
  nohup /usr/bin/env python3 "$DIR/daemon.py" >>"$LOG" 2>&1 &
  disown 2>/dev/null
fi

case "$1" in
  ensure)
    ;;
  state)
    printf '%s' "$body" | curl -m 1 -s -o /dev/null -X POST \
      "http://127.0.0.1:$PORT/state?state=$2" --data-binary @- 2>/dev/null
    ;;
  statusline)
    printf '%s' "$body" | curl -m 1 -s -o /dev/null -X POST \
      "http://127.0.0.1:$PORT/statusline" --data-binary @- 2>/dev/null
    ;;
esac
exit 0
