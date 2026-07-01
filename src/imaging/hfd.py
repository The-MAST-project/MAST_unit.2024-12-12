"""Half-Flux Diameter (HFD) focus metric.

The HFD is the diameter of the circle, centred on a star's flux centroid, that
contains half the background-subtracted flux.  The standard production estimator
is twice the flux-weighted mean radius

    HFD = 2 * sum_i(v_i * r_i) / sum_i(v_i)

over background-subtracted, non-negative pixels v_i within an aperture, r_i the
distance from the centroid.  It is the de-facto autofocus metric (FocusMax /
Weber & Brady, MaxIm DL, N.I.N.A. via the half-flux *radius*): a flux-weighted
integral that stays well-defined far from focus -- including on donut PSFs --
where peak/FWHM metrics degenerate.

This is the self-contained metric behind the HFD autofocus path (parallel to the
external ps3cli analyzer).  Design reference: docs/autofocus_design.md; unit
self-calibration design section 1 (autofocus).
"""

from __future__ import annotations

import numpy as np
from astropy.io import fits
from astropy.stats import SigmaClip
from photutils.background import Background2D, MedianBackground
from photutils.segmentation import SourceCatalog, detect_sources, detect_threshold


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
            data, box_size=box_size, filter_size=(3, 3),
            sigma_clip=SigmaClip(sigma=3.0), bkg_estimator=MedianBackground(),  # type: ignore[arg-type]
        )
        return data - bkg.background
    except Exception:
        return data - np.median(data)


