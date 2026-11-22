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


def jar_field(n, size):
    """Per-pixel (signed distance to the jar edge in px, outward unit vector) on an n x n canvas, supersampled x2."""
    r = size / 2 * 0.86
    dx, dy = optical_offset()
    cx, cy = n / 2 - dx * r, n / 2 - dy * r
    W = n * 2
    y, x = np.mgrid[0:W, 0:W]
    px, py = (x + 0.5) / W * n - cx, (y + 0.5) / W * n - cy
    t = np.arctan2(py, px)
    edge = r * (1 - 0.06 * np.sin(t) - 0.035 * np.cos(2 * t) + 0.015 * np.sin(3 * t + 0.6))
    dist = np.hypot(px, py)
    return W, edge, dist - edge, px / np.maximum(dist, 1e-6), py / np.maximum(dist, 1e-6), r


def down(a, n):
    return a.reshape(n, a.shape[0] // n, n, a.shape[1] // n, -1).mean(axis=(1, 3))


def save_rgba(rgb, alpha, name):
    img = np.dstack([rgb, alpha * 255])
    Image.fromarray(np.round(down(img, rgb.shape[0] // 2)).astype(np.uint8), "RGBA").save(OUT / name, optimize=True)


def orb_images(n=150):
    """orb_body: neutral porcelain jar, white to #F3EBDC radial. orb_rim: the same outline, white with alpha only
    toward the lower-right rim (blended with the persona glaze). Header uses them scaled to 64."""
    W, edge, d, ux, uy, r = jar_field(n, n)
    inside = np.clip(0.5 - d / 2, 0, 1)
    yy, xx = np.mgrid[0:W, 0:W]
    pxn, pyn = (xx + 0.5) / W * n, (yy + 0.5) / W * n
    c = n / 2
    kk = np.clip(np.hypot(pxn - (c - 0.18 * r), pyn - (c - 0.22 * r)) / (1.2 * r), 0, 1)
    top, bot = np.array([255, 255, 255.0]), np.array([0xF3, 0xEB, 0xDC])
    rgb = top * (1 - kk[..., None]) + bot * kk[..., None]
    save_rgba(rgb, inside, "orb_body.png")
    depth = np.clip(-d / (0.22 * r), 0, 1)  # 0 at the edge, 1 deep inside
    lower_right = np.clip(0.5 + 0.5 * (ux * 0.72 + uy * 0.72), 0, 1) ** 1.4
    rim = (1 - depth) ** 1.6 * lower_right * 0.9 * inside
    save_rgba(np.full((W, W, 3), 255.0), rim, "orb_rim.png")


def light_image(w=64, h=54):
    y, x = np.mgrid[0:h * 2, 0:w * 2]
    d = np.hypot((x + 0.5) / (w * 2) - 0.5, ((y + 0.5) / (h * 2) - 0.5) * (w / h)) / 0.5
    a = np.clip(1 - d, 0, 1) ** 2 * 0.75
    img = np.dstack([np.full((h * 2, w * 2, 3), 255.0), a * 255])
    Image.fromarray(np.round(img.reshape(h, 2, w, 2, 4).mean(axis=(1, 3))).astype(np.uint8), "RGBA").save(OUT / "orb_light.png", optimize=True)


def channel_art():
    """The channel icons and splash from the app icon artwork (design/icon/AppIcon-1024.png): the same green ground
    and porcelain jar, centred, no floor shadow. The ground is the icon's own gradient rebuilt at any aspect ratio
    (a quadratic fit to its outer pixels); the jar and its soft halo are cut from the icon with a feathered round mask."""
    src = Image.open(HERE.parent.parent / "design" / "icon" / "AppIcon-1024.png").convert("RGB")
    a = np.asarray(src).astype(float)
    # the ground: a quadratic surface fitted to the icon's pixels outside the jar's halo, so it matches the crop
    gy, gx = np.mgrid[0:1024, 0:1024]
    far = np.hypot(gx - 512, gy - 512) > 440
    gu, gv = gx[far] / 1023.0, gy[far] / 1023.0
    basis = lambda u, v: np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=-1)  # noqa: E731
    coef = np.linalg.lstsq(basis(gu, gv), a[far], rcond=None)[0]
    side = 920  # source square around the jar and its halo
    crop = src.crop((512 - side // 2, 512 - side // 2 + 8, 512 + side // 2, 512 + side // 2 + 8))

    def make(w, h, orb_h, name):
        yy, xx = np.mgrid[0:h, 0:w]
        u, v = xx / (w - 1), yy / (h - 1)
        ground = np.clip(basis(u, v) @ coef, 0, 255)
        out = Image.fromarray(np.round(ground).astype(np.uint8), "RGB")
        L = round(orb_h / (620 / side))  # the jar is about 620 px tall in the source
        piece = crop.resize((L, L), Image.LANCZOS)
        ys, xs = np.mgrid[0:L, 0:L]
        d = np.hypot((xs + 0.5) / L - 0.5, (ys + 0.5) / L - 0.5) / 0.5
        mask = np.clip((1 - d) / 0.22, 0, 1)  # opaque over the jar and its halo, feathered only in the empty ground
        out.paste(piece, ((w - L) // 2, (h - L) // 2), Image.fromarray(np.round(mask * 255).astype(np.uint8), "L"))
        out.save(OUT / name, optimize=True)

    make(336, 210, 150, "icon_hd.png")
    make(540, 405, 270, "icon_fhd.png")
    make(248, 140, 100, "icon_sd.png")
    make(1280, 720, 300, "splash_hd.png")
    make(1920, 1080, 450, "splash_fhd.png")


def tri_image(w=16, h=14):
    """The stock card's up arrow (white, alpha; the card blends the colour and rotates it 180° for down): the subset
    Pretendard has no ▲/▼ glyph."""
    W, H = w * SS, h * SS
    y, x = np.mgrid[0:H, 0:W]
    x, y = (x + 0.5) / SS, (y + 0.5) / SS
    inside = (y <= h) & (np.abs(x - w / 2) <= (y / h) * (w / 2))
    alpha = inside.reshape(h, SS, w, SS).mean(axis=(1, 3))
    img = np.dstack([np.full((h, w, 3), 255.0), alpha * 255])
    Image.fromarray(np.round(img).astype(np.uint8), "RGBA").save(OUT / "tri.png", optimize=True)


def wx_images(n=48):
    """Weather icons for the TV (white + alpha; the card tints them): wx_clear, wx_cloudy, wx_overcast, wx_rain, wx_snow,
    wx_sleet, wx_shower. Plain shapes from circles and capsules, one weight, so they read as one family at 24-56 px."""
    N = n * SS
    y, x = np.mgrid[0:N, 0:N]
    x, y = (x + 0.5) / SS, (y + 0.5) / SS

    def disc(cx, cy, r):
        return (np.hypot(x - cx, y - cy) <= r).astype(float)

    def seg(x1, y1, x2, y2, w):
        dx, dy = x2 - x1, y2 - y1
        t = np.clip(((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy), 0, 1)
        return (np.hypot(x - (x1 + t * dx), y - (y1 + t * dy)) <= w / 2).astype(float)

    def cloud(ox=0.0, oy=0.0, k=1.0):
        c = lambda cx, cy, r: disc(ox + cx * k, oy + cy * k, r * k)  # noqa: E731
        base = seg(ox + 12 * k, oy + 30 * k, ox + 36 * k, oy + 30 * k, 12 * k)
        return np.maximum.reduce([c(17, 26, 8), c(26, 20, 11), c(35, 27, 7), base])

    def sun(cx, cy, r, rays=True):
        out = disc(cx, cy, r)
        if rays:
            for i in range(8):
                a = i * math.pi / 4
                out = np.maximum(out, seg(cx + math.cos(a) * (r + 4), cy + math.sin(a) * (r + 4),
                                          cx + math.cos(a) * (r + 9), cy + math.sin(a) * (r + 9), 3.2))
        return out

    def drops(xs, y0=37, y1=45):
        return np.maximum.reduce([seg(x0, y0, x0 - 3, y1, 3.2) for x0 in xs])

    def flakes(xs, yy=41):
        return np.maximum.reduce([disc(x0, yy, 2.6) for x0 in xs])

    up = lambda m, d: np.roll(m, -d * SS, axis=0)  # noqa: E731  lift a cloud to make room for rain under it
    shapes = {
        "clear": sun(24, 24, 9),
        "cloudy": np.maximum(sun(31, 15, 6) * (1 - cloud(-2, 6)), cloud(-2, 6)),
        "overcast": np.maximum(cloud(6, -2, 0.85) * 0.55 * (1 - cloud(-2, 6)), cloud(-2, 6)),
        "rain": np.maximum(up(cloud(), 6), drops((18, 26, 34))),
        "snow": np.maximum(up(cloud(), 6), flakes((17, 25, 33))),
        "sleet": np.maximum.reduce([up(cloud(), 6), drops((19, 33)), flakes((26,))]),
        "shower": np.maximum.reduce([sun(33, 12, 5, rays=False) * (1 - up(cloud(-2, 2), 4)), up(cloud(-2, 2), 4),
                                     drops((17, 25, 33))]),
    }
    for name, m in shapes.items():
        alpha = np.clip(m, 0, 1).reshape(n, SS, n, SS).mean(axis=(1, 3))
        img = np.dstack([np.full((n, n, 3), 255.0), alpha * 255])
        Image.fromarray(np.round(img).astype(np.uint8), "RGBA").save(OUT / f"wx_{name}.png", optimize=True)


def main():
    OUT.mkdir(exist_ok=True)
    tri_image()
    wx_images()
    nine("card.9.png", rrect_mask(24, 24, 8), (8, 16, 8, 16))
    nine("focus.9.png", rrect_mask(24, 24, 8, ring=4), (8, 16, 8, 16))
    Image.fromarray(np.full((2, 2, 4), 255, dtype=np.uint8), "RGBA").save(OUT / "rule.png")
    wrap = np.zeros((2, 1920, 4), dtype=np.uint8)  # the RowList wrap seam: the safe width, in the rule colour
    wrap[:, 96:1824] = (0x26, 0x31, 0x3A, 255)
    Image.fromarray(wrap, "RGBA").save(OUT / "rule_wrap.png", optimize=True)
    orb_images()
    light_image()
    channel_art()


if __name__ == "__main__":
    main()
