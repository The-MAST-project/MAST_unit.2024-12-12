# MASTrometry coordinate surface

This note documents the one genuinely fragile thing in the MASTrometry solver,
why it exists, the bugs it has caused, and how the tests guard it.

## What it is

`MastrometryDotNet` does not hand the raw image to astrometry.net. It first
preprocesses the image in numpy:

1. (optionally) **crops** to an ROI, then
2. **downsamples** 2×2 (block-mean).

astrometry.net then solves that smaller image, so the WCS it returns
(`CRPIX`, `CD`, any SIP terms) is expressed in the **binned, cropped grid**, not
the original full frame. Any code that wants to relate the solution back to
original full-frame pixels must convert between the two grids.

`solvers/pixel_grid.py` is the single home of that conversion. Everything else
must call it.

## The convention (and why the obvious version is wrong)

`--crpix-x/--crpix-y` set the output `CRPIX1/CRPIX2` directly, in the FITS
**1-based** convention (first pixel center = `1.0`). Verified: `--crpix-center`
yields `(NAXIS+1)/2` (e.g. `2072.5` for a 4144-wide binned image).

For downsample factor `f`, binned pixel `j` (0-based) is the mean of original
pixels `j*f … j*f+f-1`, so the **center** of binned pixel `j` is at original
0-based coordinate `j*f + (f-1)/2`. In FITS 1-based terms on both grids:

```
grid_fits = (orig_fits + (f - 1)/2) / f      # original → binned
orig_fits =  grid_fits * f - (f - 1)/2       # binned → original
```

The tempting `orig_fits / f` is **wrong** by `(f-1)/(2f)` binned px — at `f=2`
that is `0.25` binned px ≈ `0.13″` at `0.2616″/px`.

## Bugs this has caused (both silent)

| Bug | Effect | Status |
|---|---|---|
| Naive `orig/f` instead of pixel-center-correct mapping | 0.5 original-px (~0.13″) offset | found in equivalence study; convention now fixed |
| ROI `refpix = (center - start) // f` (integer division) | up to ~0.75 binned px ≈ 1.5 original px ≈ 0.4″ pointing bias on the spec/fiber path | **fixed** here → `pixel_grid.roi_center_to_crpix` (fractional) |

Both are silent: the field still solves, the WCS looks fine, and the only
symptom is a small constant pointing offset. There is no exception, no log
error. That is why the math is isolated and unit-tested.

## Why we keep it instead of switching to bare astrometry

A bare `solve-field --downsample N` on the full frame returns its WCS in
original full-frame pixels — no binned grid, no conversion, no surface. We
**chose to keep** the numpy pre-downsample + crop path anyway, so that ROI
cropping remains implementable in-house without reopening the solver decision.
The surface is the cost of that future flexibility; it is fenced (this doc,
`pixel_grid.py`, `CLAUDE.md`, and the tests), not removed. If the ROI path is
ever permanently abandoned, `pixel_grid.py` and its tests can go with it.

## Tweak (SIP) is on

`mastrometry.py` does **not** pass `--no-tweak`. The reference solver leaves
tweak on, and the equivalence study showed `--no-tweak` over-constrains the WCS
to a linear fit and causes up to ~7″ disagreement (gone once SIP is fit). SIP
also models corner distortion, which matters for full-frame pixel consistency.
Re-add `--no-tweak` only if solve latency becomes binding and the accuracy loss
is acceptable.

## Off-center CRPIX behavior (matters for the ROI/spec path)

The ROI path places CRPIX at the **fiber pixel** (`--crpix-x/--crpix-y` from
`roi_center_to_crpix`), then reads `CRVAL` back as "where the fiber points". Two
measured properties of that, both real astrometry.net behavior and **not** refpix
bugs (the convention is exact — `test_pixel_grid` pins it to the milli-pixel):

- **SIP re-anchoring (~0.5–1.5″).** With tweak on, moving CRPIX off-center
  re-anchors the SIP fit. The `CRVAL` reported at an off-center CRPIX differs
  from what a CRPIX-center solve of the *same image* predicts for that pixel by
  ~0.5–1.5″, growing with distance from center. So the absolute fiber sky
  position carries this much solve-dependent scatter.
- **Small cropped field (~3–4″).** Solving a *cropped* ROI (e.g. 3000 px →
  1500 binned) instead of the full frame shifts the solution by a further few
  arcsec versus a full-frame solve at the same sky point — fewer stars and a
  shorter distortion baseline. This is a property of small-field solves,
  separate from (and larger than) the CRPIX effect above.

Implication for whoever implements ROI in-house: the *grid/refpix math* is exact,
but the *absolute* fiber pointing from a cropped, off-center solve is good only to
a few arcsec. If tighter absolute pointing is needed, prefer a full-frame solve
(CRVAL evaluated at the fiber pixel via this module) over a small cropped solve,
or budget for the scatter. `test_offcenter_crpix_is_honored_and_lands_near_truth`
guards the invocation (exact CRPIX) with a deliberately loose CRVAL bound.

## Tests / drift detection

We have no CI. The tests in `solvers/tests/` are split so that the most
important guard needs nothing:

- **`test_pixel_grid.py`** — pure math, **runs anywhere** (`pytest`), no
  astrometry.net. This is the primary drift canary for the conversion: it pins
  the convention, round-trips, and asserts the old integer-division bug stays
  dead.
- **`test_equivalence_integration.py`** — **skipped unless** astrometry.net, the
  index directory, and a sample full-frame FITS are present. It re-runs the
  equivalence study (numpy pre-downsample vs `--downsample`) and asserts the two
  WCS still agree to sub-arcsecond after converting through `pixel_grid`.

See `solvers/tests/README.md` for how to point the integration test at a solver,
indexes, and a fixture image.

Original standalone study: `C:\MAST\mastrometry-equivalence`
(`compare_solves.py`, `FINDINGS.md`).
