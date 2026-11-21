"""Writes the Azel icon SVGs and renders them to PNG with headless Chrome.

    python3 assistant/design/icon/make_icons.py          # SVGs + PNGs
    python3 assistant/design/icon/make_icons.py --svg    # SVGs only

The jar outline is the DESIGN.md §6.2 formula, so icon and in-app visualizer match.
"""
import math, pathlib, subprocess, sys, tempfile

OUT = pathlib.Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def jar_path(cx, cy, r, n=180):
    pts = []
    for i in range(n):
        t = i / n * 2 * math.pi
        k = 1 - 0.06 * math.sin(t) - 0.035 * math.cos(2 * t) + 0.015 * math.sin(3 * t + 0.6)
        pts.append((cx + r * k * math.cos(t), cy + r * k * math.sin(t)))
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"


def jar_with_lip(cx, cy, r, lip=1.0, n=240):
    """One closed outline: the §6.2 body cut flat near top and bottom, with a short wide
    mouth and a low foot ring stepped in, so it reads as a moon jar and not an egg."""
    # lip > 1 exaggerates mouth and foot for tiny sizes (menu bar)
    yt, yb = cy - r * (0.9 - 0.06 * (lip - 1)), cy + r * (0.88 - 0.06 * (lip - 1))
    mw, fw, mh, fh = r * 0.66, r * 0.7, r * 0.16 * lip, r * 0.14 * lip
    pts, foot_done, mouth_done = [], False, False
    for i in range(n + 1):  # clockwise on screen (y down), starting at the right
        t = i / n * 2 * math.pi
        k = 1 - 0.06 * math.sin(t) - 0.035 * math.cos(2 * t) + 0.015 * math.sin(3 * t + 0.6)
        x, y = cx + r * k * math.cos(t), cy + r * k * math.sin(t)
        if y > yb:
            if not foot_done:
                pts += [(cx + fw / 2, yb), (cx + fw / 2 - r * 0.03, yb + fh),
                        (cx - fw / 2 + r * 0.03, yb + fh), (cx - fw / 2, yb)]
                foot_done = True
            continue
        if y < yt:
            if not mouth_done:
                pts += [(cx - mw / 2, yt), (cx - mw / 2 - r * 0.03, yt - mh),
                        (cx + mw / 2 + r * 0.03, yt - mh), (cx + mw / 2, yt)]
                mouth_done = True
            continue
        pts.append((x, y))
    return "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in pts) + " Z"


def opening(cx, cy, r):
    """The mouth's opening seen slightly from above: (cx, cy, rx, ry)."""
    return cx, cy - r * 1.06, r * 0.33, r * 0.045


def art(cx, cy, r, mode):
    """The jar, its glow, the mouth opening and the water-line reflection. mode: color | dark | tinted."""
    jar = jar_with_lip(cx, cy, r)
    ox, oy, orx, ory = opening(cx, cy, r)
    hole_c = {"color": "#A9C7BA", "dark": "#7FB5A3", "tinted": "#7A7A7A"}[mode]
    hole = f'<ellipse cx="{ox}" cy="{oy:.1f}" rx="{orx:.1f}" ry="{ory:.1f}" fill="{hole_c}"/>'
    refl = f'<ellipse cx="{cx}" cy="{cy + r * 1.27:.1f}" rx="{r * 0.62:.1f}" ry="{r * 0.075:.1f}" fill="url(#refl)"/>'
    body = {"color": "url(#porcelain)", "dark": "url(#porcelainDark)", "tinted": "url(#gray)"}[mode]
    glow_c = {"color": "#E9F6EF", "dark": "#82CBB4", "tinted": "#FFFFFF"}[mode]
    return f"""
  <path d="{jar}" fill="{glow_c}" opacity="{0.45 if mode != 'tinted' else 0.25}" filter="url(#glow)"/>
  <path d="{jar}" fill="{body}"/>
  <path d="{jar}" fill="url(#shade)"/>
  {hole}
  {refl if mode != 'tinted' else ''}"""


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
    <radialGradient id="refl"><stop offset="0" stop-color="#F3EBDC" stop-opacity="0.55"/><stop offset="1" stop-color="#F3EBDC" stop-opacity="0"/></radialGradient>
    <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="34"/></filter>
    <filter id="mshadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="12" stdDeviation="14" flood-color="#000" flood-opacity="0.35"/></filter>
  </defs>"""


def svg(w, h, inner):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{DEFS}{inner}\n</svg>\n'


FILES = {
    # iOS: full-bleed square, the system applies the mask. Jar ~58% of the canvas.
    "azel-ios.svg": svg(1024, 1024, '<rect width="1024" height="1024" fill="url(#jade)"/>' + art(512, 482, 290, "color")),
    # iOS 18 dark: transparent background, the system supplies the dark backdrop.
    "azel-ios-dark.svg": svg(1024, 1024, art(512, 482, 290, "dark")),
    # iOS 18 tinted: grayscale on transparent; the system tints by luminance.
    "azel-ios-tinted.svg": svg(1024, 1024, art(512, 482, 290, "tinted")),
    # macOS: 824 pt body on the 1024 grid (100 pt margin), continuous corners, drop shadow.
    "azel-macos.svg": svg(1024, 1024,
        '<clipPath id="body"><rect x="100" y="100" width="824" height="824" rx="185"/></clipPath>'
        '<rect x="100" y="100" width="824" height="824" rx="185" fill="url(#jade)" filter="url(#mshadow)"/>'
        f'<g clip-path="url(#body)">{art(512, 488, 235, "color")}</g>'),
}

# Menu-bar templates: black + alpha only (macOS recolors them). Outline when idle,
# filled while a call is up (DESIGN.md §8.1).
def template(size, filled):
    k = size / 18
    jar = jar_with_lip(9 * k, 9.4 * k, 6.4 * k, lip=2.0)
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
