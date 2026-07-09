# Solvers — guidance & warnings

## ⚠️ Fragile coordinate surface: the binned/cropped → full-frame transform

`MastrometryDotNet` (`mastrometry.py`) pre-downsamples (2×2) and optionally
ROI-crops the image **in numpy before** calling astrometry.net. As a result the
WCS astrometry.net returns lives in the **binned, cropped** grid — not the
original full frame. Relating that solution back to original full-frame pixels
requires a coordinate transform, and **that transform has already produced two
silent sub-arcsecond bugs** (a 0.5px naive-division offset, and an
integer-division ROI `refpix` that biased pointing by ~0.4").

**Rules when working in this directory:**

1. **All** original↔binned / ROI-`refpix` coordinate math goes through
   `solvers/pixel_grid.py`. Do not hand-roll `orig/factor` or `(center-start)//factor`
   inline — those are the exact bugs. `--crpix-x/--crpix-y` are FITS **1-based**.
2. The transform is **silent on failure**: the WCS still solves and the numbers
   look plausible; the error surfaces only as a small constant pointing offset
   (worst on the spec/fiber path). Treat any change here as high-risk.
3. After any change to `pixel_grid.py`, `mastrometry.py` downsample/crop/refpix
   logic, or the solve-field flags, **run `solvers/tests/`** (see that dir's
   README). The pure-math tests run anywhere; the integration tests run on a
   machine with astrometry.net + indexes + a sample FITS.
4. **Tweak (SIP) is intentionally ON** in `mastrometry.py` — do not re-add
   `--no-tweak` without reading the comment there; it breaks agreement with the
   reference solver and with full-frame pixel consistency.

## Why the surface is kept (don't "simplify" it away)

A bare `solve-field --downsample N` on the full frame would avoid this surface
entirely (its WCS comes back in full-frame pixels). We **deliberately keep** the
numpy pre-downsample + crop path so ROI cropping stays implementable in-house
without reopening the solver choice. The surface is the price of that
flexibility; it is fenced, not removed. See `COORDINATE_SURFACE.md`.

## Background

Conventions, derivation, and the equivalence evidence are in
`pixel_grid.py` (module docstring) and `COORDINATE_SURFACE.md`. The original
standalone study lives at `C:\MAST\mastrometry-equivalence`.