def _detect(data_sub, nsigma, npixels, min_area, max_area=1e9):
    """Return (x, y, semimajor_sigma) arrays of detected sources, or empty."""
    segm = detect_sources(data_sub, detect_threshold(data_sub, nsigma=nsigma), npixels=npixels)
    if segm is None:
        return np.empty(0), np.empty(0), np.empty(0)
    cat = SourceCatalog(data_sub, segm)
    x = np.asarray(cat.xcentroid, dtype=float)
    y = np.asarray(cat.ycentroid, dtype=float)
    s = np.asarray(cat.semimajor_sigma.value, dtype=float)
    a = np.asarray(cat.area.value, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(s) & (a >= min_area) & (a <= max_area)
    return x[ok], y[ok], s[ok]


def _measure_hfds_at(data, data_sub, stars, r_factor, r_min, r_max, stamp_pad, k_thresh, max_value):
    """HFDs of the given ``(x, y, smaj)`` stars in one frame (aperture per star
    adapts to its size).  Skips saturated/edge stamps."""
    ny, nx = data.shape
    hfds = []
    for x, y, s in stars:
        r_out = float(np.clip(r_factor * s, r_min, r_max))
        half = int(round(r_out + stamp_pad))
        xc, yc = int(round(x)), int(round(y))
        x0, x1 = max(0, xc - half), min(nx, xc + half + 1)
        y0, y1 = max(0, yc - half), min(ny, yc + half + 1)
        if x1 - x0 < 5 or y1 - y0 < 5:
            continue
        if data[y0:y1, x0:x1].max() >= max_value:  # saturated -> biased HFD
            continue
        h = half_flux_diameter(data_sub[y0:y1, x0:x1], r_out, k_thresh)
        if np.isfinite(h) and h > 0:
            hfds.append(h)
    return hfds


def _low_coma_keep(x, y, shape, center, radius, near_axis_frac):
    """Boolean mask of the stars inside the low-coma (near-axis) zone.

    When an optical ``center`` + ``radius`` are supplied -- from the unit's
    optical-center calibration, expressed in THIS image's pixel frame (subtract
    the ROI origin for a sub-frame) -- keep only stars within ``radius`` of it:
    the disk where coma-driven elongation stays under the calibration's
    tolerance.  With no calibration, fall back to ``near_axis_frac`` of the field
    radius about the GEOMETRIC center (the stand-in the design uses before the
    optical center is known).  ``near_axis_frac >= 1.0`` and no center keeps
    everything -- the correct default for a small focus ROI.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ny, nx = shape
    if center is not None and radius is not None:
        cx, cy = center
        return np.hypot(x - cx, y - cy) <= radius
    if near_axis_frac < 1.0:
        cx0, cy0 = (nx - 1) / 2.0, (ny - 1) / 2.0
        return np.hypot(x - cx0, y - cy0) <= near_axis_frac * np.hypot(cx0, cy0)
    return np.ones(len(x), dtype=bool)


def _consistent_stars(detections, n_frames, match_tol=3.0, min_frac=0.6):
    """Cross-match per-frame ``(x, y, smaj)`` detections into stars seen in many
    frames.  Returns ``[(x_mean, y_mean, smaj_max), ...]`` -- the max size so the
    aperture suits the most-defocused frame."""
    clusters = []
    for fi, arr in enumerate(detections):
        for x, y, s in arr:
            best, bestd = None, match_tol
            for c in clusters:
                d = np.hypot(c["x"] - x, c["y"] - y)
                if d < bestd:
                    best, bestd = c, d
            if best is None:
                clusters.append({"x": x, "y": y, "n": 1, "frames": {fi}, "smaj": s})
            else:
                n = best["n"]
                best["x"] = (best["x"] * n + x) / (n + 1)
                best["y"] = (best["y"] * n + y) / (n + 1)
                best["n"] = n + 1
                best["frames"].add(fi)
                best["smaj"] = max(best["smaj"], s)
    need = max(2, int(round(min_frac * n_frames)))
    return [(c["x"], c["y"], c["smaj"]) for c in clusters if len(c["frames"]) >= need]


def half_flux_diameter(stamp, r_out, k_thresh=3.0, min_pixels=5) -> float:
    """HFD of a single-star stamp.

    Subtract a robust per-stamp border background, locate the flux centroid (on
    the non-negative image, for stability), then form ``2 * <r>`` over the
    aperture using the **background-subtracted, un-clamped** values: zero-mean
    background noise then cancels in the sums, so the star's wings -- where the
    defocus signal lives -- are retained.  (Clamping to >=0 was the original
    bug: it rectifies noise to a positive pedestal that fills the aperture and
    flattens HFD vs focus; a hard threshold instead throws away the wings and
    *also* flattens it.)  ``k_thresh`` is used only as a centroid-stability /
    significance check.  Returns ``nan`` if the aperture flux is not positive.
    """
    ny, nx = stamp.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    border = np.concatenate([stamp[0], stamp[-1], stamp[1:-1, 0], stamp[1:-1, -1]])
    bg = np.median(border)
    noise = 1.4826 * np.median(np.abs(border - bg)) or 1.0
    v = stamp - bg
    vc = np.clip(v, 0.0, None)
    if (v > k_thresh * noise).sum() < min_pixels or vc.sum() <= 0:
        return float("nan")
    cx = float((vc * xx).sum() / vc.sum())
    cy = float((vc * yy).sum() / vc.sum())
    r = np.hypot(xx - cx, yy - cy)
    m = r <= r_out
    s = v[m].sum()
    if s <= 0:
        return float("nan")
    return float(2.0 * (v[m] * r[m]).sum() / s)


def frame_hfd(
    image,
    nsigma=3.0,  # detection threshold, sigma above background
    npixels=5,  # min connected pixels for a source
    box_size=64,  # 2D background box
    k_thresh=3.0,  # per-stamp star/background threshold (x border noise)
    r_factor=6.0,  # HFD aperture = r_factor x the source's major sigma (adaptive
    #                to defocus, so broad/donut stars are not clipped)
    r_min=8.0,
    r_max=70.0,
    stamp_pad=6,  # stamp half-size = aperture + this
    min_stars=1,  # need at least this many usable stars, else invalid
    max_value=63000.0,  # reject saturated stars (raw ADU)
    near_axis_frac=1.0,  # keep stars within this fraction of the field radius;
    #                      1.0 = all (focus ROIs are small).  For a FULL-FRAME
    #                      focus image set < 1 so coma-inflated margins are
    #                      excluded -- the opposite of optical-center finding.
    center=None,  # optical center (cx, cy) in THIS image's pixel frame; when given
    radius=None,  # with radius, keep stars within this low-coma disk (from the
    #               unit's optical-center calibration) instead of near_axis_frac.
    min_area=4,
    max_area=1e5,
) -> tuple[float, int]:
    """Median HFD of the (near-axis) stars in a focus image.

    Stars are restricted to the low-coma zone: the optical-center disk
    ``(center, radius)`` when the unit is calibrated, else ``near_axis_frac`` of
    the field about the geometric center (see :func:`_low_coma_keep`).  Returns
    ``(hfd_median, n_stars)``; ``(nan, 0)`` when no usable star is found -- real
    focus sweeps routinely have very few or zero stars in the small ROI, so the
    caller marks such a sample invalid rather than failing the run.
    """
    data = _load(image)
    ny, nx = data.shape
    data_sub = _bg_subtract(data, box_size)
    x, y, smaj = _detect(data_sub, nsigma, npixels, min_area, max_area)
    if len(x) == 0:
        return float("nan"), 0
    keep = _low_coma_keep(x, y, (ny, nx), center, radius, near_axis_frac)
    stars = list(zip(x[keep], y[keep], smaj[keep]))
    hfds = _measure_hfds_at(data, data_sub, stars, r_factor, r_min, r_max, stamp_pad, k_thresh, max_value)
    if len(hfds) < min_stars:
        return float("nan"), 0
    return float(np.median(hfds)), len(hfds)


def measure_sweep_hfd(
    images,
    nsigma=2.5,
    npixels=5,
    box_size=64,
    k_thresh=3.0,
    r_factor=6.0,
    r_min=8.0,
    r_max=70.0,
    stamp_pad=6,
    min_stars=1,
    max_value=63000.0,
    near_axis_frac=1.0,
    center=None,  # optical center (cx, cy) in the sweep's pixel frame; with radius,
    radius=None,  # restrict to this low-coma disk instead of near_axis_frac.
    min_area=4,
    max_area=1e5,
    match_tol=3.0,
    min_frac=0.6,
) -> tuple[list[tuple[float, int]], int]:
    """Joint HFD over a focus sweep using a CONSISTENT star set.

    A sweep's frames share pointing, so the same stars sit at the same pixels.
    Detecting each frame independently and taking the median lets the detected
    *set* wobble frame-to-frame, which adds noise that buries the (often shallow)
    V-curve.  Instead we cross-match detections into stars seen across the sweep
    and measure HFD at those fixed positions in every frame -- giving the clean,
    near-monotonic curve the external (catalog-matched) analyzer produces.

    Returns ``(per_frame, n_consistent)``; ``per_frame[i] = (hfd_median, n_used)``
    aligned with ``images[i]``.
    """
    loaded = [_load(im) for im in images]
    subs = [_bg_subtract(d, box_size) for d in loaded]
    detections = [list(zip(*_detect(ds, nsigma, npixels, min_area, max_area))) for ds in subs]
    stars = _consistent_stars(detections, len(images), match_tol, min_frac)
    if stars and (radius is not None or near_axis_frac < 1.0):
        sx = np.array([s[0] for s in stars])
        sy = np.array([s[1] for s in stars])
        keep = _low_coma_keep(sx, sy, loaded[0].shape, center, radius, near_axis_frac)
        stars = [st for st, k in zip(stars, keep) if k]

    per_frame = []
    for data, ds in zip(loaded, subs):
        hfds = _measure_hfds_at(data, ds, stars, r_factor, r_min, r_max, stamp_pad, k_thresh, max_value)
        per_frame.append((float(np.median(hfds)) if len(hfds) >= min_stars else float("nan"), len(hfds)))
    return per_frame, len(stars)


def assess_focus_regime(image, near_hfd_max=None, **frame_kw) -> str:
    """Phase-0 triage of a single frame: ``"near"`` | ``"far"`` | ``"empty"``.

    ``"near"`` = usable point-source HFD (and, if ``near_hfd_max`` is given, below
    it) -> go to the V-curve; ``"far"`` = structure present but no point-source
    HFD (large blobs / donuts) -> coarse acquisition; ``"empty"`` = nothing.
    Deliberately simple in v1; the donut/cold-start handling matures with Phase 2.
    """
    valid = {k: v for k, v in frame_kw.items() if k in frame_hfd.__code__.co_varnames}
    hfd, n = frame_hfd(image, **valid)
    if n > 0 and np.isfinite(hfd):
        return "near" if (near_hfd_max is None or hfd <= near_hfd_max) else "far"
    data = _load(image)
    bg = np.median(data)
    mad = np.median(np.abs(data - bg)) or 1.0
    bright_frac = float(np.mean(data > bg + 8 * 1.4826 * mad))
    return "far" if bright_frac > 0.002 else "empty"
