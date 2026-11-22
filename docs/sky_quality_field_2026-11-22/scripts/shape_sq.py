"""Per-star shape (adaptive moments) against radius from the fibre pixel, 30% box."""
import glob, json, os, sys
import numpy as np
from astropy.io import fits
sys.path[:0] = ["top", "top/unit/src"]
from common.config.calibration import FocusOnStarSettings
from imaging.fov import fov_box
from imaging.hfd import field_stars, measure_star

ls = FocusOnStarSettings(); SAT = 63000.0; FOV = int(sys.argv[1]) if len(sys.argv) > 1 else 30
H = 24

def adaptive(stamp, xc, yc):
    yy, xx = np.mgrid[0:stamp.shape[0], 0:stamp.shape[1]].astype(float)
    cov = np.array([[9.0, 0], [0, 9.0]]); x0, y0 = xc, yc
    for _ in range(25):
        try: inv = np.linalg.inv(cov)
        except np.linalg.LinAlgError: return None
        dx, dy = xx - x0, yy - y0
        w = np.exp(-0.5 * (inv[0, 0]*dx*dx + 2*inv[0, 1]*dx*dy + inv[1, 1]*dy*dy))
        f = stamp * w; s = f.sum()
        if s <= 0: return None
        x1 = (f * xx).sum() / s; y1 = (f * yy).sum() / s
        dx, dy = xx - x1, yy - y1
        mxx = (f*dx*dx).sum()/s; myy = (f*dy*dy).sum()/s; mxy = (f*dx*dy).sum()/s
        new = 2 * np.array([[mxx, mxy], [mxy, myy]])
        if not np.all(np.isfinite(new)) or np.linalg.det(new) <= 0.05: return None
        done = abs(x1 - x0) < 0.01 and abs(y1 - y0) < 0.01 and np.allclose(new, cov, rtol=1e-3)
        cov, x0, y0 = new, x1, y1
        if done: break
    ev, evec = np.linalg.eigh(cov); a, b = ev[1], ev[0]
    major = evec[:, 1]; phi = np.arctan2(major[1], major[0])
    return (a - b) / (a + b), 2.355 * (a * b) ** 0.25 / np.sqrt(2) * np.sqrt(2) / 1.0, phi, x0, y0

rows = []
for d in sorted(glob.glob("/Storage/mast-share/MAST/mast03/2026-11-1*/Calibration/FocusOnStar/*")):
    if not os.path.exists(f"{d}/sky_quality.json"): continue
    run = json.load(open(f"{d}/focus_on_star.json")); sq = json.load(open(f"{d}/sky_quality.json"))
    data = np.asarray(fits.getdata(glob.glob(f"{d}/*SKYQUALITY.fits")[0]), dtype=float)
    ny, nx = data.shape; cx, cy = run["focus_center"]
    box = fov_box((ny, nx), (cx, cy), FOV, 1)
    stars, noise = field_stars(data, box, max_value=SAT)
    lab = d.split("/")[-4][5:] + "/" + d.split("/")[-1]
    keep = []
    for s in stars:
        if s.saturated or s.peak < 12 * noise: continue
        xi, yi = int(round(s.x)), int(round(s.y))
        if xi < H or yi < H or xi >= nx - H or yi >= ny - H: continue
        st = data[yi-H:yi+H+1, xi-H:xi+H+1].copy()
        border = np.concatenate([st[0], st[-1], st[1:-1, 0], st[1:-1, -1]]); st -= np.median(border)
        r = adaptive(st, H + s.x - xi, H + s.y - yi)
        if r is None: continue
        e, fw, phi, _, _ = r
        rx, ry = s.x - cx, s.y - cy; rad = np.hypot(rx, ry)
        dphi = phi - np.arctan2(ry, rx)
        keep.append((rad, e, fw, np.cos(2 * dphi)))
    k = np.array(keep)
    if len(k) == 0: print(lab, "no stars"); continue
    cen = np.median(k[k[:, 0] < 400, 2]) if (k[:, 0] < 400).any() else np.median(k[:, 2])
    print(f"{lab}: {len(k)} stars (peak > 12 sigma), FWHM near centre {cen*0.2616:.2f}\"")
    for row in k: rows.append((lab, row[0], row[1], row[2] / cen, row[3]))
a = np.array([(r[1], r[2], r[3], r[4]) for r in rows])
print("\npooled", len(a), "stars; columns: median ellipticity, median FWHM/central, mean cos(2*dphi) (+1 radial, -1 tangential)")
edges = [0, 400, 700, 1000, 1300, 1800]
for lo, hi in zip(edges[:-1], edges[1:]):
    s = a[(a[:, 0] >= lo) & (a[:, 0] < hi)]
    if len(s) > 2: print(f"  r {lo:4d}-{hi:4d} px: n={len(s):3d}  e {np.median(s[:,1]):.3f}  FWHM/c {np.median(s[:,2]):.2f}  radial alignment {s[:,3].mean():+.2f} (sem {s[:,3].std()/np.sqrt(len(s)):.2f})")
p = np.polyfit(a[:, 0], a[:, 1], 1); print(f"  ellipticity slope {p[0]*1000:+.3f} per 1000 px")
p = np.polyfit(a[:, 0], a[:, 2], 1); print(f"  FWHM/central slope {p[0]*1000:+.3f} per 1000 px")
