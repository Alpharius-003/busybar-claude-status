#!/usr/bin/env python3
"""Install (or refresh) the on-device "claude" BUSY/CUSTOM theme.

The theme shows a breathing claude-orange ring with the pixel companion
typing in the middle. It appears in the device's theme picker, serves as
the screen while a claude-theme focus session runs, and is the on-device
switch for the daemon's `theme` render mode.

Usage: python3 install_theme.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://10.0.4.20/api"
THEME_DIR = "/ext/apps_assets/busy/themes/claude"
ANIM_NAME = "claude_72x16.anim"


def api(method: str, path: str, body: bytes | None = None) -> bytes:
    req = urllib.request.Request(BASE + path, data=body, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    time.sleep(0.3)
    return data


def main():
    import animgen
    frames = animgen.anim_claude_theme()
    blob = animgen.encode_anim(frames, fps=20)
    animgen.decode_check(blob, frames)
    print(f"theme animation: {len(frames)} frames, {len(blob)} bytes")

    try:
        api("POST", "/storage/mkdir?path=" + urllib.parse.quote(THEME_DIR))
    except urllib.error.HTTPError:
        pass  # already exists

    # NOTE: uploading fails with "Failed to open file for writing" while a
    # claude-theme session is playing this file — end the session first.
    api("POST", "/storage/write?path=" + urllib.parse.quote(f"{THEME_DIR}/{ANIM_NAME}"), blob)
    print("uploaded", ANIM_NAME)

    theme = json.dumps({"bg_path": f"{THEME_DIR}/{ANIM_NAME}", "order": 99}).encode()
    api("POST", "/storage/write?path=" + urllib.parse.quote(f"{THEME_DIR}/theme.json"), theme)
    print("theme.json written — pick it on the device: CUSTOM → SETUP → theme")


if __name__ == "__main__":
    sys.exit(main())
