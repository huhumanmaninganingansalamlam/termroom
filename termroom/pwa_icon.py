from __future__ import annotations

import binascii
import math
import struct
import zlib
from functools import lru_cache

PWA_ICON_VERSION = "1"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _distance_to_segment(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    vx = bx - ax
    vy = by - ay
    length_squared = vx * vx + vy * vy
    if length_squared == 0:
        return math.hypot(px - ax, py - ay)
    projection = ((px - ax) * vx + (py - ay) * vy) / length_squared
    projection = max(0.0, min(1.0, projection))
    cx = ax + projection * vx
    cy = ay + projection * vy
    return math.hypot(px - cx, py - cy)


@lru_cache(maxsize=4)
def termroom_png_icon(size: int) -> bytes:
    if size not in {180, 192, 512}:
        raise ValueError("Termroom PWA icon size must be 180, 192, or 512")

    scale = size / 512.0
    stroke_radius = 21.0 * scale
    accent = (0x8D, 0xA2, 0xFB)
    foreground = (0xF0, 0xF1, 0xF3)
    background = (0x11, 0x13, 0x18)
    arrow_segments = (
        (126 * scale, 151 * scale, 237 * scale, 256 * scale),
        (237 * scale, 256 * scale, 126 * scale, 361 * scale),
    )
    underline = (270 * scale, 361 * scale, 386 * scale, 361 * scale)

    rows = bytearray()
    for y in range(size):
        rows.append(0)
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            color = background
            if any(
                _distance_to_segment(px, py, *segment) <= stroke_radius
                for segment in arrow_segments
            ):
                color = accent
            if _distance_to_segment(px, py, *underline) <= stroke_radius:
                color = foreground
            rows.extend(color)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )
