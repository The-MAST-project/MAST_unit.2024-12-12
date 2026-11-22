"""Clean stars (kept or spare) per frame inside a disk about the fibre pixel, and what the shadow took."""
import csv, glob, os, sys
import numpy as np
root, fx, fy = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
R = 0.25 * 5644
print("frame | shadow off px / umbra / penumbra | disk R=%.0f: kept+spare, shadow-rejected, other | kept+spare in 600 px, 1000 px | ROI 2000x400 and 1000x1000 about fibre: kept+spare" % R)
tot = []
for d in sorted(glob.glob(os.path.join(root, "[0-9][0-9]"))):
    try:
        sv = list(csv.DictReader(open(f"{d}/survey.csv")))[0]
        src = list(csv.DictReader(open(f"{d}/sources.csv")))
    except Exception:
        continue
    x = np.array([float(r["x_full"]) for r in src]); y = np.array([float(r["y_full"]) for r in src])
    fate = np.array([r["fate"] for r in src]); r_ = np.hypot(x - fx, y - fy)
    good = np.isin(fate, ["kept", "spare"])
    inside = r_ <= R
    n_good, n_sh, n_oth = int((good & inside).sum()), int(((fate == "shadow") & inside).sum()), int((~good & (fate != "shadow") & inside).sum())
    roi1 = good & (abs(x - fx) <= 1000) & (abs(y - fy) <= 200)
    roi2 = good & (abs(x - fx) <= 500) & (abs(y - fy) <= 500)
    tot.append((n_good, int((good & (r_ <= 600)).sum()), int((good & (r_ <= 1000)).sum())))
    print(f"{os.path.basename(d)} | {sv.get('shadow_offset_px','-'):>8} {sv.get('shadow_umbra_half_px','-'):>6} {sv.get('shadow_penumbra_half_px','-'):>6} | {n_good:3d} {n_sh:3d} {n_oth:3d} | {tot[-1][1]:3d} {tot[-1][2]:3d} | {int(roi1.sum()):3d} {int(roi2.sum()):3d}")
t = np.array(tot)
print("median over frames: kept+spare in disk %d, within 600 px %d, within 1000 px %d" % tuple(np.median(t, axis=0)))
