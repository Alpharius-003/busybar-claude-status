#!/usr/bin/env python3
"""Generate Busy Bar `.anim` files ("bicycle0" format) for the Claude
status ring — one seamless-looping animation per session state.

The ring is drawn PER-PIXEL along the 172px perimeter path at 25 fps and
played natively by the device's anim decoder, which is exactly how the
built-in keep_out theme gets its smoothness.

Format reference: firmware lib/anim_file/anim_file_format.h (+ RLE from
lib/toolbox/rle_encode.c / scripts/flipper/rle.py).

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
RLE_THRESHOLD = 3


def rle_compress(source: bytes, blk: int) -> bytes:
    """Port of scripts/flipper/rle.py (compatible with toolbox/rle_encode)."""
    src_i, src_len = 0, len(source)
    dest = bytearray()
    while src_i < src_len:
        repeat = 0
        first = source[src_i:src_i + blk]
        for i in range(src_i, src_len, blk):
            if source[i:i + blk] == first:
                repeat += 1
            else:
                break
        repeat = min(repeat, MAX_BLOCKS)
        if repeat == 0:
            break
        if repeat < RLE_THRESHOLD:
            rep, verbatim = 0, 0
            for i in range(src_i, src_len, blk):
                if source[i:i + blk] == source[i + blk:i + 2 * blk]:
                    rep += 1
                    if rep > RLE_THRESHOLD:
                        break
                else:
                    verbatim += 1 + rep
                    rep = 0
            verbatim += rep
            verbatim = min(verbatim, MAX_BLOCKS)
            dest.append(0x80 | verbatim)
            dest.extend(source[src_i:src_i + verbatim * blk])
            src_i += verbatim * blk
        else:
            dest.append(repeat)
            dest.extend(first)
            src_i += repeat * blk
    return bytes(dest)


def rle_decompress(source: bytes, blk: int) -> bytes:
    src_i, dest = 0, bytearray()
    while src_i < len(source):
        op = source[src_i]
        count = op & 0x7F
        src_i += 1
        if op & 0x80:
            dest.extend(source[src_i:src_i + count * blk])
            src_i += count * blk
        else:
            dest.extend(source[src_i:src_i + blk] * count)
            src_i += blk
    return bytes(dest)


def encode_anim(display_frames: list[bytes], fps: int = FPS) -> bytes:
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
        W, H,
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


def decode_check(blob: bytes, display_frames: list[bytes]):
    """Round-trip sanity check of our own encoding."""
    sig, flags, w, h, fmt, fps, max_len, s_len, f_len, s_cnt, ff_cnt, df_cnt = \
        struct.unpack("<8sBBBBBHxIIIII", blob[:36])
    assert sig == b"bicycle0" and (w, h, fmt) == (W, H, 2) and df_cnt == len(display_frames)
    off = 36 + s_len
    out = []
    for _ in range(ff_cnt):
        enc, dur, ln = struct.unpack("<BBH", blob[off:off + 4])
        off += 4
        data = blob[off:off + ln]
        off += ln
        raw = rle_decompress(data, 4) if enc == 1 else data
        assert len(raw) == W * H * 4
        out.extend([raw] * dur)
    assert out == display_frames, "roundtrip mismatch"


ANIMS = {
    "work.anim": anim_working,
    "think.anim": anim_thinking,
    "done.anim": anim_complete,
    "wait.anim": anim_wait,
    "error.anim": anim_error,
    "idle.anim": anim_idle,
}


def main():
    import pathlib
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for fname, gen in ANIMS.items():
        frames = gen()
        blob = encode_anim(frames)
        decode_check(blob, frames)
        (out / fname).write_bytes(blob)
        print(f"{fname}: {len(frames)} frames, {len(blob)} bytes")


if __name__ == "__main__":
    main()
