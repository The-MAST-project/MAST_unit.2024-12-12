# Solver drift tests

These guard the fragile binned/full-frame coordinate surface
(`solvers/pixel_grid.py`, `solvers/COORDINATE_SURFACE.md`). There is no CI, so
run them by hand after touching `pixel_grid.py`, the `mastrometry.py`
downsample/crop/refpix logic, or the solve-field flags.

## Two tiers

| File | Needs astrometry.net? | What it catches |
|---|---|---|
| `test_pixel_grid.py` | **No** — runs anywhere | The convention itself: naive `orig/factor`, integer-division `refpix`, the off-by-(f-1)/2f error. This is the primary canary. |
| `test_equivalence_integration.py` | **Yes** — skipped otherwise | Drift in the numpy kernel, the solve-field version/behavior, or the conversion end-to-end. Re-runs the equivalence study and asserts sub-arcsecond agreement. |

## Running

Pure-math tests (always):

```
pytest src/solvers/tests/test_pixel_grid.py -v
```

Integration test — only on a machine with astrometry.net. Point it at your
setup (these are the defaults for the dev unit):

```
# bash
MAST_SOLVE_FIELD="C:/cygwin64/usr/local/astrometry/bin/solve-field" \
MAST_INDEX_DIR="D:\mast-indexes" \
MAST_TEST_FITS="C:\MAST\full-frame.fits" \
pytest src/solvers/tests/test_equivalence_integration.py -v -s
```

```
# PowerShell
$env:MAST_SOLVE_FIELD="C:/cygwin64/usr/local/astrometry/bin/solve-field"
$env:MAST_INDEX_DIR="D:\mast-indexes"
$env:MAST_TEST_FITS="C:\MAST\full-frame.fits"
pytest src/solvers/tests/test_equivalence_integration.py -v -s
```

If any of the three is missing the test reports *skipped* with the reason, not a
failure. `-s` shows the per-point separations.

## Known gap / future work

The integration test replicates the numpy downsample kernel standalone (as the
original study did) to avoid the RAM-disk / `Filer` / unit-config plumbing
needed to drive the real `MastrometryDotNet`. The fragile part — the coordinate
conversion — is the real `pixel_grid` code.

The highest-value addition when ROI is implemented in-house: an **end-to-end ROI
test** that drives the real class, sets `--crpix-x/--crpix-y` via
`roi_center_to_crpix`, and asserts the solved `CRVAL` lands on the same sky point
the full-frame solution gives for that ROI-center pixel — directly exercising the
refpix path that the spec/fiber pointing depends on.
