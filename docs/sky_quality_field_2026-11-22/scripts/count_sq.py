"""Replay focus_on_star step 7 (sky quality) on saved frames with several box sizes."""

import glob
import json
import os
import sys

import numpy as np
from astropy.io import fits

sys.path[:0] = ["top", "top/unit/src"]
from catalogs.catshtm import CatsHtm  # noqa: E402
from common.config.calibration import FocusOnStarSettings  # noqa: E402
from focus_on_star import sky_quality  # noqa: E402
from focus_on_star.star_selection import field_catalog, years_since  # noqa: E402
from imaging.fov import fov_box  # noqa: E402
from imaging.hfd import field_stars, measure_star  # noqa: E402

ls = FocusOnStarSettings()
SAT = 63000.0
FOVS = [10, 15, 20, 30]
cat = CatsHtm("/mnt/last0/LAST-CONTAINER/catalogs/GAIA/DR3", ls.catalog_name)

root = "/Storage/mast-share/MAST/mast03"
for d in sorted(glob.glob(f"{root}/2026-11-1*/Calibration/FocusOnStar/*")):
    if not os.path.exists(f"{d}/sky_quality.json"):
        continue
    run = json.load(open(f"{d}/focus_on_star.json"))
    sq = json.load(open(f"{d}/sky_quality.json"))
    frame = glob.glob(f"{d}/*SKYQUALITY.fits")[0]
    data = np.asarray(fits.getdata(frame), dtype=float)
    scale = sq["plate_scale_arcsec"] * sq["binning"]
    ny, nx = data.shape
    cx, cy = run["focus_center"]
    star = run["star"]
    near = (cx, cy)
    m = measure_star(data, near, search_radius=3 * ls.star_search_px / sq["binning"], max_value=SAT)
    label = "/".join(d.split("/")[-4:-3] + d.split("/")[-1:])
    if m is None:
        print(label, "star not found near fibre pixel")
        continue
    print(f"{label}  frame {nx}x{ny}  star G={star['mag']:.2f}  recorded: det={sq['detected_stars']} cat={sq['catalog_stars']} match={sq['matched_stars']}")
    for fov in FOVS:
        box = fov_box((ny, nx), (cx, cy), fov, sq["binning"])
        x0, x1, y0, y1 = box
        stars, _ = field_stars(data, box, max_value=SAT)
        r = 0.5 * float(np.hypot(x1 - x0, y1 - y0)) * scale / 3600.0
        ra, dec, mag = field_catalog(
            cat,
            ra_deg=star["ra_deg"],
            dec_deg=star["dec_deg"],
            radius_deg=r,
            mag_column=ls.mag_column,
            faintest_mag=sky_quality.FAINTEST_G,
            years_of_proper_motion=years_since(ls.catalog_epoch),
        )
        f = sky_quality.measure(
            m, star["ra_deg"], star["dec_deg"], stars, ra, dec, mag,
            scale_arcsec=scale, exposure_seconds=sq["exposure_seconds"],
        )
        zp = f"{f.zero_point_mag:.2f}+-{f.zero_point_spread_mag or 0:.2f}" if f.zero_point_mag else "none"
        print(
            f"   {fov:>2}%  box {x1 - x0}x{y1 - y0}  catalog {f.catalog_stars:3d}  detected {f.detected_stars:3d}  "
            f"matched {f.matched_stars:3d}  zp stars {f.zero_point_stars:3d}  zp {zp}"
        )
