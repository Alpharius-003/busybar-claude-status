#!/usr/bin/env python3
"""Bind the Busy Bar's CUSTOM button to the Claude status display.

    python3 claude_card.py install   # back up the current CUSTOM card,
                                     # then make CUSTOM = "Claude" (INFINITE,
                                     # theme claude, smart-home trigger off)
    python3 claude_card.py restore   # put the backed-up card back
    python3 claude_card.py show      # print the current CUSTOM card

After install, pressing the physical CUSTOM key and starting the session
launches the status display (the daemon's snapshot watcher takes over the
screen); pressing OFF ends it.
"""

import json
import pathlib
import sys
import time
import urllib.request

BASE = "http://10.0.4.20/api"
BACKUP = pathlib.Path(__file__).parent / "custom_profile.backup.json"

CLAUDE_PROFILE = {
    "sort_order": -1,
    "title": "Claude",
    "id": "00000000-0000-0000-0000-00000000c1de",
    "timer_settings": {"type": "INFINITE"},
    "busy_bar_settings": {
        "theme": "claude",
        "show_work_phase_only": True,
        "trigger_smart_home": False,
    },
    "profile_timestamp_ms": 0,
}


def get_profile() -> dict:
    with urllib.request.urlopen(BASE + "/busy/profiles/custom", timeout=10) as r:
        return json.loads(r.read())


def put_profile(profile: dict):
    profile = dict(profile, profile_timestamp_ms=int(time.time() * 1000))
    req = urllib.request.Request(
        BASE + "/busy/profiles/custom",
        data=json.dumps(profile).encode(),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "install":
        current = get_profile()
        if current.get("title") != "Claude":
            BACKUP.write_text(json.dumps(current, indent=2))
            print(f"backed up current CUSTOM card ({current.get('title')!r}) -> {BACKUP.name}")
        print(put_profile(CLAUDE_PROFILE))
        print('CUSTOM key now launches "Claude". Press CUSTOM then START on the device.')
    elif cmd == "restore":
        if not BACKUP.exists():
            sys.exit("no backup file found")
        print(put_profile(json.loads(BACKUP.read_text())))
        print("CUSTOM card restored.")
    else:
        print(json.dumps(get_profile(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
