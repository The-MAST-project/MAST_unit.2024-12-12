r"""
Coordinate-grid transforms between the original full-frame image and the
downsampled (binned) image that MASTrometry hands to astrometry.net.

==============================================================================
  !!  FRAGILE SURFACE -- READ THIS BEFORE TOUCHING  !!
==============================================================================
This module is the *single source of truth* for converting pixel coordinates
between the original full-frame grid and the 2x2-binned grid that
``MastrometryDotNet`` solves on. It exists because that conversion has already
produced two real, silent, sub-arcsecond bugs:

  1. A 0.5-original-pixel offset from using the naive ``orig/factor`` mapping
     instead of the pixel-center-correct one. (Found in the equivalence study;
     see below.)
  2. An integer-division ROI ``refpix`` (``(center - start) // factor``) that
     biased the reference pixel by up to ~0.75 binned px ~= 1.5 original px
     ~= 0.4", landing directly as a telescope/fiber pointing error on the
     spec path. (Was live in ``mastrometry.py``; this module fixes it.)

These bugs are insidious: the WCS still "solves", the numbers look plausible,
and the error only shows up as a small, constant pointing offset. There is no
loud failure. THAT is why the math lives here, in one tiny pure-Python module
with no heavy imports, fenced by unit tests that run anywhere (no astrometry.net
required) -- so drift is caught the moment it is introduced.

WHY THE SURFACE EXISTS AT ALL
-----------------------------
``MastrometryDotNet`` pre-downsamples (and optionally ROI-crops) the image in
numpy *before* calling astrometry.net. astrometry.net therefore returns a WCS
whose CRPIX/CD live in the *binned, cropped* grid -- not the original full
frame. Anything that wants to relate that solution back to original full-frame
pixels (e.g. a future in-house ROI implementation) MUST go through this module.

A bare ``solve-field --downsample N`` on the full frame would NOT need any of
this -- its WCS comes back in original full-frame pixels. We deliberately keep
the numpy pre-downsample + crop path (and therefore this surface) so that ROI
cropping stays implementable in-house without reopening the solver choice.
If the ROI path is ever dropped for good, this module can go with it.

THE CONVENTION (validated)
--------------------------
solve-field's ``--crpix-x/--crpix-y`` set the output CRPIX1/CRPIX2 directly,
in the FITS 1-based pixel convention (first pixel center = 1.0). This was
verified against ``--crpix-center``, which yields ``(NAXIS+1)/2`` -- e.g. 2072.5
for a 4144-wide binned image.

For a downsample factor ``f``, binned pixel ``j`` (0-based) averages original
pixels ``[j*f .. j*f + f - 1]``, so the *center* of binned pixel ``j`` sits at
original 0-based coordinate ``j*f + (f-1)/2``. Converting to the FITS 1-based
convention on both grids gives the only correct mapping:

    grid_fits  = (orig_fits + (f - 1) / 2) / f          # orig -> binned
    orig_fits  =  grid_fits * f  -  (f - 1) / 2         # binned -> orig

The naive ``orig_fits / f`` is wrong by ``(f-1)/(2f)`` binned px (0.25 px at
f=2 ~= 0.13" at 0.2616"/px). Do not reintroduce it.

EQUIVALENCE STUDY
-----------------
The mapping above was derived and validated in the standalone study at
``C:\MAST\mastrometry-equivalence`` (``compare_solves.py``, ``FINDINGS.md``).
There the numpy pre-downsample, ``solve-field --downsample 2``, and the full
resolution solve all agreed to <=0.08" at field center and <=0.34" across the
full frame once this convention was applied. ``solvers/tests/`` ports that
study into runnable drift tests.
==============================================================================
"""

from __future__ import annotations


def orig_to_grid_fits(orig_fits: float, factor: int) -> float:
    """Map a FITS 1-based coordinate on the ORIGINAL grid to the downsampled grid.

    ``factor`` is the downsample (binning) factor. With ``factor == 1`` this is
    the identity. See the module docstring for the derivation; the result is the
    value to hand to solve-field's ``--crpix-x/--crpix-y`` (also FITS 1-based).
    """
    if factor < 1:
        raise ValueError(f"downsample factor must be >= 1, got {factor}")
    return (orig_fits + (factor - 1) / 2.0) / factor


def grid_to_orig_fits(grid_fits: float, factor: int) -> float:
    """Inverse of :func:`orig_to_grid_fits`: downsampled FITS coord -> original.

    Use this to relate an astrometry.net solution that was solved on the binned
    image back to original full-frame pixels (e.g. when comparing WCS solutions
    or mapping a binned-grid pixel to sky in original-frame terms).
    """
    if factor < 1:
        raise ValueError(f"downsample factor must be >= 1, got {factor}")
    return grid_fits * factor - (factor - 1) / 2.0


def roi_center_to_crpix(center_orig0: int, start_orig0: int, factor: int) -> float:
    """``--crpix`` value (1-based, on the cropped+binned grid) for an ROI center.

    All inputs are along a single axis (call once for x, once for y).

    - ``center_orig0``: the ROI center pixel, 0-based index in the ORIGINAL full
      frame (this is ``ImagerRoi._center.x`` / ``._center.y``).
    - ``start_orig0``:  the crop origin (ROI top-left), 0-based index in the
      ORIGINAL full frame (this is ``ImagerRoi.x`` / ``.y``).
    - ``factor``: downsample factor applied after cropping.

    The crop shifts the origin, so the ROI center sits at 0-based index
    ``center_orig0 - start_orig0`` in the cropped image; its FITS 1-based
    coordinate there is ``(center_orig0 - start_orig0) + 1``. We then map that
    onto the binned grid with the validated convention.

    NOTE: returns a float on purpose. The previous code used integer division
    (``(center - start) // factor``), which both truncated and skipped the
    pixel-center correction -- a silent ~0.4" pointing bias. solve-field accepts
    fractional CRPIX, so pass the float through unrounded.
    """
    cropped_fits = (center_orig0 - start_orig0) + 1  # FITS 1-based in the cropped frame
    return orig_to_grid_fits(cropped_fits, factor)
