#!/usr/bin/env python3
"""Generate Busy Bar `.anim` files ("bicycle0" format) for the Claude
status ring — one seamless-looping animation per session state.

The ring is drawn PER-PIXEL along the 172px perimeter path at 25 fps and
played natively by the device's anim decoder, which is exactly how the
built-in keep_out theme gets its smoothness.

Format reference: the firmware's "File Formats" documentation page and
the public struct layouts in lib/anim_file/anim_file_format.h. The RLE
codec here is an independent implementation of that documented format.

Usage:
    python3 animgen.py out_dir/     # writes work.anim, think.anim, ...
"""

from __future__ import annotations

import math
import struct
import sys

W, H = 72, 16
FPS = 25
PERIMETER = 2 * (W + H) - 4  # 172

# Claude Code theme rainbow (rainbow_red .. rainbow_violet)
CLI_RAINBOW = [
    (235, 95, 87), (245, 139, 87), (250, 195, 95), (145, 200, 130),
    (130, 170, 220), (155, 130, 200), (200, 130, 180),
]

PURPLE = (175, 135, 255)   # CLI effortUltra
GREEN = (32, 192, 64)
ORANGE = (255, 106, 0)
RED = (255, 32, 32)
GRAY = (80, 80, 80)


# --------------------------------------------------------------------------
# Perimeter path: clockwise from top-left. Position -> (x, y).
# --------------------------------------------------------------------------

def path_pixels() -> list[tuple[int, int]]:
    pixels = []
    pixels += [(x, 0) for x in range(72)]           # top
    pixels += [(71, y) for y in range(1, 16)]       # right
    pixels += [(x, 15) for x in range(70, -1, -1)]  # bottom
    pixels += [(0, y) for y in range(14, 0, -1)]    # left
    assert len(pixels) == PERIMETER
    return pixels


PATH = path_pixels()


# --------------------------------------------------------------------------
# Color helpers
# --------------------------------------------------------------------------

def lerp(a, b, u):
    return tuple(ca + (cb - ca) * u for ca, cb in zip(a, b))


def rainbow_at(u: float):
    n = len(CLI_RAINBOW)
    pos = (u % 1.0) * n
    i = int(pos) % n
    return lerp(CLI_RAINBOW[i], CLI_RAINBOW[(i + 1) % n], pos - int(pos))


def scale(rgb, v):
    return tuple(c * v for c in rgb)


# --------------------------------------------------------------------------
# Frame renderers: (frame_idx, frame_count) -> color per path position
# --------------------------------------------------------------------------

def render_frame(color_at) -> bytes:
    """BGRA8888 frame: transparent interior, colored 1px ring."""
    buf = bytearray(W * H * 4)  # all zero = transparent
    for p, (x, y) in enumerate(PATH):
        r, g, b = (max(0, min(255, round(c))) for c in color_at(p))
        i = (y * W + x) * 4
        buf[i:i + 4] = bytes((b, g, r, 255))
    return bytes(buf)


def anim_working(n=80):  # rainbow marquee, one full revolution per loop
    return [
        render_frame(lambda p, f=f: rainbow_at(p / PERIMETER + f / n))
        for f in range(n)
    ]


def anim_thinking(n=50):  # two purple crests traveling around
    def color(p, f):
        v = 0.22 + 0.78 * (0.5 + 0.5 * math.sin(2 * math.pi * (p / PERIMETER * 2 - f / n)))
        return scale(PURPLE, v)
    return [render_frame(lambda p, f=f: color(p, f)) for f in range(n)]


def _pulse(base, n, floor):
    frames = []
    for f in range(n):
        v = floor + (1 - floor) * (0.5 - 0.5 * math.cos(2 * math.pi * f / n))
        frames.append(render_frame(lambda p, v=v: scale(base, v)))
    return frames


def anim_complete():
    return _pulse(GREEN, 70, 0.15)   # 2.8s calm breathing


def anim_wait():
    return _pulse(ORANGE, 22, 0.30)  # 0.88s urgent pulse


def anim_error(n=12):  # 2 Hz hard blink, dim in the off phase
    frames = []
    for f in range(n):
        v = 1.0 if f < n // 2 else 0.12
        frames.append(render_frame(lambda p, v=v: scale(RED, v)))
    return frames


def anim_idle():
    return [render_frame(lambda p: scale(GRAY, 0.5))]


# --------------------------------------------------------------------------
# "bicycle0" encoder
# --------------------------------------------------------------------------

MAX_BLOCKS = 127


