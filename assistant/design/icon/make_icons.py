"""Writes the Azel icon SVGs and renders them to PNG with headless Chrome.

    python3 assistant/design/icon/make_icons.py          # SVGs + PNGs
    python3 assistant/design/icon/make_icons.py --svg    # SVGs only

The orb outline is the DESIGN.md §6.2 jar(θ) formula, so icon and in-call orb match.
"""
import math, pathlib, subprocess, sys, tempfile

OUT = pathlib.Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def jar_radius(t):
    """DESIGN.md §6.2 jar(θ), the in-call orb's outline (y down)."""
    return 1 - 0.06 * math.sin(t) - 0.035 * math.cos(2 * t) + 0.015 * math.sin(3 * t + 0.6)


def jar_path(cx, cy, r, n=48):
    """Closed smooth outline: n samples of jar(θ) joined by Catmull-Rom → cubic Béziers,
    so there are no polygon facets at any size."""
    p = [(cx + r * jar_radius(t) * math.cos(t), cy + r * jar_radius(t) * math.sin(t))
         for t in (i / n * 2 * math.pi for i in range(n))]
    d = f"M{p[0][0]:.2f},{p[0][1]:.2f}"
    for i in range(n):
        p0, p1, p2, p3 = p[i - 1], p[i], p[(i + 1) % n], p[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f" C{c1[0]:.2f},{c1[1]:.2f} {c2[0]:.2f},{c2[1]:.2f} {p2[0]:.2f},{p2[1]:.2f}"
    return d + " Z"


def optical_offset(n=720):
    """(dx, dy) in units of r from the formula's origin to the orb's optical center: the
    midpoint of its bounding-box center and its area centroid (the shoulder makes it top-heavy)."""
    p = [(jar_radius(t) * math.cos(t), jar_radius(t) * math.sin(t)) for t in (i / n * 2 * math.pi for i in range(n))]
    a = gx = gy = 0.0
    for (x0, y0), (x1, y1) in zip(p, p[1:] + p[:1]):
        c = x0 * y1 - x1 * y0
        a, gx, gy = a + c, gx + (x0 + x1) * c, gy + (y0 + y1) * c
    gx, gy = gx / (3 * a), gy / (3 * a)
    bx = (min(x for x, _ in p) + max(x for x, _ in p)) / 2
    by = (min(y for _, y in p) + max(y for _, y in p)) / 2
    return (bx + gx) / 2, (by + gy) / 2


def art(cx, cy, r, mode):
    """The orb and its glow, optically centered on (cx, cy). mode: color | dark | tinted."""
    dx, dy = optical_offset()
    jar = jar_path(cx - dx * r, cy - dy * r, r)
    body = {"color": "url(#porcelain)", "dark": "url(#porcelainDark)", "tinted": "url(#gray)"}[mode]
    glow_c = {"color": "#E9F6EF", "dark": "#82CBB4", "tinted": "#FFFFFF"}[mode]
    return f"""
  <path d="{jar}" fill="{glow_c}" opacity="{0.45 if mode != 'tinted' else 0.25}" filter="url(#glow)"/>
  <path d="{jar}" fill="{body}"/>
  <path d="{jar}" fill="url(#shade)"/>"""


DEFS = """
  <defs>
    <linearGradient id="jade" x1="0" y1="0" x2="0.35" y2="1">
      <stop offset="0" stop-color="#4E9A85"/><stop offset="0.55" stop-color="#2F6F5F"/><stop offset="1" stop-color="#1E4A40"/>
    </linearGradient>
    <radialGradient id="porcelain" cx="0.38" cy="0.3" r="0.85">
      <stop offset="0" stop-color="#FFFFFF"/><stop offset="0.55" stop-color="#F3EBDC"/><stop offset="1" stop-color="#D9E8DF"/>
    </radialGradient>
    <radialGradient id="porcelainDark" cx="0.38" cy="0.3" r="0.85">
      <stop offset="0" stop-color="#FFFFFF"/><stop offset="0.5" stop-color="#F3EBDC"/><stop offset="1" stop-color="#9FD6C4"/>
    </radialGradient>
    <radialGradient id="gray" cx="0.38" cy="0.3" r="0.85">
      <stop offset="0" stop-color="#FFFFFF"/><stop offset="1" stop-color="#9A9A9A"/>
    </radialGradient>
    <linearGradient id="shade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0.3" stop-color="#1E4A40" stop-opacity="0"/><stop offset="1" stop-color="#1E4A40" stop-opacity="0.16"/>
    </linearGradient>
    <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="34"/></filter>
    <filter id="mshadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="12" stdDeviation="14" flood-color="#000" flood-opacity="0.35"/></filter>
  </defs>"""


def svg(w, h, inner):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{DEFS}{inner}\n</svg>\n'


FILES = {
    # iOS: full-bleed square, the system applies the mask. Orb ~58% of the canvas.
    "azel-ios.svg": svg(1024, 1024, '<rect width="1024" height="1024" fill="url(#jade)"/>' + art(512, 512, 300, "color")),
    # iOS 18 dark: transparent background, the system supplies the dark backdrop.
    "azel-ios-dark.svg": svg(1024, 1024, art(512, 512, 300, "dark")),
    # iOS 18 tinted: grayscale on transparent; the system tints by luminance.
    "azel-ios-tinted.svg": svg(1024, 1024, art(512, 512, 300, "tinted")),
    # macOS: 824 pt body on the 1024 grid (100 pt margin), continuous corners, drop shadow.
    "azel-macos.svg": svg(1024, 1024,
        '<clipPath id="body"><rect x="100" y="100" width="824" height="824" rx="185"/></clipPath>'
        '<rect x="100" y="100" width="824" height="824" rx="185" fill="url(#jade)" filter="url(#mshadow)"/>'
        f'<g clip-path="url(#body)">{art(512, 512, 245, "color")}</g>'),
}

# Menu-bar templates: black + alpha only (macOS recolors them). The plain orb: outline
# when idle, filled while a call is up (DESIGN.md §8.1).
def template(size, filled):
    k = size / 18
    jar = jar_path(9 * k, 9.3 * k, 7.0 * k)
    paint = 'fill="#000"' if filled else f'fill="none" stroke="#000" stroke-width="{1.5 * k:.2f}" stroke-linejoin="round"'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
            f'<path d="{jar}" {paint}/></svg>\n')

