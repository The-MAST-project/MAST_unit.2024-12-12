"""Per matched star at 30%: zero point and peak/flux against radius from the fibre pixel."""
import glob, json, os, sys
import numpy as np
from astropy.io import fits
sys.path[:0] = ["top", "top/unit/src"]
from catalogs.catshtm import CatsHtm
from common.config.calibration import FocusOnStarSettings
from focus_on_star import sky_quality as sqm
from focus_on_star.star_selection import field_catalog, years_since
from imaging.fov import fov_box
from imaging.hfd import field_stars, measure_star

ls = FocusOnStarSettings(); SAT = 63000.0; FOV = int(sys.argv[1]) if len(sys.argv) > 1 else 30
cat = CatsHtm("/mnt/last0/LAST-CONTAINER/catalogs/GAIA/DR3", ls.catalog_name)
rows = []
for d in sorted(glob.glob("/Storage/mast-share/MAST/mast03/2026-11-1*/Calibration/FocusOnStar/*")):
    if not os.path.exists(f"{d}/sky_quality.json"): continue
    run = json.load(open(f"{d}/focus_on_star.json")); sq = json.load(open(f"{d}/sky_quality.json"))
    data = np.asarray(fits.getdata(glob.glob(f"{d}/*SKYQUALITY.fits")[0]), dtype=float)
    scale = sq["plate_scale_arcsec"]; ny, nx = data.shape; cx, cy = run["focus_center"]; star = run["star"]
    m = measure_star(data, (cx, cy), search_radius=3 * ls.star_search_px, max_value=SAT)
    if m is None: continue
    box = fov_box((ny, nx), (cx, cy), FOV, 1); x0, x1, y0, y1 = box
    stars, _ = field_stars(data, box, max_value=SAT)
    r = 0.5 * float(np.hypot(x1 - x0, y1 - y0)) * scale / 3600
    ra, dec, mag = field_catalog(cat, ra_deg=star["ra_deg"], dec_deg=star["dec_deg"], radius_deg=r,
        mag_column=ls.mag_column, faintest_mag=sqm.FAINTEST_G, years_of_proper_motion=years_since(ls.catalog_epoch))
    xi, eta = sqm.tangent_offsets_arcsec(star["ra_deg"], star["dec_deg"], ra, dec)
    mt = sqm.match_field([s.x - m.x for s in stars], [s.y - m.y for s in stars], xi, eta, scale)
    if mt is None: print(d[-22:], "no match"); continue
    lab = d.split("/")[-4][5:] + "/" + d.split("/")[-1]
    zps = []
    for c, k in mt.pairs:
        s = stars[k]
        if s.saturated or s.flux <= 0: continue
        zps.append((np.hypot(s.x - cx, s.y - cy), mag[c] + 2.5 * np.log10(s.flux / sq["exposure_seconds"]), s.peak / s.flux, mag[c]))
    z = np.array(zps); med = np.median(z[:, 1])
    print(f"{lab}: {len(z)} stars, median zp {med:.2f}")
    for rr, zp, pf, g in sorted(zps):
        rows.append((lab, rr, zp - med, pf, g))
        print(f"   r={rr:6.0f} px  G={g:5.2f}  zp-med={zp - med:+.2f}  peak/flux={pf:.4f}")
a = np.array([(r[1], r[2], r[3]) for r in rows])
print("\npooled", len(a), "stars")
for lo, hi in [(0, 800), (800, 1300), (1300, 1800), (1800, 5000)]:
    s = a[(a[:, 0] >= lo) & (a[:, 0] < hi)]
    if len(s): print(f"  r {lo:4d}-{hi:4d} px: n={len(s):2d}  zp-med mean {s[:,1].mean():+.3f} (median {np.median(s[:,1]):+.3f}, sd {s[:,1].std():.3f})  peak/flux median {np.median(s[:,2]):.4f}")
p = np.polyfit(a[:, 0], a[:, 1], 1); print(f"  zp slope {p[0]*1000:+.3f} mag per 1000 px")
p = np.polyfit(a[:, 0], a[:, 2], 1); print(f"  peak/flux slope {p[0]*1000:+.4f} per 1000 px (mean {a[:,2].mean():.4f})")