def rle_compress(data: bytes, blk: int) -> bytes:
    """Encode to the firmware's RLE stream format.

    The format (documented in the firmware's File Formats page): a stream
    of opcode bytes, each followed by payload. Opcode with the high bit
    set (0x80|n, 1<=n<=127) introduces n literal blocks; opcode without
    it (n, 1<=n<=127) repeats the single following block n times. Any
    grouping that follows those rules is a valid stream; we emit a repeat
    op for runs of 3+ equal blocks (below that a literal is no larger).
    """
    blocks = [data[i:i + blk] for i in range(0, len(data), blk)]
    out = bytearray()
    literals: list[bytes] = []

    def flush():
        for j in range(0, len(literals), MAX_BLOCKS):
            chunk = literals[j:j + MAX_BLOCKS]
            out.append(0x80 | len(chunk))
            out.extend(b"".join(chunk))
        literals.clear()

    i, n = 0, len(blocks)
    while i < n:
        run = 1
        while run < MAX_BLOCKS and i + run < n and blocks[i + run] == blocks[i]:
            run += 1
        if run >= 3:
            flush()
            out.append(run)
            out.extend(blocks[i])
        else:
            literals.extend(blocks[i:i + run])
        i += run
    flush()
    return bytes(out)


def rle_decompress(data: bytes, blk: int) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        op = data[i]
        n = op & 0x7F
        i += 1
        if op & 0x80:
            out += data[i:i + n * blk]
            i += n * blk
        else:
            out += data[i:i + blk] * n
            i += blk
    return bytes(out)


def encode_anim(display_frames: list[bytes], fps: int = FPS,
                w: int = W, h: int = H) -> bytes:
    """Encode BGRA8888 display frames into a .anim blob (one 'default' section)."""
    # Interframe RLE: collapse identical consecutive display frames.
    file_frames: list[tuple[bytes, int]] = []
    for frame in display_frames:
        if file_frames and file_frames[-1][0] == frame and file_frames[-1][1] < 255:
            file_frames[-1] = (frame, file_frames[-1][1] + 1)
        else:
            file_frames.append((frame, 1))

    frames_blob = bytearray()
    max_len = 0
    for raw, duration in file_frames:
        rle = rle_compress(raw, 4)
        if len(rle) < len(raw):
            encoding, data = 1, rle  # AnimFileFrameEncodingRle
        else:
            encoding, data = 0, raw  # AnimFileFrameEncodingRaw
        assert len(data) <= 0xFFFF
        max_len = max(max_len, len(data))
        frames_blob += struct.pack("<BBH", encoding, duration, len(data))
        frames_blob += data

    name = b"default\x00"
    header_size = 36
    sections_len = 4 + 4 + 4 + 1 + len(name)
    section = struct.pack(
        "<IIIB", 0, len(display_frames) - 1, header_size + sections_len,
        file_frames[0][1],
    ) + name

    header = struct.pack(
        "<8sBBBBBHxIIIII",
        b"bicycle0",
        0,                       # flags
        w, h,
        2,                       # AnimFileColorFormatBgra8888
        fps,
        max_len,
        len(section),
        len(frames_blob),
        1,                       # section_count
        len(file_frames),
        len(display_frames),
    )
    assert len(header) == header_size, len(header)
    return bytes(header + section + frames_blob)


def decode_check(blob: bytes, display_frames: list[bytes],
                 w: int = W, h: int = H):
    """Round-trip sanity check of our own encoding."""
    sig, flags, fw, fh, fmt, fps, max_len, s_len, f_len, s_cnt, ff_cnt, df_cnt = \
        struct.unpack("<8sBBBBBHxIIIII", blob[:36])
    assert sig == b"bicycle0" and (fw, fh, fmt) == (w, h, 2) and df_cnt == len(display_frames)
    off = 36 + s_len
    out = []
    for _ in range(ff_cnt):
        enc, dur, ln = struct.unpack("<BBH", blob[off:off + 4])
        off += 4
        data = blob[off:off + ln]
        off += ln
        raw = rle_decompress(data, 4) if enc == 1 else data
        assert len(raw) == w * h * 4
        out.extend([raw] * dur)
    assert out == display_frames, "roundtrip mismatch"


# --------------------------------------------------------------------------
# Avatar animations ("avatar" display style): a little pixel companion,
# 14x14, drawn from character grids. Body color = the CLI's clawd_body.
# --------------------------------------------------------------------------

AVATAR_W = AVATAR_H = 14
AVATAR_FPS = 5

