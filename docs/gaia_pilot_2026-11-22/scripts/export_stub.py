"""On mast08: a light copy of survey frame folders (stub solved FITS = header only, .corr, CSVs) for the analyse tool."""
import glob, os, shutil, sys, zipfile
import numpy as np
from astropy.io import fits

root, dest = sys.argv[1], sys.argv[2]
tmp = dest + ".tmp"
shutil.rmtree(tmp, ignore_errors=True)
for d in sorted(glob.glob(os.path.join(root, "[0-9][0-9]"))):
    n = os.path.basename(d)
    out = os.path.join(tmp, n)
    os.makedirs(out)
    for f in glob.glob(os.path.join(d, "*-sources.csv")) + glob.glob(os.path.join(d, "survey.csv")):
        shutil.copy(f, os.path.join(out, os.path.basename(f)[-24:] if f.endswith("-sources.csv") else "survey.csv"))
    for sub in glob.glob(os.path.join(d, "*-original")) + glob.glob(os.path.join(d, "*-selective")):
        o = os.path.join(out, os.path.basename(sub)); os.makedirs(o)
        for solved in glob.glob(os.path.join(sub, "*,solver=*.fits")):
            hdr = fits.getheader(solved, 0)
            stem = "frame,solver=mastrometry.net"
            fits.PrimaryHDU(np.zeros((2, 2), np.float32), hdr).writeto(os.path.join(o, stem + ".fits"), overwrite=True)
            corr = solved[:-5] + ".corr"
            if os.path.exists(corr):
                shutil.copy(corr, os.path.join(o, stem + ".corr"))
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for dp, _, files in os.walk(tmp):
        for f in files:
            full = os.path.join(dp, f); z.write(full, os.path.relpath(full, tmp))
shutil.rmtree(tmp)
print("wrote", dest)
