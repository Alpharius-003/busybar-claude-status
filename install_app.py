#!/usr/bin/env python3
"""Install the "Claude Status" JS app onto the Busy Bar over USB.

- writes device_app/ files to /ext/user_assets/org.anthropic.claude_status/
- generates and writes the menu icons (8x8 front, 11x11 back)
- enables JS apps in the APPS menu (apps_menu flag file)
- regenerates and uploads the .anim ring assets for app "claude_status"

Idempotent: safe to re-run after changing main.js or the animations.
"""

from __future__ import annotations

import pathlib
import struct
import sys
import time
import urllib.parse
import urllib.request
import zlib

BASE = "http://10.0.4.20/api"
APP_ID = "org.anthropic.claude_status"
APP_ROOT = f"/ext/user_assets/{APP_ID}"
CANVAS_APP = "claude_status"  # where the .anim assets live
HERE = pathlib.Path(__file__).parent


def api(method: str, path: str, body: bytes | None = None, retry: bool = True) -> bytes:
    # The device web server occasionally stalls on rapid-fire requests;
    # pace the calls and retry on timeouts.
    last_err = None
    for attempt in range(3 if retry else 1):
        req = urllib.request.Request(
            BASE + path, data=body, method=method,
            headers={"Content-Type": "application/octet-stream"} if body else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            time.sleep(0.3)
            return data
        except urllib.error.HTTPError:
            raise  # 4xx/5xx: not transient, let the caller decide
        except (TimeoutError, OSError) as e:
            last_err = e
            if retry:
                print(f"  retry {attempt + 1} after {type(e).__name__} on {path}")
                time.sleep(2)
    raise last_err


def mkdir(path: str):
    try:
        api("POST", "/storage/mkdir?path=" + urllib.parse.quote(path))
        print("mkdir", path)
    except urllib.error.HTTPError as e:
        if e.code != 400:  # 400: already exists
            raise


def write_file(path: str, data: bytes):
    api("POST", "/storage/write?path=" + urllib.parse.quote(path), data)
    print(f"write {path} ({len(data)} bytes)")


# --- tiny PNG writer -------------------------------------------------------

def png(width, height, rows, greyscale=False):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    color_type = 0 if greyscale else 2
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def make_icons():
    """Front 8x8 colour + back 11x11 greyscale: a Claude-orange spark."""
    O = (255, 153, 51)   # claude orange
    W = (255, 224, 178)
    K = (0, 0, 0)
    star8 = [
        "...x....",
        ".x.x.x..",
        "..xxx...",
        "xxxOxxx.",
        "..xxx...",
        ".x.x.x..",
        "...x....",
        "........",
    ]
    rows8 = []
    for line in star8:
        row = []
        for ch in line:
            row += list(O if ch == "x" else W if ch == "O" else K)
        rows8.append(row)

    star11 = [
        ".....x.....",
        ".x...x...x.",
        "..x..x..x..",
        "...x.x.x...",
        "....xxx....",
        "xxxxxOxxxxx",
        "....xxx....",
        "...x.x.x...",
        "..x..x..x..",
        ".x...x...x.",
        ".....x.....",
    ]
    rows11 = []
    for line in star11:
        row = []
        for ch in line:
            row.append(200 if ch == "x" else 255 if ch == "O" else 0)
        rows11.append(row)

    return png(8, 8, rows8), png(11, 11, rows11, greyscale=True)


def main():
    # 1. app files
    mkdir(APP_ROOT)
    mkdir(APP_ROOT + "/appmeta")
    mkdir(APP_ROOT + "/scripts")
    write_file(APP_ROOT + "/scripts/main.js",
               (HERE / "device_app/scripts/main.js").read_bytes())

    front_icon, back_icon = make_icons()
    write_file(APP_ROOT + "/appmeta/icon_front_8x8.png", front_icon)
    write_file(APP_ROOT + "/appmeta/icon_back_11x11.png", back_icon)

    # 2. enable JS apps in the APPS menu
    mkdir("/ext/apps_data/apps_menu")
    write_file("/ext/apps_data/apps_menu/js_apps_enabled", b"1")

    # 3. ring animations (regenerate + upload)
    sys.path.insert(0, str(HERE))
    import animgen
    for fname, gen in animgen.ANIMS.items():
        frames = gen()
        blob = animgen.encode_anim(frames)
        animgen.decode_check(blob, frames)
        api("POST", f"/assets/upload?application_name={CANVAS_APP}&file={fname}", blob)
        print(f"upload asset {fname} ({len(blob)} bytes)")

    # 4. manifest LAST: the firmware's JS-app scanner reacts to it, and a
    #    manifest pointing at an incomplete app tree crashes (and reboots)
    #    the device. Everything else must already be in place. Single try.
    try:
        api("POST", "/storage/write?path=" + urllib.parse.quote(
            APP_ROOT + "/appmeta/manifest.json"),
            (HERE / "device_app/appmeta/manifest.json").read_bytes(), retry=False)
        print("write manifest.json")
    except Exception as e:
        print(f"manifest write: {type(e).__name__} (device may be rescanning)")
    time.sleep(3)
    listing = api("GET", "/storage/list?path=" + urllib.parse.quote(APP_ROOT + "/appmeta"))
    assert b"manifest.json" in listing, f"manifest missing: {listing!r}"
    print("manifest verified on device")

    print("\nDone. Press the APPS key on the device (or POST /api/input?key=apps)"
          "\nand select ✦ Claude Status.")


if __name__ == "__main__":
    main()
