"""Donut acquisition -- Phase 2 of the self-contained HFD autofocus.

Far from best focus the central obstruction turns each star into an annular
**donut** whose outer diameter grows ~linearly with the focuser offset from
focus.  Out here the HFD V-curve of Phase 1 is meaningless (SEP point-source
extraction fails; the donuts are large and may be blended), so a different,
coarser routine is needed to *jump* back near focus before handing off:

  1. **Detect** donuts with blob / connected-component / threshold detection
     (`detect_donuts`) -- never point-source extraction.
  2. **Measure** a per-frame donut size plus the crude "getting warmer" signals
     (`frame_donut_metric`): median outer diameter (shrinks toward focus) and the
     donut count / above-threshold area (both rise toward focus -- the cold-start
     signal before any diameter is meaningful).
  3. **Plan the jump** (`plan_donut_jump`): from >=2 frames at known focuser
     positions, fit outer-diameter vs. position.  Defocus is an *even* wavefront
     term, so a single donut's diameter gives the *magnitude* of the offset but
     not its *sign* (inside vs. outside focus look identical); the differential
     move across the frames is exactly what resolves the sign.  Extrapolate the
     linear arm to its vertex and return an (undershot) target that lands near
     focus, where Phase 0 re-triages and Phase 1 refines.

The donut-diameter-vs-offset slope is **measured here, never assumed**: the design
(docs/autofocus_design.md sec. 2.5, 6) leaves its value to be characterised on
real optics, so `plan_donut_jump` calibrates it from the differential move itself.

Design reference: docs/autofocus_design.md sec. 2.5 (donuts / sign ambiguity),
sec. 3 Phase 2; unit self-calibration design sec. 1 (autofocus).  Parallel to
`calibration.analysis.hfd` (Phase 1, near focus) and routed to by `hfd.assess_focus_regime`
returning ``"far"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import scipy.ndimage as ndi
from astropy.io import fits
from astropy.stats import SigmaClip
from photutils.background import Background2D, MedianBackground
from photutils.segmentation import detect_threshold


def _load(image) -> np.ndarray:
    """Accept a FITS path or a 2D array; return a float image."""
    data = image if isinstance(image, np.ndarray) else fits.getdata(image)
    data = np.asarray(data, dtype=float)
    if data.ndim != 2:
        raise ValueError(f"expected a 2D image, got shape {data.shape}")
    return data


def _bg_subtract(data, box_size=64):
    try:
        bkg = Background2D(
            data,
            box_size=box_size,
            filter_size=(3, 3),
            sigma_clip=SigmaClip(sigma=3.0),
            bkg_estimator=MedianBackground(),  # type: ignore[arg-type]
        )
        return data - bkg.background
    except Exception:  # noqa: BLE001 -- background estimation is best-effort; any failure falls back to the median
        return data - np.median(data)


@dataclass
class DonutBlob:
    """One detected donut (or defocused blob).

    ``outer_diameter`` is the area-equivalent diameter of the **hole-filled**
    blob (``2*sqrt(filled_area/pi)``): filling the central obstruction shadow
    recovers the full disc, so this is the donut's *outer* diameter regardless of
    how annular it looks.  ``annularity`` is the filled-in hole fraction (0 = a
    filled disc, ->1 = a thin ring), a diagnostic of how far out of focus we are
    -- not a detection requirement, since the hole is unresolved at moderate
    defocus.  ``axis_ratio`` (major/minor from the filled second moments) flags
    blended / clipped blobs.
    """

    x: float
    y: float
    outer_diameter: float  # area-equivalent diameter of the hole-filled blob (pix)
    area: float  # above-threshold ring area (pix), the hole NOT filled
    annularity: float  # hole_area / filled_area, 0..1
    axis_ratio: float  # major/minor axis (filled), >= 1; ~1 = round single donut
    fill_fraction: float  # filled_area / bbox_area; a lone round disc ~ pi/4


@dataclass
class DonutMetric:
    """Per-frame donut summary and the cold-start "getting warmer" signals.

    ``median_diameter`` shrinks monotonically toward focus (the metric the jump
    plan fits).  ``n_donuts`` and ``area_fraction`` are the coarse *emergence*
    signals for cold-start: as flux concentrates out of sky-limited blur, faint
    stars cross threshold and usable structure appears.  Their exact behaviour vs.
    defocus is design-intent to characterise on real optics (autofocus_design.md
    sec. 6), so treat them as "structure is appearing", not a calibrated distance.
    """

    median_diameter: float  # median outer diameter of accepted donuts (pix), nan if none
    n_donuts: int  # accepted donut count (emergence signal, see class docstring)
    area_fraction: float  # accepted donut area / image area (emergence signal)
    blobs: list[DonutBlob] = field(default_factory=list, repr=False)


@dataclass
class DonutJump:
    """A planned focuser move from the donut differential measurement.

    ``best_focus_estimate`` is the linear extrapolation of the donut arm to zero
    diameter (``x* = -b/m``) -- an over-estimate of the travel (the real curve
    rounds near the vertex), so ``target_position`` undershoots it to land on the
    *same* arm near focus rather than overshooting to the far side.  ``direction``
    is the sign of the move toward focus; ``slope`` is the measured, signed
    dDiameter/dPosition (pix per tick) that also fixes the inside/outside sign.
    """

    has_solution: bool
    best_focus_estimate: float | None  # x* = -b/m, extrapolated vertex (focuser pos)
    target_position: float | None  # recommended next focuser position (undershot)
    slope: float | None  # signed dDiameter/dPosition (pix/tick)
    direction: int  # +1 / -1 toward focus, 0 if unknown
    n_samples: int
    residual_rms: float  # of the linear diameter-vs-position fit (pix)
    message: str


def _blob_props(filled):
    """Area, local centroid, axis ratio and moment radius of a filled blob mask."""
    ys, xs = np.nonzero(filled)
    area = float(xs.size)
    cx, cy = float(xs.mean()), float(ys.mean())
    dx, dy = xs - cx, ys - cy
    # covariance of the (uniform) filled disc -> principal axes; for a disc of
    # radius R, each variance is R^2/4, so the axis ratio flags elongation/blends.
    vxx = float(np.mean(dx * dx)) + 1 / 12.0  # +1/12: finite-pixel (Sheppard) correction
    vyy = float(np.mean(dy * dy)) + 1 / 12.0
    vxy = float(np.mean(dx * dy))
    tr, det = vxx + vyy, vxx * vyy - vxy * vxy
    disc = max(tr * tr / 4.0 - det, 0.0)
    lam1, lam2 = tr / 2.0 + np.sqrt(disc), tr / 2.0 - np.sqrt(disc)
    axis_ratio = float(np.sqrt(lam1 / lam2)) if lam2 > 0 else np.inf
    return area, cx, cy, axis_ratio


def detect_donuts(
    image,
    nsigma=3.0,  # detection threshold, sigma above background
    box_size=64,  # 2D background box
    min_diameter=12.0,  # reject point sources / noise specks below this (pix)
    max_diameter=500.0,  # reject frame-spanning junk above this (pix)
    max_axis_ratio=1.8,  # reject blended / clipped blobs (major/minor)
    min_fill_fraction=0.45,  # filled_area / bbox_area; a lone round disc ~ pi/4=0.79
    open_iter=1,  # binary opening to shed noise necks before labelling
    edge_margin=6,  # drop blobs touching within this of the frame edge (clipped)
) -> list[DonutBlob]:
    """Blob-detect donuts / large defocused stars in one frame.

    Threshold the background-subtracted frame, label connected components, and
    for each: fill the central-obstruction hole to recover the outer disc, then
    keep only round, unclipped, correctly-sized blobs.  This is deliberately
    **not** the SEP / photutils *point-source* path used near focus -- donuts are
    large, annular and often blended, which point extraction mishandles.

    Returns the accepted :class:`DonutBlob` list (possibly empty).
    """
    data = _load(image)
    ny, nx = data.shape
    data_sub = _bg_subtract(data, box_size)
    mask = data_sub > detect_threshold(data_sub, n_sigma=nsigma)
    if open_iter > 0:
        mask = ndi.binary_opening(mask, iterations=int(open_iter))

    labels, n = ndi.label(mask)  # type: ignore[misc]
    if n == 0:
        return []
    slices = ndi.find_objects(labels)

    min_area = np.pi / 4.0 * min_diameter**2
    max_area = np.pi / 4.0 * max_diameter**2
    blobs: list[DonutBlob] = []
    for i, sl in enumerate(slices, start=1):
        if sl is None:
            continue
        y0, x0 = sl[0].start, sl[1].start
        sub = labels[sl] == i
        filled = ndi.binary_fill_holes(sub)
        assert filled is not None  # None only when an output array is passed
        ring_area = float(sub.sum())
        filled_area = float(filled.sum())
        outer_diameter = 2.0 * np.sqrt(filled_area / np.pi)
        if not (min_area <= filled_area <= max_area):
            continue
        if not (min_diameter <= outer_diameter <= max_diameter):
            continue

        area_a, cx_l, cy_l, axis_ratio = _blob_props(filled)
        if axis_ratio > max_axis_ratio:
            continue
        bh, bw = sub.shape
        if filled_area / (bh * bw) < min_fill_fraction:
            continue
        cx, cy = x0 + cx_l, y0 + cy_l
        if cx < edge_margin or cx > nx - 1 - edge_margin or cy < edge_margin or cy > ny - 1 - edge_margin:
            continue

        annularity = (filled_area - ring_area) / filled_area if filled_area > 0 else 0.0
        blobs.append(
            DonutBlob(
                x=float(cx),
                y=float(cy),
                outer_diameter=float(outer_diameter),
                area=ring_area,
                annularity=float(annularity),
                axis_ratio=float(axis_ratio),
                fill_fraction=float(filled_area / (bh * bw)),
            )
        )
    return blobs


def frame_donut_metric(image, min_donuts=1, **detect_kw) -> DonutMetric:
    """Median donut diameter and the "getting warmer" signals for one frame.

    ``median_diameter`` is ``nan`` (with ``n_donuts`` still reported) when fewer
    than ``min_donuts`` donuts are accepted -- so a caller can use the rising
    ``n_donuts`` / ``area_fraction`` as a coarse cold-start signal even before any
    diameter is trustworthy.
    """
    data = _load(image)
    blobs = detect_donuts(data, **detect_kw)
    n = len(blobs)
    area_fraction = float(sum(b.area for b in blobs) / data.size) if n else 0.0
    if n < min_donuts:
        return DonutMetric(float("nan"), n, area_fraction, blobs)
    median_diameter = float(np.median([b.outer_diameter for b in blobs]))
    return DonutMetric(median_diameter, n, area_fraction, blobs)


def plan_donut_jump(
    positions,
    diameters,
    weights=None,
    undershoot_frac=0.15,  # stop this fraction short of the extrapolated vertex, so
    #                        we land on the CURRENT arm near focus, not past it
    min_abs_slope=1e-3,  # |dDiam/dPos| floor: below it the move was too small (or we
    #                      are near the vertex) to fix magnitude/sign -> take a bigger step
    max_residual_frac=0.2,  # linear-fit rms as a fraction of the diameter span (n>=3)
) -> DonutJump:
    """Plan the focuser jump from donut diameters at known positions.

    Needs >=2 frames at distinct focuser positions (the **differential move** that
    resolves the inside/outside-focus sign).  Fits ``D = m*x + b``; the vertex
    ``x* = -b/m`` is the extrapolated best focus and ``sign(x*-x_near)`` the move
    direction.  Fails (``has_solution=False``) when:
      * the diameter barely changes (``|m| < min_abs_slope``) -- step was too small;
      * with >=3 samples the smallest donut is *interior* -- the sweep already
        **brackets** focus, so hand straight to the HFD V-curve (Phase 1);
      * with >=3 samples the fit is too nonlinear (not a clean single arm).
    """
    x = np.asarray(positions, dtype=float)
    d = np.asarray(diameters, dtype=float)
    ok = np.isfinite(x) & np.isfinite(d) & (d > 0)
    x, d = x[ok], d[ok]
    w = None if weights is None else np.asarray(weights, dtype=float)[ok]
    n = len(x)

    def fail(msg):
        return DonutJump(False, None, None, None, 0, n, float("nan"), msg)

    if n < 2 or len(np.unique(x)) < 2:
        return fail(f"need >=2 donut frames at distinct positions, have {n} (take a differential move)")

    order = np.argsort(x)
    x, d = x[order], d[order]
    w = None if w is None else w[order]

    # Bracketing check: a V (min diameter interior) means focus is already inside
    # the swept range -- the linear arm model is wrong there; refine with Phase 1.
    if n >= 3 and 0 < int(np.argmin(d)) < n - 1:
        return fail("focus bracketed: smallest donut is interior -- switch to the HFD V-curve (Phase 1)")

    m, b = np.polyfit(x, d, 1, w=w)
    if abs(m) < min_abs_slope:
        return fail(f"donut diameter nearly flat (|slope|={abs(m):.2e}); increase the differential step")

    resid = d - (m * x + b)
    rms = float(np.sqrt(np.mean(resid**2)))
    span = float(np.ptp(d)) or 1.0
    if n >= 3 and rms > max_residual_frac * span:
        return fail(f"donut arm nonlinear (fit rms {rms:.1f} > {max_residual_frac:.0%} of {span:.1f})")

    x_star = float(-b / m)
    x_near = float(x[int(np.argmin(d))])  # sample closest to focus = current best vantage
    target = float(x_star + undershoot_frac * (x_near - x_star))
    direction = int(np.sign(x_star - x_near))
    msg = f"donut jump: best~{x_star:.0f}, target {target:.0f} (dir {direction:+d}, slope {m:+.3f} pix/tick, n={n})"
    return DonutJump(True, x_star, target, float(m), direction, n, rms, msg)


def plot_donuts(image, blobs: list[DonutBlob], vmin=None, vmax=None):
    """Show the frame with detected donuts circled (inspection helper)."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle

    data = _load(image)
    if vmin is None:
        vmin = float(np.percentile(data, 5))
    if vmax is None:
        vmax = float(np.percentile(data, 99))
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.imshow(data, origin="lower", cmap="gray", vmin=vmin, vmax=vmax)
    ax.set_title(f"{len(blobs)} donut(s)")
    for bl in blobs:
        circ = Circle((bl.x, bl.y), bl.outer_diameter / 2.0, fill=False, color="cyan", lw=1.0)
        ax.add_patch(circ)
        ax.plot(bl.x, bl.y, "+", color="magenta", ms=8)
    ax.set_xlabel("X (pix)")
    ax.set_ylabel("Y (pix)")
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "D:/MAST/tmp/Samples/sample1.fits"
    metric = frame_donut_metric(path)
    print(
        f"{metric.n_donuts} donut(s), median outer diameter "
        f"{metric.median_diameter:.1f} pix, area frac {metric.area_fraction:.4f}"
    )
    plot_donuts(path, metric.blobs)