_AVATAR_PALETTE = {
    "B": (215, 119, 87), "b": (176, 92, 66), "D": (26, 16, 12),
    "Y": (255, 210, 30), "y": (140, 110, 20), "W": (232, 232, 232),
    "G": (110, 116, 130), "S": (207, 227, 255), "s": (120, 140, 170),
}


def _grid_frame(grid: str) -> bytes:
    rows = [r.ljust(AVATAR_W, ".") for r in grid.strip("\n").split("\n")]
    assert len(rows) == AVATAR_H, len(rows)
    buf = bytearray(AVATAR_W * AVATAR_H * 4)
    for y, row in enumerate(rows):
        for x in range(AVATAR_W):
            c = _AVATAR_PALETTE.get(row[x])
            if c:
                r, g, b = c
                i = (y * AVATAR_W + x) * 4
                buf[i:i + 4] = bytes((b, g, r, 255))
    return bytes(buf)


_AV_WORK_BLK = """
..............
..............
..............
..............
..BBBBBBBBBBB.
.BBBBBBBBBBBBB
.BBBBBBBBBBBBB
..BBBBBBBBBBB.
..BsSSSSSSSsB.
..BGGGGGGGGGB.
..GGGGGGGGGGG.
..GGGGGGGGGGG.
..............
..............
"""
_AV_THINK_BLK = """
...........YY.
..........YYYY
...........YY.
...........ss.
..BBBBBBBBBBB.
.BBBBBBBBBBBBB
.BBBBBBBBBBBBB
..BBBBBBBBBBB.
..BBBBBBBBBBB.
..BBBBBBBBBBB.
...B.B...B.B..
...B.B...B.B..
..............
..............
"""
_AV_WORK_A = """
..............
..............
..............
..............
..BBBBBBBBBBB.
.BBBBDBBBDBBBB
.BBBBDBBBDBBBB
..BBBBBBBBBBB.
..BsSSSSSSSsB.
..BGGGGGGGGGB.
..GGGGGGGGGGG.
..GGGGGGGGGGG.
..............
..............
"""
_AV_WORK_B = """
..............
..............
..............
..............
..BBBBBBBBBBB.
.BBBBDBBBDBBBB
.BBBBDBBBDBBBB
..BBBBBBBBBBB.
..BsSSSSSSSsB.
..BGGGGGGGGGB.
..GGGWGGGWGGG.
..GGGGGGGGGGG.
..............
..............
"""
_AV_THINK_A = """
...........YY.
..........YYYY
...........YY.
...........ss.
..BBBBBBBBBBB.
.BBBBDBBBDBBBB
.BBBBDBBBDBBBB
..BBBBBBBBBBB.
..BBBBBBBBBBB.
..BBBBBBBBBBB.
...B.B...B.B..
...B.B...B.B..
..............
..............
"""
_AV_THINK_B = """
...........yy.
..........y..y
...........yy.
...........ss.
..BBBBBBBBBBB.
.BBBBDBBBDBBBB
.BBBBDBBBDBBBB
..BBBBBBBBBBB.
..BBBBBBBBBBB.
..BBBBBBBBBBB.
...B.B...B.B..
...B.B...B.B..
..............
..............
"""
_AV_DONE_A = """
..............
..............
..............
..............
..BBBBBBBBBBB.
.BBBBBBBBBWBBB
.BBBDDBBDDBWBB
..BBBBBBBBWsW.
..BBBBBBBBWWWW
..BBBBBBBBWWWW
...B.B...BWWW.
...B.B...B.B..
..............
..............
"""
_AV_DONE_B = """
..............
..............
..............
..............
..BBBBBBBBBBB.
.BBBBBBBBBBWBB
.BBBDDBBDDBBWB
..BBBBBBBBWsW.
..BBBBBBBBWWWW
..BBBBBBBBWWWW
...B.B...BWWW.
...B.B...B.B..
..............
..............
"""
_AV_WAIT_A = """
......WW......
......WW......
..............
......WW......
..BBBBBBBBBBB.
.BBBBDBBBDBBBB
.BBBBDBBBDBBBB
..BBBBBBBBBBB.
..BBBBBBBBBBB.
..BBBBBBBBBBB.
...B.B...B.B..
...B.B...B.B..
..............
..............
"""
_AV_WAIT_B = """
..............
......WW......
......WW......
..............
..BBBBWWBBBBB.
.BBBBDBBBDBBBB
.BBBBDBBBDBBBB
..BBBBBBBBBBB.
..BBBBBBBBBBB.
..BBBBBBBBBBB.
...B.B...B.B..
...B.B...B.B..
..............
..............
"""
_AV_ERROR_A = """
..............
..............
..............
..............
..BBDBDBDBDBB.
.BBBBDBBBDBBBB
.BBBDBDBDBDBBB
..BBBBBBBBBBB.
..BBBBBBBBBBB.
..BBBBBBBBBBB.
...B.B...B.B..
...B.B...B.B..
..............
..............
"""
_AV_ERROR_B = """
..............
..............
..............
..............
...BBDBDBDBDBB
..BBBBDBBBDBBB
..BBBDBDBDBDBB
...BBBBBBBBBBB
...BBBBBBBBBBB
...BBBBBBBBBBB
....B.B...B.B.
....B.B...B.B.
..............
..............
"""
_AV_IDLE_A = """
..........WW..
..............
........W.....
..............
..BBBBBBBBBBB.
.BBBBBBBBBBBBB
.BBBDDBBDDBBBB
..BBBBBBBBBBB.
..BBBBBBBBBBB.
..BBBBBBBBBBB.
...B.B...B.B..
...B.B...B.B..
..............
..............
"""
_AV_IDLE_B = """
..............
.........WW...
..............
.......W......
..BBBBBBBBBBB.
.BBBBBBBBBBBBB
.BBBDDBBDDBBBB
..BBBBBBBBBBB.
..BBBBBBBBBBB.
..BBBBBBBBBBB.
...B.B...B.B..
...B.B...B.B..
..............
..............
"""


