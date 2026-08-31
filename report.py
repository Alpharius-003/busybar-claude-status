#!/usr/bin/env python3
"""Cross-platform forwarder + daemon supervisor (Python twin of report.sh).

    report.py ensure               # just make sure the daemon is running
    report.py state <STATE>        # forward a hook event  (JSON on stdin)
    report.py statusline           # forward statusline JSON (on stdin)

Used by setup_claude.py on Windows (and usable everywhere); report.sh
remains for existing POSIX installs. Must never slow Claude Code down:
sub-second timeouts, failures ignored, daemon spawned detached.

Reads env.sh next to this file for configuration (plain `KEY=VALUE` or
`export KEY=VALUE` lines — the same file bash sources on macOS/Linux):

    BUSYBAR_HUB=http://<hub>:8765   forward to the computer that owns the
                                    Bar instead of running a daemon here
    BUSYBAR_HUB_TOKEN=...           shared secret, if the hub requires one
    BUSYBAR_HOST_TAG=W | #00A4EF    how this computer's sessions are marked
                                    on the display (letter(s) or a flag color)
    BUSYBAR_HOST=studio-pc          name shown in /status (default: hostname)
"""

from __future__ import annotations

import os
import pathlib
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PORT = 8765
LOG = pathlib.Path.home() / ".claude" / "busybar-daemon.log"
DOWN_MARK = HERE / ".hub_down"   # touched when the hub is unreachable
DOWN_BACKOFF_S = 20              # ... then skip forwarding for this long
FORWARD_CAP_S = 1.2              # hard cap per hook, DNS included


def load_env() -> dict:
    env = dict(os.environ)
    try:
        for line in (HERE / "env.sh").read_text().splitlines():
            m = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=("?)(.*)\2\s*$', line)
            if m and not line.lstrip().startswith("#"):
                env[m.group(1)] = m.group(3)
    except OSError:
        pass
    return env


ENV = load_env()
HUB = ENV.get("BUSYBAR_HUB", "").strip().rstrip("/")
BASE = HUB or f"http://127.0.0.1:{PORT}"
HEADERS = {"Content-Type": "application/json",
           "X-Busybar-Host": (ENV.get("BUSYBAR_HOST") or platform.node().split(".")[0])[:64] or "?"}
if ENV.get("BUSYBAR_HOST_TAG"):
    HEADERS["X-Busybar-Host-Tag"] = ENV["BUSYBAR_HOST_TAG"].strip()
if ENV.get("BUSYBAR_HUB_TOKEN"):
    HEADERS["X-Busybar-Token"] = ENV["BUSYBAR_HUB_TOKEN"].strip()


def daemon_alive() -> bool:
    try:
        urllib.request.urlopen(BASE + "/health", timeout=0.4)
        return True
    except OSError:
        return False


def ensure_daemon():
    if HUB or daemon_alive():
        return  # a hub runs the daemon for us
    try:
        if LOG.exists() and LOG.stat().st_size > 1 << 20:
            LOG.write_text("")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG, "ab")
    except OSError:
        log = subprocess.DEVNULL
    kwargs: dict = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL,
                    "env": ENV, "cwd": str(HERE)}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    # sys.executable sidesteps the python vs python3 naming mess entirely.
    subprocess.Popen([sys.executable, str(HERE / "daemon.py")], **kwargs)


def _hub_down() -> bool:
    try:
        return time.time() - DOWN_MARK.stat().st_mtime < DOWN_BACKOFF_S
    except OSError:
        return False


def forward(path: str, body: bytes) -> bool:
    """POST to the daemon/hub. Runs in a daemon thread so a stalled DNS
    lookup or an asleep hub can never hold a Claude Code hook longer than
    FORWARD_CAP_S; an unreachable hub is then backed off for a while."""
    if HUB and _hub_down():
        return False
    ok = [False]

    def _post():
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + path, data=body, headers=HEADERS), timeout=1)
            ok[0] = True
        except OSError:
            pass

    t = threading.Thread(target=_post, daemon=True)
    t.start()
    t.join(FORWARD_CAP_S)
    if HUB:
        try:
            if ok[0]:
                DOWN_MARK.unlink(missing_ok=True)
            else:
                DOWN_MARK.touch()
        except OSError:
            pass
    return ok[0]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ensure"
    ensure_daemon()
    if mode == "state":
        state = sys.argv[2] if len(sys.argv) > 2 else "WORKING"
        forward(f"/state?state={state}", sys.stdin.buffer.read() or b"{}")
    elif mode == "statusline":
        forward("/statusline", sys.stdin.buffer.read() or b"{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
