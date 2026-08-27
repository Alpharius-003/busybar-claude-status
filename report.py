#!/usr/bin/env python3
"""Cross-platform forwarder + daemon supervisor (Python twin of report.sh).

    report.py ensure               # just make sure the daemon is running
    report.py state <STATE>        # forward a hook event  (JSON on stdin)
    report.py statusline           # forward statusline JSON (on stdin)

Used by setup_claude.py on Windows (and usable everywhere); report.sh
remains for existing POSIX installs. Must never slow Claude Code down:
sub-second timeouts, failures ignored, daemon spawned detached.

Reads env.sh next to this file for configuration (plain `KEY=VALUE` or
`export KEY=VALUE` lines — the same file bash sources on macOS/Linux).
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
LOG = pathlib.Path.home() / ".claude" / "busybar-daemon.log"


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


def daemon_alive() -> bool:
    try:
        urllib.request.urlopen(BASE + "/health", timeout=0.4)
        return True
    except OSError:
        return False


def ensure_daemon():
    if daemon_alive():
        return
    try:
        if LOG.exists() and LOG.stat().st_size > 1 << 20:
            LOG.write_text("")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(LOG, "ab")
    except OSError:
        log = subprocess.DEVNULL
    kwargs: dict = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL,
                    "env": load_env(), "cwd": str(HERE)}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    # sys.executable sidesteps the python vs python3 naming mess entirely.
    subprocess.Popen([sys.executable, str(HERE / "daemon.py")], **kwargs)


def forward(path: str, body: bytes):
    try:
        urllib.request.urlopen(urllib.request.Request(
            BASE + path, data=body,
            headers={"Content-Type": "application/json"}), timeout=1)
    except OSError:
        pass


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
