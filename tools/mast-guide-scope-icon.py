"""Generate mast-guide-scope.ico: an oscilloscope front view, a green waveform on its screen, and a
rifle guide-scope reticle (ring + crosshair + ticks) overlaid on the display. Drawn supersampled for
clean edges, saved as a multi-size .ico."""

import math
import sys

from PIL import Image, ImageDraw

S = 1024  # supersample canvas


def pxv(v: float) -> int:
    return int(v * S / 256)


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# --- instrument body (the scope's front panel) ---
d.rounded_rectangle([pxv(14), pxv(30), pxv(242), pxv(226)], radius=pxv(24),
                    fill=(40, 52, 60, 255), outline=(96, 116, 128, 255), width=pxv(5))

# --- screen (CRT) ---
sx0, sy0, sx1, sy1 = pxv(32), pxv(48), pxv(224), pxv(166)
d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=pxv(10), fill=(5, 17, 9, 255), outline=(18, 42, 28, 255), width=pxv(3))

# grid
for i in range(1, 6):
    x = sx0 + (sx1 - sx0) * i / 6
    d.line([(x, sy0), (x, sy1)], fill=(16, 54, 34, 170), width=max(1, pxv(1)))
for i in range(1, 4):
    y = sy0 + (sy1 - sy0) * i / 4
    d.line([(sx0, y), (sx1, y)], fill=(16, 54, 34, 170), width=max(1, pxv(1)))

# --- green waveform (phosphor) ---
mid = (sy0 + sy1) / 2
amp = (sy1 - sy0) * 0.30
wave = [(sx0 + (sx1 - sx0) * k / 239, mid - amp * math.sin(2 * math.pi * 2 * (k / 239))) for k in range(240)]
d.line(wave, fill=(60, 255, 40, 255), width=pxv(4), joint="curve")

# --- rifle guide-scope reticle, centred on the screen ---
cx, cy = (sx0 + sx1) / 2, (sy0 + sy1) / 2
R = (sy1 - sy0) * 0.44
ret = (235, 238, 240, 235)
d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=ret, width=pxv(3))
d.line([(cx - R, cy), (cx + R, cy)], fill=ret, width=pxv(2))
d.line([(cx, cy - R), (cx, cy + R)], fill=ret, width=pxv(2))
for frac in (0.35, 0.6, 0.85):
    r = R * frac
    t = pxv(4)
    for ax, ay in ((0, -r), (0, r), (-r, 0), (r, 0)):
        d.line([(cx + ax - (t if ay else 0), cy + ay - (t if ax else 0)),
                (cx + ax + (t if ay else 0), cy + ay + (t if ax else 0))], fill=ret, width=pxv(2))
d.ellipse([cx - pxv(4), cy - pxv(4), cx + pxv(4), cy + pxv(4)], fill=(235, 70, 70, 255))

# --- knobs along the panel foot ---
for kx in (pxv(74), pxv(128), pxv(182)):
    d.ellipse([kx - pxv(15), pxv(188), kx + pxv(15), pxv(218)], fill=(26, 34, 40, 255), outline=(96, 116, 128, 255), width=pxv(3))
    d.line([(kx, pxv(194)), (kx, pxv(202))], fill=(190, 200, 210, 255), width=pxv(2))

base = img.resize((256, 256), Image.LANCZOS)
out = sys.argv[1] if len(sys.argv) > 1 else "mast-guide-scope.ico"
base.save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out)
