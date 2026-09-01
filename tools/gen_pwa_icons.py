#!/usr/bin/env python3
"""Generate surge PWA/app icons — pure-Python, zero dependencies.

Renders the surge mark (a rising ↗ arrow on the brand teal) at the sizes the
stores and PWA installers need, as anti-aliased RGBA PNGs, plus an SVG master.
This is a ONE-OFF generator: its output is committed under
src/surge/dashboard/static/pwa/ and the nightly export merely copies it, so the
pipeline never pays the raster cost. Re-run only when the mark changes:

    python tools/gen_pwa_icons.py
"""
from __future__ import annotations

import math
import pathlib
import struct
import zlib

# brand palette (matches export.py --accent / dark ground)
TEAL = (0x2E, 0x7D, 0x6B)
WHITE = (0xFF, 0xFF, 0xFF)
DARK = (0x10, 0x15, 0x1A)

# ↗ arrow in unit coords: shaft A→B, two barbs from the tip B
_A = (0.30, 0.70)
_B = (0.71, 0.29)
_BARB1 = (0.71, 0.50)
_BARB2 = (0.50, 0.29)
_SEGMENTS = ((_A, _B), (_B, _BARB1), (_B, _BARB2))
_STROKE = 0.058          # half-width of the arrow stroke (unit)
_CORNER = 0.185          # rounded-rect corner radius (unit) for the "any" icon


def _dist_seg(px: float, py: float, a: tuple, b: tuple) -> float:
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
    return math.hypot(dx, dy)


def _in_arrow(x: float, y: float) -> bool:
    return min(_dist_seg(x, y, a, b) for a, b in _SEGMENTS) <= _STROKE


def _in_rrect(x: float, y: float, r: float) -> bool:
    qx = abs(x - 0.5) - (0.5 - r)
    qy = abs(y - 0.5) - (0.5 - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0)
    return outside <= 0.0


def _render(size: int, *, maskable: bool = False, opaque: bool = False,
            ss: int = 3) -> bytes:
    """Return RGBA bytes (size*size*4). maskable/opaque ⇒ full-bleed square;
    otherwise a rounded-rect tile with transparent corners. `opaque` fills the
    corners with the dark ground (iOS ignores alpha on apple-touch-icon)."""
    buf = bytearray(size * size * 4)
    inv = 1.0 / size
    step = 1.0 / ss
    sub = [(i + 0.5) * step for i in range(ss)]
    nsub = ss * ss
    for py in range(size):
        for px in range(size):
            bg_hits = fg_hits = 0
            for oy in sub:
                yy = (py + oy) * inv
                for ox in sub:
                    xx = (px + ox) * inv
                    on_tile = True if (maskable or opaque) else _in_rrect(
                        xx, yy, _CORNER)
                    if not on_tile:
                        continue
                    bg_hits += 1
                    if _in_arrow(xx, yy):
                        fg_hits += 1
            i = (py * size + px) * 4
            if bg_hits == 0:
                if opaque:                       # corners → dark, fully opaque
                    buf[i:i + 4] = bytes((*DARK, 255))
                continue
            cov = bg_hits / nsub                 # tile coverage (for AA edges)
            fgc = fg_hits / nsub
            # composite: white arrow over teal tile
            r = TEAL[0] * (1 - fgc) + WHITE[0] * fgc
            g = TEAL[1] * (1 - fgc) + WHITE[1] * fgc
            b = TEAL[2] * (1 - fgc) + WHITE[2] * fgc
            a = 255 if opaque else round(255 * cov)
            if opaque:                           # blend tile edge over dark
                r = r * cov + DARK[0] * (1 - cov)
                g = g * cov + DARK[1] * (1 - cov)
                b = b * cov + DARK[2] * (1 - cov)
            buf[i:i + 4] = bytes((round(r), round(g), round(b), a))
    return bytes(buf)


def _png(size: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)                            # filter: none
        raw += rgba[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="512" height="512">
  <rect width="100" height="100" rx="18.5" fill="#2E7D6B"/>
  <path d="M30 70 L71 29 M71 29 L71 50 M71 29 L50 29"
        fill="none" stroke="#FFFFFF" stroke-width="11.6"
        stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


def main() -> None:
    out = pathlib.Path("src/surge/dashboard/static/pwa")
    out.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("icon-192.png", 192, dict()),
        ("icon-512.png", 512, dict()),
        ("icon-maskable-512.png", 512, dict(maskable=True)),
        ("apple-touch-icon-180.png", 180, dict(opaque=True)),
    ]
    for name, size, kw in jobs:
        data = _png(size, _render(size, **kw))
        (out / name).write_bytes(data)
        print(f"wrote {name} ({size}px, {len(data)} bytes)")
    (out / "icon.svg").write_text(_SVG, encoding="utf-8")
    print("wrote icon.svg")


if __name__ == "__main__":
    main()
