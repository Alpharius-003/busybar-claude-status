#!/bin/bash
# Forward Claude Code status to the BusyBar daemon (starting it if needed).
#
# Usage (both modes read JSON on stdin):
#   report.sh state <STATE>   # from settings.json hooks; STATE e.g. WORKING
#   report.sh statusline      # from statusline-command.sh, forwards the payload
#   report.sh ensure          # only guarantee the daemon is up (no stdin)
#
# Must never slow Claude Code down: short curl timeouts, failures ignored.
# Optional persistent config lives in env.sh next to this file
# (BUSYBAR_STYLE / _TRANSPORT / _RENDER_MODE / _LISTEN / _HUB / _HOST_TAG ...).

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/.claude/busybar-daemon.log"
[ -f "$DIR/env.sh" ] && . "$DIR/env.sh"
PORT=8765

# BUSYBAR_HUB: another computer owns the Bar - forward there, run nothing here.
# BUSYBAR_STANDBY=1 on top: run the local daemon after all; it mirrors to the
# hub and takes over the Bar while the hub is asleep.
FORWARD=""
STANDBY=""
case "$(printf '%s' "$BUSYBAR_STANDBY" | tr 'A-Z' 'a-z')" in 1|true|yes|on) STANDBY=1;; esac
[ -n "$BUSYBAR_HUB" ] && [ -z "$STANDBY" ] && FORWARD=1
if [ -n "$FORWARD" ]; then BASE="${BUSYBAR_HUB%/}"; else BASE="http://127.0.0.1:$PORT"; fi
HDR=(-H "X-Busybar-Host: ${BUSYBAR_HOST:-$(hostname -s 2>/dev/null || hostname)}")
[ -n "$BUSYBAR_HOST_TAG" ] && HDR+=(-H "X-Busybar-Host-Tag: $BUSYBAR_HOST_TAG")
[ -n "$BUSYBAR_HUB_TOKEN" ] && HDR+=(-H "X-Busybar-Token: $BUSYBAR_HUB_TOKEN")

[ "$1" = "ensure" ] || body=$(cat)

# Spawn the daemon if the port is not answering. A race here is harmless:
# the loser of the bind exits immediately.
if [ -z "$FORWARD" ] && ! curl -m 0.3 -s -o /dev/null "http://127.0.0.1:$PORT/health"; then
  [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 1048576 ] && : >"$LOG"
  nohup /usr/bin/env python3 "$DIR/daemon.py" >>"$LOG" 2>&1 &
  disown 2>/dev/null
fi

case "$1" in
  ensure)
    ;;
  state)
    printf '%s' "$body" | curl -m 1 -s -o /dev/null -X POST "${HDR[@]}" \
      "$BASE/state?state=$2" --data-binary @- 2>/dev/null
    ;;
  statusline)
    printf '%s' "$body" | curl -m 1 -s -o /dev/null -X POST "${HDR[@]}" \
      "$BASE/statusline" --data-binary @- 2>/dev/null
    ;;
esac
exit 0
