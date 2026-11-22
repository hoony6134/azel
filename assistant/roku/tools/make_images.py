"""Generates the channel's small images (run from anywhere): 9-patches for cards, the focus ring and pills, and the
porcelain orb (body + glow) from the same jar outline as the app icon (assistant/design/icon/make_icons.py).

    python3 assistant/roku/tools/make_images.py
Needs Pillow and numpy.
"""
import math
import pathlib
import sys

import numpy as np
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "design" / "icon"))
from make_icons import jar_radius, optical_offset  # noqa: E402

OUT = HERE.parent / "images"
SS = 4  # supersampling for edges


def rrect_mask(w, h, r, inset=0.0, ring=None):
    """Anti-aliased rounded-rect alpha (0..1). `ring`: stroke width, for a hollow ring."""
    W, H = w * SS, h * SS
    y, x = np.mgrid[0:H, 0:W]
    x, y = (x + 0.5) / SS, (y + 0.5) / SS
    cx, cy = w / 2, h / 2
    hw, hh = w / 2 - inset, h / 2 - inset
    qx, qy = np.abs(x - cx) - (hw - r), np.abs(y - cy) - (hh - r)
    d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r
    a = (d <= 0).astype(float)
    if ring:
        a = a * (d >= -ring)
    return a.reshape(h, SS, w, SS).mean(axis=(1, 3))


def nine(path, inner, stretch, pad=None):
    """`inner`: alpha (h,w). Adds the 1 px 9-patch border: black markers over `stretch` = (x0,x1,y0,y1) in inner px,
    and optional `pad` = (left, top, right, bottom) content padding markers on the right and bottom edges."""
    h, w = inner.shape
    img = np.zeros((h + 2, w + 2, 4), dtype=np.uint8)
    img[1:-1, 1:-1, :3] = 255
    img[1:-1, 1:-1, 3] = np.round(inner * 255)
    x0, x1, y0, y1 = stretch
    img[0, 1 + x0:1 + x1, :] = (0, 0, 0, 255)
    img[1 + y0:1 + y1, 0, :] = (0, 0, 0, 255)
    if pad:
        pl, pt, pr, pb = pad
        img[h + 1, 1 + pl:1 + w - pr, :] = (0, 0, 0, 255)
        img[1 + pt:1 + h - pb, w + 1, :] = (0, 0, 0, 255)
    Image.fromarray(img, "RGBA").save(OUT / path, optimize=True)


def orb(size, glow=False):
    """The jar-outlined orb. body: white to #F3EBDC radial; glow: soft white halo (alpha only), 1.4x the body."""
    n = round(size * (1.4 if glow else 1))
    r = size / 2 * 0.86
    dx, dy = optical_offset()
    cx, cy = n / 2 - dx * r, n / 2 - dy * r
    W = n * (2 if n > 200 else 3)
    y, x = np.mgrid[0:W, 0:W]
    px, py = (x + 0.5) / W * n - cx, (y + 0.5) / W * n - cy
    t = np.arctan2(py, px)
    edge = r * (1 - 0.06 * np.sin(t) - 0.035 * np.cos(2 * t) + 0.015 * np.sin(3 * t + 0.6))
    dist = np.hypot(px, py) - edge  # < 0 inside
    s = n / W
    if glow:
        a = np.clip(1 - np.maximum(dist, 0) / (0.34 * r), 0, 1) ** 2 * 0.7
        rgb = np.full((W, W, 3), 255.0)
    else:
        a = np.clip(0.5 - dist / s, 0, 1)
        k = np.clip(np.hypot(px + 0.25 * r, py + 0.3 * r) / (1.15 * r), 0, 1)
        top, bot = np.array([255, 255, 255.0]), np.array([0xF3, 0xEB, 0xDC])
        rgb = top * (1 - k[..., None]) + bot * k[..., None]
    img = np.dstack([rgb, a * 255])
    img = img.reshape(n, W // n, n, W // n, 4).mean(axis=(1, 3)) if W % n == 0 else \
        np.asarray(Image.fromarray(img.astype(np.uint8), "RGBA").resize((n, n), Image.LANCZOS)).astype(float)
    Image.fromarray(np.round(img).astype(np.uint8), "RGBA").save(OUT / f"orb_{'glow' if glow else 'body'}_{size}.png", optimize=True)


def main():
    OUT.mkdir(exist_ok=True)
    nine("card.9.png", rrect_mask(40, 40, 16), (16, 24, 16, 24))
    ring = rrect_mask(52, 52, 22, ring=6)
    nine("focus.9.png", ring, (22, 30, 22, 30), pad=(6, 6, 6, 6))
    nine("pill.9.png", rrect_mask(72, 72, 36), (36, 36 + 1, 36, 36 + 1))
    for size in (360, 72):
        orb(size)
        orb(size, glow=True)


if __name__ == "__main__":
    main()