def _avatar(*grids):
    return [_grid_frame(g) for g in grids]


def _blit(buf: bytearray, grid: str, ox: int, oy: int, canvas_w: int = W):
    """Paint an avatar grid onto a BGRA canvas at (ox, oy)."""
    rows = [r.ljust(AVATAR_W, ".") for r in grid.strip("\n").split("\n")]
    for y, row in enumerate(rows):
        for x in range(AVATAR_W):
            c = _AVATAR_PALETTE.get(row[x])
            if c:
                r_, g_, b_ = c
                i = ((oy + y) * canvas_w + ox + x) * 4
                buf[i:i + 4] = bytes((b_, g_, r_, 255))


CLAUDE_ORANGE = (255, 153, 51)


def anim_claude_theme(n=80):
    """The on-device "claude" BUSY/CUSTOM theme background: a breathing
    claude-orange ring with the companion typing away in the middle."""
    frames = []
    for f in range(n):
        v = 0.25 + 0.75 * (0.5 - 0.5 * math.cos(2 * math.pi * f / n))
        buf = bytearray(render_frame(lambda p, v=v: scale(CLAUDE_ORANGE, v)))
        _blit(buf, _AV_WORK_A if (f // 8) % 2 == 0 else _AV_WORK_B, 29, 1)
        frames.append(bytes(buf))
    return frames


# filename -> (frame generator, width, height, fps)
ANIMS = {
    "work.anim": (anim_working, W, H, FPS),
    "think.anim": (anim_thinking, W, H, FPS),
    "done.anim": (anim_complete, W, H, FPS),
    "wait.anim": (anim_wait, W, H, FPS),
    "error.anim": (anim_error, W, H, FPS),
    "idle.anim": (anim_idle, W, H, FPS),
    "claw_work.anim": (lambda: _avatar(_AV_WORK_A, _AV_WORK_B, _AV_WORK_A, _AV_WORK_B, _AV_WORK_BLK, _AV_WORK_B), AVATAR_W, AVATAR_H, AVATAR_FPS),
    "claw_think.anim": (lambda: _avatar(_AV_THINK_A, _AV_THINK_A, _AV_THINK_B, _AV_THINK_BLK), AVATAR_W, AVATAR_H, 3),
    "claw_done.anim": (lambda: _avatar(_AV_DONE_A, _AV_DONE_B), AVATAR_W, AVATAR_H, 3),
    "claw_wait.anim": (lambda: _avatar(_AV_WAIT_A, _AV_WAIT_B), AVATAR_W, AVATAR_H, AVATAR_FPS),
    "claw_error.anim": (lambda: _avatar(_AV_ERROR_A, _AV_ERROR_B), AVATAR_W, AVATAR_H, 6),
    "claw_idle.anim": (lambda: _avatar(_AV_IDLE_A, _AV_IDLE_B), AVATAR_W, AVATAR_H, 2),
}


def main():
    import pathlib
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for fname, (gen, w, h, fps) in ANIMS.items():
        frames = gen()
        blob = encode_anim(frames, fps=fps, w=w, h=h)
        decode_check(blob, frames, w=w, h=h)
        (out / fname).write_bytes(blob)
        print(f"{fname}: {len(frames)} frames, {len(blob)} bytes")


if __name__ == "__main__":
    main()