FILES["azel-menubar-template.svg"] = template(18, False)
FILES["azel-menubar-template-filled.svg"] = template(18, True)

RENDERS = [  # svg, png, size
    ("azel-ios.svg", "AppIcon-1024.png", 1024),
    ("azel-ios-dark.svg", "AppIcon-1024-dark.png", 1024),
    ("azel-ios-tinted.svg", "AppIcon-1024-tinted.png", 1024),
    ("azel-macos.svg", "AppIcon-macOS-1024.png", 1024),
    ("azel-menubar-template.svg", "MenuBarTemplate@2x.png", 36),
    ("azel-menubar-template.svg", "MenuBarTemplate.png", 18),
    ("azel-menubar-template-filled.svg", "MenuBarTemplateFilled@2x.png", 36),
    ("azel-menubar-template-filled.svg", "MenuBarTemplateFilled.png", 18),
]


def render(svg_name, png_name, size):
    win = max(size, 100)
    with tempfile.TemporaryDirectory() as tmp:
        page, shot = pathlib.Path(tmp, "p.html"), pathlib.Path(tmp, "shot.png")
        page.write_text(f'<html><body style="margin:0;background:transparent">'
                        f'<img src="{(OUT / svg_name).as_uri()}" width="{size}" height="{size}" '
                        f'style="display:block;margin:{(win - size) // 2}px"></body></html>')
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                        "--default-background-color=00000000", f"--window-size={win},{win}", f"--user-data-dir={tmp}",
                        f"--screenshot={shot}", page.as_uri()],
                       check=True, capture_output=True, cwd=tmp, timeout=60)
        # tiny headless windows hang, so small renders use a 100 px window, centered, and sips
        # crops the center
        subprocess.run(["sips", "-c", str(size), str(size), str(shot),
                        "--out", str(OUT / png_name)], check=True, capture_output=True)


if __name__ == "__main__":
    for name, text in FILES.items():
        (OUT / name).write_text(text)
    if "--svg" not in sys.argv:
        for args in RENDERS:
            render(*args)
            assert (OUT / args[1]).stat().st_size > 0, args[1]
    print("ok")
