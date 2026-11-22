"""astrometry.net's own star positions (original mode's .corr: image2xy) against our fixed-window centroids, same stars.

usage: extractor_vs_fixed_window.py FRAMES_DIR FIBRE_X FIBRE_Y   (FRAMES_DIR: a folder of frame folders, as the survey leaves them)
Needs MAST_unit's src on PYTHONPATH (solvers.pixel_grid). Prints the median difference, image2xy minus fixed-window.
"""
import csv, glob, os, sys, warnings
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from scipy.spatial import cKDTree
from solvers.pixel_grid import grid_to_orig_fits, orig_to_grid_fits

root, fx, fy = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
warnings.simplefilter("ignore")
rows = []
for d in sorted(glob.glob(os.path.join(root, "[0-9][0-9]"))):
    try:
        src = [r for r in csv.DictReader(open(glob.glob(d + "/*sources.csv")[0])) if r["fate"] in ("kept", "spare")]
        solved = glob.glob(glob.glob(d + "/*-original")[0] + "/*,solver=*.fits")[0]
        wcs = WCS(fits.getheader(solved, 0))
        table = fits.getdata(solved[:-5] + ".corr", 1)
    except Exception:
        continue
    ox = np.asarray(grid_to_orig_fits(np.asarray(table["field_x"], float), 2)) - 1.0  # image2xy, full frame, 0-based
    oy = np.asarray(grid_to_orig_fits(np.asarray(table["field_y"], float), 2)) - 1.0
    fxw = np.array([float(r["x_full"]) for r in src]); fyw = np.array([float(r["y_full"]) for r in src])
    dist, idx = cKDTree(np.column_stack([fxw, fyw])).query(np.column_stack([ox, oy]))
    taken = {}
    for k in np.argsort(dist):
        if dist[k] <= 4.0 and idx[k] not in taken:
            taken[idx[k]] = k
    def sky(x, y):
        return wcs.all_pix2world([orig_to_grid_fits(x + 1.0, 2)], [orig_to_grid_fits(y + 1.0, 2)], 1)
    for j, k in taken.items():
        ra0, dec0 = sky(ox[k], oy[k]); ra1, dec1 = sky(fxw[j], fyw[j])
        rows.append((os.path.basename(d), np.hypot(ox[k] - fx, oy[k] - fy), ox[k] - fxw[j], oy[k] - fyw[j],
                     (ra0[0] - ra1[0]) * np.cos(np.radians(dec0[0])) * 3600, (dec0[0] - dec1[0]) * 3600))
a = np.array([r[1:] for r in rows]); frames = sorted({r[0] for r in rows})
print(f"{len(a)} stars used by astrometry.net's own fit, paired to a clean star of ours within 4 px, in {len(frames)} frames")
print("image2xy minus fixed-window: median dx, dy (full-frame px) and (east, north) arcsec, with the star-to-star spread (1.48 MAD)")
for label, m in (("all", a[:, 0] >= 0), ("within 600 px", a[:, 0] < 600), ("within 1000 px", a[:, 0] < 1000),
                 ("1000-2000 px", (a[:, 0] >= 1000) & (a[:, 0] < 2000)), ("beyond 2000 px", a[:, 0] >= 2000)):
    x = a[m]
    if len(x) < 5: continue
    med = np.median(x, axis=0); sd = 1.4826 * np.median(np.abs(x - med), axis=0)
    print(f"  {label:<15} n={len(x):5d}  dx {med[1]:+.2f} dy {med[2]:+.2f} px = ({med[3]:+.2f} E, {med[4]:+.2f} N) arcsec  [spread {sd[3]:.2f}, {sd[4]:.2f}]")
per = []
for f in frames:
    x = np.array([r[1:] for r in rows if r[0] == f and r[1] < 1000])
    if len(x) >= 5: per.append(np.median(x, axis=0)[[3, 4]])
per = np.array(per)
print(f"per frame, stars within 1000 px: median over {len(per)} frames ({np.median(per[:,0]):+.2f} E, {np.median(per[:,1]):+.2f} N) arcsec, frame-to-frame spread ({per[:,0].std():.2f}, {per[:,1].std():.2f})")
