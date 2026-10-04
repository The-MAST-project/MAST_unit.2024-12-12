"""Self-contained HFD focus analysis -- the analyzer behind ``/calibrate/focuser``.

The alternative to the external ps3cli analyzer in ``focus_analysis.py``: it
needs no ``ps3cli`` server and no star catalog, and its metric (half-flux
diameter over near-axis stars) tolerates the asymmetric PSFs an f/3 parabola
produces, where a catalog-matched RMS diameter does not.

It returns :class:`calibration.analysis.models.HFDAutofocusStatus`, which
satisfies :mod:`calibration.analysis.protocols` -- so a consumer typed against
``FocusAnalysisResultLike`` (the V-curve plotter, report generators) accepts this
and the ps3cli result interchangeably, with no cast.

For each ``FOCUSnnnnn.fits`` image (the focuser position is encoded in the file
name) it measures the median Half-Flux Diameter of the near-axis stars
(``imaging.hfd.frame_hfd``), then fits the V-curve as

    D^2 = a*x^2 + b*x + c      (linear least-squares, error-weighted)

giving best focus ``x* = -b/2a``, minimum diameter ``Dmin = sqrt(c - b^2/4a)``,
and a tolerance window where the fitted diameter rises by ``tolerance_frac``.

Design reference: docs/autofocus_design.md.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
from astropy.io import fits

from calibration.analysis.models import (
    HFDAutofocusResult,
    HFDAutofocusStatus,
    HFDFocusSample,
)
from calibration.logging_context import init_calibration_log
from imaging.donut import DonutJump, frame_donut_metric, plan_donut_jump
from imaging.hfd import measure_sweep_hfd

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)

_POS_RE = re.compile(r"FOCUS(-?\d+)", re.IGNORECASE)


def focus_position_of(path) -> float | None:
    """Focuser position from the ``FOCUSnnnnn`` file name, else the FOCUSPOS header."""
    m = _POS_RE.search(Path(path).name)
    if m:
        return float(m.group(1))
    try:
        return float(fits.getheader(path)["FOCUSPOS"])
    except (OSError, KeyError, ValueError):
        return None


def _fit_vcurve(positions, diameters, tolerance_frac):
    """Error-weighted fit of D^2 = a x^2 + b x + c.

    Returns ``(a, b, c, xstar, dmin, tolerance)`` or ``None`` if the fit is not a
    valid (concave-up) cup.  Smaller diameters (near focus) are weighted up, as
    they pin the vertex; the fit is linear in (a, b, c) on D^2.
    """
    x = np.asarray(positions, dtype=float)
    d = np.asarray(diameters, dtype=float)
    if len(x) < 3 or len(np.unique(x)) < 3:
        return None
    w = 1.0 / np.clip(d, 1e-6, None) ** 2
    a, b, c = np.polyfit(x, d**2, 2, w=w)
    if a <= 0:
        return None
    dmin2 = c - b * b / (4.0 * a)
    if dmin2 <= 0:
        return None
    xstar = -b / (2.0 * a)
    dmin = float(np.sqrt(dmin2))
    f = tolerance_frac
    tol = float(np.sqrt(dmin2 * ((1.0 + f) ** 2 - 1.0) / a))
    return float(a), float(b), float(c), float(xstar), dmin, tol


def vcurve_shape(positions, diameters, fit) -> tuple[float, float]:
    """``(r2, edge_rise)`` of a fitted V-curve: does it describe a real V at all?

    ``r2`` is the coefficient of determination of the fitted ``D^2`` parabola
    against the samples' ``D^2``.  ``edge_rise`` is the HIGHER of the two sweep
    ends divided by the fitted ``Dmin`` -- the higher, because a real vertex near
    one edge rises little on its short side.

    Why this is needed on top of bracketing: a sweep far from focus, over faint
    donuts, measures a near-constant HFD (the aperture, not a star), and noise puts the
    smallest sample in the interior more often than not.  A parabola through that
    is a vertex with nothing behind it.  On mast00 2026-10-01 three such sweeps,
    2000-5000 ticks from focus, passed every other check and one was persisted.
    """
    x = np.asarray(positions, dtype=float)
    d2 = np.asarray(diameters, dtype=float) ** 2
    a, b, c, _, dmin, _ = fit
    ss_tot = float(((d2 - d2.mean()) ** 2).sum())
    ss_res = float(((d2 - (a * x * x + b * x + c)) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    order = np.argsort(x)
    d = np.sqrt(d2[order])
    return r2, float(max(d[0], d[-1]) / dmin)


def _result(samples, errors, fit=None, n_consistent: int = 0, fit_r2: float | None = None) -> HFDAutofocusStatus:
    if fit is None:
        ar = HFDAutofocusResult(
            has_solution=False,
            best_focus_position=None,
            best_focus_star_diameter=None,
            tolerance=None,
            vcurve_a=None,
            vcurve_b=None,
            vcurve_c=None,
            n_consistent_stars=n_consistent,
            fit_r2=fit_r2,
            focus_samples=samples,
            errors=errors,
        )
        msg = errors[-1] if errors else "no solution"
    else:
        a, b, c, xstar, dmin, tol = fit
        ar = HFDAutofocusResult(
            has_solution=True,
            best_focus_position=xstar,
            best_focus_star_diameter=dmin,
            tolerance=tol,
            vcurve_a=a,
            vcurve_b=b,
            vcurve_c=c,
            n_consistent_stars=n_consistent,
            fit_r2=fit_r2,
            focus_samples=samples,
            errors=errors,
        )
        msg = f"HFD V-curve: best={xstar:.1f}, Dmin={dmin:.2f}, tol={tol:.1f}"
    return HFDAutofocusStatus(message=msg, errors=errors, analysis_result=ar)


def analyze_focus_samples(
    samples,
    *,
    tolerance_frac: float = 0.025,
    min_valid: int = 3,
    require_bracketed: bool = True,  # only solve when the sweep straddles focus
    min_fit_r2: float | None = 0.85,  # shape gate -- see vcurve_shape(); None skips
    min_edge_rise: float | None = 1.25,
    **hfd_kw,
) -> HFDAutofocusStatus:
    """HFD V-curve analysis of an **in-memory** focus sweep.

    ``samples`` is a list of ``(focuser_position, image)`` pairs, ``image`` being
    a 2D array -- e.g. ``imager.image_array`` after ``wait_for_image_ready()`` on
    an imager whose ``can_image_to_memory`` is True (the ZWO) -- or a FITS path.
    This is the array-native core behind :func:`analyze_focus_files_hfd`, so the
    Calibrator can feed frames straight from memory with no ``FOCUSnnnnn``
    round-trip through disk.
    ``hfd_kw`` is forwarded to :func:`imaging.hfd.measure_sweep_hfd` (e.g.
    ``nsigma``, ``r_factor``, ``near_axis_frac``).  Autofocus runs on FULL frames,
    which include the coma-heavy margins, so restrict the metric to the calibrated
    low-coma zone by forwarding ``center`` + ``radius``: the stored
    ``calibration.optical_center`` is already in full-frame detector pixels, so
    pass ``.center`` directly (``.local_center(roi_x, roi_y)`` is only for an ROI
    sub-frame), guarded by ``.matches(image_shape, mechanical_epoch)``.  When the
    unit is not yet calibrated, omit ``center`` / ``radius`` and pass a geometric
    ``near_axis_frac`` (~middle third) as the stand-in about the image center --
    not the ``1.0`` default, which would keep the margins.  The sweep is analysed
    JOINTLY -- a consistent star set is measured at fixed positions in every frame
    -- so the V-curve is not buried by frame-to-frame detection jitter.
    """
    samples = sorted(samples, key=lambda s: s[0] if s[0] is not None else float("inf"))
    positions = [p for p, _ in samples]
    images = [im for _, im in samples]
    errors: list[str] = []
    try:
        per_frame, n_consistent = measure_sweep_hfd(images, **hfd_kw)
    except Exception:
        logger.exception("measure_sweep_hfd failed")
        per_frame, n_consistent = [(float("nan"), 0)] * len(images), 0

    hfd_samples: list[HFDFocusSample] = []
    for pos, (hfd, n) in zip(positions, per_frame, strict=True):
        valid = pos is not None and n > 0 and np.isfinite(hfd)
        hfd_samples.append(
            HFDFocusSample(
                is_valid=bool(valid),
                focus_position=pos,
                num_stars=int(n),
                hfd_pixels=(float(hfd) if np.isfinite(hfd) else None),
            )
        )

    good = [s for s in hfd_samples if s.is_valid]
    if len(good) < min_valid:
        errors.append(f"only {len(good)} valid HFD samples (consistent stars={n_consistent}); need >= {min_valid}")
        return _result(hfd_samples, errors, n_consistent=n_consistent)

    fit = _fit_vcurve(
        [s.focus_position for s in good],
        [s.hfd_pixels for s in good],
        tolerance_frac,
    )
    if fit is None:
        errors.append("V-curve fit failed (non-concave / degenerate)")
        return _result(hfd_samples, errors, n_consistent=n_consistent)

    # Trust the vertex only if the sweep actually BRACKETS focus -- the
    # smallest-HFD sample must be interior, not at an edge.  A monotonic ramp
    # (minimum at an edge) means best focus is outside the swept range, where the
    # parabola is pure extrapolation (unreliable for any method); the correct
    # response is to shift/extend the sweep (the acquisition phase), not to
    # report a confident focus.
    if require_bracketed:
        pairs = sorted(
            (float(s.focus_position), float(s.hfd_pixels))
            for s in good
            if s.focus_position is not None and s.hfd_pixels is not None
        )
        diam = [d for _, d in pairs]
        if not diam or int(np.argmin(diam)) in (0, len(diam) - 1):
            errors.append("focus not bracketed: minimum HFD at a sweep edge -- shift/extend the sweep")
            return _result(hfd_samples, errors, n_consistent=n_consistent)

    r2, rise = vcurve_shape([s.focus_position for s in good], [s.hfd_pixels for s in good], fit)
    if min_fit_r2 is not None and r2 < min_fit_r2:
        errors.append(f"V-curve shape: fit R^2={r2:.2f} < {min_fit_r2} -- the samples are not a V (noise?)")
        return _result(hfd_samples, errors, n_consistent=n_consistent, fit_r2=r2)
    if min_edge_rise is not None and rise < min_edge_rise:
        errors.append(f"V-curve shape: sweep ends rise only {rise:.2f}x Dmin (< {min_edge_rise}) -- flat, not a V")
        return _result(hfd_samples, errors, n_consistent=n_consistent, fit_r2=r2)
    return _result(hfd_samples, errors, fit, n_consistent=n_consistent, fit_r2=r2)


def analyze_focus_files_hfd(
    files,
    *,
    tolerance_frac: float = 0.025,
    min_valid: int = 3,
    require_bracketed: bool = True,
    min_fit_r2: float | None = 0.85,
    min_edge_rise: float | None = 1.25,
    host=None,
    port=None,
    timeout=None,  # host/port/timeout accepted (ignored) for signature parity
    **hfd_kw,
) -> HFDAutofocusStatus:
    """HFD V-curve analysis of a ``FOCUSnnnnn.fits`` sweep on disk.

    Thin file-based wrapper over :func:`analyze_focus_samples`: it reads each
    focuser position from the ``FOCUSnnnnn`` name (falling back to the ``FOCUSPOS``
    header) and delegates.  This is the path the current file-only imager (PHD2,
    ``wait_for_image_saved``) uses; a memory-capable imager should call
    :func:`analyze_focus_samples` directly.
    """
    samples = [(focus_position_of(f), f) for f in files]
    return analyze_focus_samples(
        samples,
        tolerance_frac=tolerance_frac,
        min_valid=min_valid,
        require_bracketed=require_bracketed,
        min_fit_r2=min_fit_r2,
        min_edge_rise=min_edge_rise,
        **hfd_kw,
    )


def analyze_donut_samples(
    samples,
    *,
    min_donuts: int = 1,
    undershoot_frac: float = 0.15,
    **detect_kw,
) -> DonutJump:
    """Plan a Phase-2 donut jump from **in-memory** ``(focuser_position, image)`` samples.

    Array-native core behind :func:`analyze_donut_files`; ``image`` is a 2D array
    (``imager.image_array``) or a FITS path.  For each frame it measures the median
    donut outer diameter (:func:`imaging.donut.frame_donut_metric`) and fits
    diameter-vs-position, weighting by donut *count* so richer frames pull harder.
    ``detect_kw`` is forwarded to :func:`imaging.donut.detect_donuts` (e.g.
    ``nsigma``, ``min_diameter``).  Only frames with a position and at least
    ``min_donuts`` donuts feed the fit.  Returns a :class:`imaging.donut.DonutJump`;
    check ``has_solution``.  Position ordering is handled by
    :func:`imaging.donut.plan_donut_jump`, so ``samples`` need not be sorted.
    """
    positions, diameters, weights = [], [], []
    for pos, img in samples:
        if pos is None:
            continue
        try:
            metric = frame_donut_metric(img, min_donuts=min_donuts, **detect_kw)
        except Exception:
            logger.exception("frame_donut_metric failed")
            continue
        if metric.n_donuts >= min_donuts and np.isfinite(metric.median_diameter):
            positions.append(pos)
            diameters.append(metric.median_diameter)
            weights.append(float(metric.n_donuts))

    if len(positions) < 2:
        return DonutJump(
            False,
            None,
            None,
            None,
            0,
            len(positions),
            float("nan"),
            f"only {len(positions)} frame(s) with donuts; need >=2 at distinct positions",
        )
    return plan_donut_jump(positions, diameters, weights=weights, undershoot_frac=undershoot_frac)


def analyze_donut_files(
    files,
    *,
    min_donuts: int = 1,
    undershoot_frac: float = 0.15,
    **detect_kw,
) -> DonutJump:
    """Plan a Phase-2 donut jump from a ``FOCUSnnnnn.fits`` differential set on disk.

    Thin file-based wrapper over :func:`analyze_donut_samples`: reads each focuser
    position from the ``FOCUSnnnnn`` name (or ``FOCUSPOS`` header) and delegates.
    The memory-capable path is :func:`analyze_donut_samples`.
    """
    samples = [(focus_position_of(f), f) for f in files]
    return analyze_donut_samples(samples, min_donuts=min_donuts, undershoot_frac=undershoot_frac, **detect_kw)
