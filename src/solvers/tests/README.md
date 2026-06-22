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

Integration test — only on a machine with astrometry.net. The sample frame is
bundled at `fixtures/full-frame.fits` via **git-lfs**, so run `git lfs pull`
once to materialize it (otherwise it's a small pointer file and the test skips).
The only thing not checked in is the index set on `D:\` (too large). Defaults
target the dev unit; override via env vars if your paths differ:

```
# bash — defaults (solve-field on PATH/cygwin, indexes on D:\, bundled fixture)
pytest src/solvers/tests/test_equivalence_integration.py -v -s

# override any of them
MAST_SOLVE_FIELD="C:/cygwin64/usr/local/astrometry/bin/solve-field" \
MAST_INDEX_DIR="D:\mast-indexes" \
MAST_TEST_FITS="C:\some\other.fits" \
pytest src/solvers/tests/test_equivalence_integration.py -v -s
```

```
# PowerShell — override example
$env:MAST_INDEX_DIR="D:\mast-indexes"
pytest src/solvers/tests/test_equivalence_integration.py -v -s
```

If solve-field, the index dir, or the fixture is missing the test reports
*skipped* with the reason, not a failure. `-s` shows the per-point separations.

## Coverage notes

`test_equivalence_integration.py` has two tests:

- `test_numpy_downsample_matches_native_downsample` — numpy pre-downsample vs
  `--downsample`, both CRPIX-center, agreement at center and corners.
- `test_offcenter_crpix_is_honored_and_lands_near_truth` — the ROI/spec path:
  an explicit fractional off-center `--crpix-x/--crpix-y` (from
  `roi_center_to_crpix`) is applied exactly, and `CRVAL` lands near truth. Its
  CRVAL bound is deliberately loose because off-center CRPIX re-anchors SIP
  (~0.5–1.5″ scatter — see `COORDINATE_SURFACE.md`, "Off-center CRPIX
  behavior"). A *tight* end-to-end CRVAL assertion is not possible: it would
  measure that scatter, not the convention. The convention is pinned exactly by
  `test_pixel_grid`.

Both integration tests replicate the numpy downsample kernel standalone (as the
original study did) to avoid the RAM-disk / `Filer` / unit-config plumbing needed
to drive the real `MastrometryDotNet`. The fragile part — the coordinate
conversion — is the real `pixel_grid` code.

## Future work

When ROI is implemented in-house, add an **end-to-end test driving the real
`MastrometryDotNet`** (RAM disk + unit config) so the production crop→downsample→
`roi_center_to_crpix`→solve path is exercised as a unit, not reconstructed here.
