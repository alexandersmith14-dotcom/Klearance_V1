"""Generate the favicon and home-screen icons.

Run after changing the branding, same as make_og_image.py. Output is committed
so the published page can reference it.

v4, 2026-08-30: use Kaufman Rossin's actual "K|R" monogram as the installed-app
icon, per the firm's real logo -- a KR-blue field, white K and R, split by a
thin lime pipe. The prior version (v3) rendered only "K|" because a single
product name has no second word to split; the firm now wants the app icon to
read as the KR brand mark itself, not a Klearance-specific variant.
"""
import json

from PIL import Image, ImageDraw, ImageFont

PRODUCT_NAME = "Klearance"

# The Kaufman Rossin monogram: one initial per word of the firm name, split by
# a lime pipe. Used verbatim as the app icon regardless of product name.
LETTERS = ("K", "R")

# Bump alongside the matching ?v= in dashboard.py's <link> tags whenever the
# icon artwork changes -- Android's "Install app" flow mints a WebAPK via a
# Google server that caches the icon server-side once fetched, so a plain
# redeploy isn't enough to make it refetch. The manifest's own icon srcs need
# the same cache-buster, not just the HTML <link> tags, since the WebAPK
# minting service reads icon paths from the manifest.
ICON_VERSION = 4

KR_BLUE = (30, 76, 126)         # Kaufman Rossin logo blue (#1e4c7e)
WHITE = (255, 255, 255)
GREEN = (174, 209, 54)          # KR lime (#aed136)

CAP_FRACTION = 0.50       # max letter height, as a share of the field
MAX_W_FRACTION = 0.74     # max width of the whole K|R group, as a share of the field
FONT_PATH = "C:/Windows/Fonts/arialbd.ttf"
PIPE_W_FRACTION = 0.038   # pipe stroke width, as a share of the field
PIPE_H_FRACTION = 0.92    # pipe height, as a share of the letters' own height
PIPE_GAP_FRACTION = 0.26  # gap between a letter and the pipe, as a share of letter height


def render(size, padding=0.0):
    """One icon. `padding` insets the artwork for maskable (croppable) icons."""
    img = Image.new("RGB", (size, size), KR_BLUE)
    d = ImageDraw.Draw(img)

    inset = round(size * padding)
    inner = size - 2 * inset

    # Binary-search the largest font size that fits BOTH bounds: letter ink
    # height <= CAP_FRACTION of the field, and the full K|R group width <=
    # MAX_W_FRACTION. Two wide letters plus a pipe are width-bound, not
    # height-bound, so height alone would overflow the edges.
    def group_metrics(f):
        a = d.textbbox((0, 0), LETTERS[0], font=f)
        b = d.textbbox((0, 0), LETTERS[1], font=f)
        wa, ha = a[2] - a[0], a[3] - a[1]
        wb, hb = b[2] - b[0], b[3] - b[1]
        th = max(ha, hb)
        pw = max(1, round(inner * PIPE_W_FRACTION))
        gp = th * PIPE_GAP_FRACTION
        return wa + gp + pw + gp + wb, th

    lo, hi = 1, size * 2
    font = ImageFont.truetype(FONT_PATH, 1)
    while lo <= hi:
        mid = (lo + hi) // 2
        f = ImageFont.truetype(FONT_PATH, mid)
        gw, th = group_metrics(f)
        if th <= inner * CAP_FRACTION and gw <= inner * MAX_W_FRACTION:
            font, lo = f, mid + 1
        else:
            hi = mid - 1

    b0 = d.textbbox((0, 0), LETTERS[0], font=font)
    b1 = d.textbbox((0, 0), LETTERS[1], font=font)
    w0, h0 = b0[2] - b0[0], b0[3] - b0[1]
    w1, h1 = b1[2] - b1[0], b1[3] - b1[1]
    text_h = max(h0, h1)

    pipe_w = max(1, round(inner * PIPE_W_FRACTION))
    pipe_h = text_h * PIPE_H_FRACTION
    gap = text_h * PIPE_GAP_FRACTION
    group_w = w0 + gap + pipe_w + gap + w1

    gx = inset + (inner - group_w) / 2
    gy = inset + (inner - text_h) / 2

    d.text((gx - b0[0], gy - b0[1] + (text_h - h0) / 2), LETTERS[0], font=font, fill=WHITE)

    px0 = gx + w0 + gap
    py0 = gy + (text_h - pipe_h) / 2
    d.rectangle([px0, py0, px0 + pipe_w, py0 + pipe_h], fill=GREEN)

    rx = px0 + pipe_w + gap
    d.text((rx - b1[0], gy - b1[1] + (text_h - h1) / 2), LETTERS[1], font=font, fill=WHITE)
    return img


def main():
    written = []

    # Multi-size .ico still has the broadest support, and is what a browser
    # reaches for when no <link rel="icon"> matches.
    ico = render(48)
    ico.save("favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    written.append("favicon.ico")

    for size in (16, 32, 180, 192, 512):
        # 180 is Apple's home-screen size; iOS rounds the corners itself and
        # composites on black, so the artwork must stay full-bleed and opaque.
        name = "apple-touch-icon.png" if size == 180 else f"icon-{size}.png"
        render(size).save(name, "PNG", optimize=True)
        written.append(name)

    # Android masks icons to whatever shape the launcher uses and can crop up to
    # 20% off each edge. The padded variant keeps the mark inside that safe zone.
    render(512, padding=0.18).save("icon-maskable-512.png", "PNG", optimize=True)
    written.append("icon-maskable-512.png")

    manifest = {
        "name": "Regulatory update tracker — community banks & fintechs",
        "short_name": PRODUCT_NAME,
        "description": "Daily federal regulatory updates for community banks and "
                       "fintechs, in plain English.",
        # Relative, because the site is served from a subpath rather than a
        # domain root. An absolute "/" would break the installed app.
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        # Matches the icon's KR-blue field (the splash-screen colour a PWA
        # launch briefly shows). theme_color stays navy since that's the app's
        # real in-use browser-chrome colour, unrelated to the icon graphic.
        "background_color": "#1e4c7e",
        "theme_color": "#003b6a",
        "icons": [
            {"src": f"icon-192.png?v={ICON_VERSION}", "sizes": "192x192", "type": "image/png"},
            {"src": f"icon-512.png?v={ICON_VERSION}", "sizes": "512x512", "type": "image/png"},
            {"src": f"icon-maskable-512.png?v={ICON_VERSION}", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }
    with open("site.webmanifest", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    written.append("site.webmanifest")

    print("Wrote " + ", ".join(written))


if __name__ == "__main__":
    main()
