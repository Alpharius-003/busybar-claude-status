#!/usr/bin/env python3
"""Debug tool: capture the Busy Bar front display to an upscaled PNG.

The /api/screen endpoint returns base64 of the raw 72x16 framebuffer in
BGR byte order; this swaps it back to RGB.

Usage: python3 screenshot.py [out.png]
"""
import base64, struct, sys, urllib.request, zlib

W, H, SCALE = 72, 16, 6
url = "http://10.0.4.20/api/screen?display=0"
raw = base64.b64decode(urllib.request.urlopen(url, timeout=5).read())
assert len(raw) == W * H * 3, len(raw)

out = sys.argv[1] if len(sys.argv) > 1 else "front.png"
rows = []
for y in range(H):
    row = bytearray()
    for x in range(W):
        i = (y * W + x) * 3
        b, g, r = raw[i], raw[i + 1], raw[i + 2]
        row += bytes((r, g, b)) * SCALE
    for _ in range(SCALE):
        rows.append(b"\x00" + bytes(row))

def chunk(tag, data):
    c = tag + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", W * SCALE, H * SCALE, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(b"".join(rows)))
png += chunk(b"IEND", b"")
open(out, "wb").write(png)
print("saved", out)
