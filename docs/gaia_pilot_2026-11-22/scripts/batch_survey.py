"""Run tools/source_survey.py --via-solver (original, selective) on each frame of a list, one output folder per frame.

usage: batch_survey.py LIST OUT UNIT FIBRE_X FIBRE_Y   (LIST: frame paths relative to the unit's folder on the share)
"""
import subprocess, sys
from pathlib import Path

lst, out, unit, fx, fy = sys.argv[1:6]
share = rf"\\mast-ns-control\mast-share\MAST\{unit}"
py = r"C:\MAST\src\.venv\Scripts\python.exe"
tool = r"C:\MAST\src\unit\tools\source_survey.py"
frames = [ln.strip() for ln in Path(lst).read_text().splitlines() if ln.strip()]
for i, rel in enumerate(frames):
    dest = Path(out) / f"{i:02d}"
    if (dest / "survey.csv").exists():
        print(i, "done already", flush=True)
        continue
    path = share + "\\" + rel.replace("/", "\\")
    cmd = [py, tool, path, "--out", str(dest), "--via-solver", "--solver-modes", "original,selective",
           "--fibre-xy", fx, fy, "--hot-pixels", unit,
           "--param", "centroid_tolerance=0.002", "--param", "centroid_max_iterations=32",
           "--param", "min_sep_fwhm=1.5", "--param", "max_fwhm_ratio=2.5"]
    print(i, rel[:60], flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    (Path(out) / f"{i:02d}.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    print("   ->", r.returncode, (r.stdout.strip().splitlines() or [""])[-1][:100], flush=True)
