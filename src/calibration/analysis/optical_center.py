"""Optical-center finding from a sky image via coma elongation.

For the 61cm f/3 parabola, coma elongates each star roughly *radially* about
the optical axis, with magnitude growing ~linearly with field radius.  The
elongation therefore vanishes at the optical center, and every star's major
axis points (anti-)radially through it.  We recover the center as the point
through which those major-axis lines best pass, weighted so that nearly-round
(low-coma, noisy-orientation) sources near the axis contribute little.

This is the working successor to the earlier ``src/science/coma.py`` sketch,
whose least-squares solve and return were left commented out.

Design reference: unit self-calibration, section "Optical center
(coma elongation null)".
"""

import logging
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits
from astropy.stats import SigmaClip
from photutils.background import Background2D, MedianBackground
from photutils.segmentation import SourceCatalog, detect_sources, detect_threshold

from calibration.logging_context import init_calibration_log

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)

# NOTE: the pre-existing code below reports failures with bare `print()`, which
# on the unit goes nowhere -- the service's output is not the rotating log.  New
# code here logs instead; converting the old prints is a separate change.


@dataclass
class OpticalCenterResult:
    """Optical center plus provenance/quality for the per-unit calibration DB."""

    center_x: float
    center_y: float
    n_sources: int  # sources used in the final (post-clip) fit
    n_detected: int  # sources detected before filtering
    residual_rms: float  # RMS perpendicular distance of axes to center (pix)
    radiality: float  # spin-2: weighted <cos 2(theta_ellipse - radial)>; +1 radial, 0 random
    radiality_spin1: float  # spin-1: weighted <cos(theta_centroid-peak - radial)>; +1 = coma
    #                         points outward.  Cleaner coma confirmation than spin-2 (the
    #                         centroid-vs-peak offset is coma's odd/third-moment signature).
    image_shape: tuple  # (ny, nx)

    # per-source arrays kept for plotting / inspection (not persisted)
    x: np.ndarray | None = field(default=None, repr=False)
    y: np.ndarray | None = field(default=None, repr=False)
    theta: np.ndarray | None = field(default=None, repr=False)  # major-axis angle, rad
    weight: np.ndarray | None = field(default=None, repr=False)

    @property
    def center(self) -> tuple:
        return (self.center_x, self.center_y)


def extract_sources(image, nsigma=2.0, npixels=5, box_size=50, exclude_mask=None) -> dict | None:
    """Detect and measure every source in one frame -- with NO filtering applied.

    Split out of :func:`find_optical_center` so two consumers can share exactly
    one detection code path.  The distinction matters: the centre fit *wants*
    the aggressive cuts (``min_ellipticity``, ``min_field_radius``) because it
    needs clean, unambiguous orientations from margin stars -- but a coma-slope
    fit run on that same cut sample would be **biased**.  Truncating at a
    minimum ellipticity removes exactly the low-``e`` sources that anchor the
    origin end of an ``e = k*r`` relation, so the surviving sample is
    e-truncated and the fitted slope comes out too steep, which in turn makes
    ``low_coma_radius = coma_tolerance / k`` too small.  So the slope fit
    re-extracts with its own (looser) cuts rather than reusing the centre fit's
    sample.

    Returns raw per-source arrays plus the background-subtracted image, or
    ``None`` when nothing was detected.
    """
    raw = image if isinstance(image, np.ndarray) else fits.getdata(image)
    data = np.asarray(raw, dtype=float)
    ny, nx_img = data.shape

    bkg = Background2D(
        data,
        box_size=(box_size, box_size),
        filter_size=(3, 3),
        sigma_clip=SigmaClip(sigma=3.0),
        bkg_estimator=MedianBackground(),  # type: ignore[arg-type]  (valid BackgroundBase)
    )
    data_sub = data - bkg.background

    threshold = detect_threshold(data_sub, n_sigma=nsigma, mask=exclude_mask)
    segm = detect_sources(data_sub, threshold, n_pixels=npixels, mask=exclude_mask)
    if segm is None:
        logger.warning("no sources detected; consider lowering nsigma or npixels")
        return None

    cat = SourceCatalog(data_sub, segm, background=bkg.background)
    x = np.asarray(cat.x_centroid, dtype=float)
    return {
        "data_sub": data_sub,
        "shape": (ny, nx_img),
        "x": x,
        "y": np.asarray(cat.y_centroid, dtype=float),
        "theta": np.asarray(cat.orientation.to("rad").value, dtype=float),
        "ellipticity": np.asarray(cat.ellipticity, dtype=float),
        "area": np.asarray(cat.area.value, dtype=float),
        "flux": np.asarray(cat.segment_flux, dtype=float),
        # peak pixel (for coma's spin-1 centroid-vs-peak offset)
        "peak_x": np.asarray(cat.max_value_xindex, dtype=float),
        "peak_y": np.asarray(cat.max_value_yindex, dtype=float),
        "n_detected": len(x),
    }


def _solve_center(x, y, theta, weights):
    """Weighted least-squares intersection of major-axis lines.

    Each source defines a line through (x_i, y_i) with direction
    u_i = (cos t_i, sin t_i).  The signed distance from a candidate center p
    to that line is n_i . (p - p_i), where n_i = (-sin t_i, cos t_i) is the
    line normal.  Minimising sum_i w_i (n_i . (p - p_i))^2 gives the normal
    equations M p = v.  This normal-vector form is numerically stable for all
    orientations (the original tan(theta) form blew up near vertical axes).

    Returns (px, py, residuals) where residuals are signed perpendicular
    distances of each line from the solved center.
    """
    nx = -np.sin(theta)
    ny = np.cos(theta)

    # M = sum w * n n^T  (2x2);  v = sum w * n (n . p_i)
    ndotp = nx * x + ny * y
    mxx = np.sum(weights * nx * nx)
    mxy = np.sum(weights * nx * ny)
    myy = np.sum(weights * ny * ny)
    vx = np.sum(weights * nx * ndotp)
    vy = np.sum(weights * ny * ndotp)

    m = np.array([[mxx, mxy], [mxy, myy]])
    v = np.array([vx, vy])
    px, py = np.linalg.solve(m, v)

    residuals = nx * (px - x) + ny * (py - y)
    return px, py, residuals


def _coma_radiality(x, y, theta, flux, peak_x, peak_y, weights, px, py, min_sources):
    """How radial the inlier elongation field is about the fitted center (px, py).

    Two estimators, returned as ``(radiality, radiality_spin1, gate_val, gate_name)``:
      spin-2 (ellipse major axis):  ``<cos 2(theta - theta_radial)>``, headless.
      spin-1 (centroid-vs-peak):    ``<cos(theta_cp - theta_radial)>``, DIRECTED --
        coma shifts the flux centroid outward off the peak, so this also confirms
        the *sense* (outward), which a pure ellipse cannot.  It is the cleaner coma
        confirmation (the centroid-peak offset is coma's odd/third-moment signature)
        but too noisy to *fit* the center -- hence the ellipse fits, spin-1 gates.
    Each is +1 = radial coma, 0 = random, -1 = tangential/inward.  ``gate_val`` is
    spin-1 when enough resolved offsets exist, else spin-2 (centroid-peak needs
    supra-pixel offsets to beat peak-quantization noise).
    """
    rad = np.arctan2(y - py, x - px)
    radiality = float(np.sum(weights * np.cos(2 * (theta - rad))) / (np.sum(weights) or 1.0))

    ox, oy = x - peak_x, y - peak_y
    omag = np.hypot(ox, oy)
    cp_ok = omag > 0.5  # sub-pixel offsets are peak-quantization noise
    w1 = flux * omag
    if cp_ok.sum() >= min_sources and np.sum(w1[cp_ok]) > 0:
        theta_cp = np.arctan2(oy[cp_ok], ox[cp_ok])
        spin1 = float(np.sum(w1[cp_ok] * np.cos(theta_cp - rad[cp_ok])) / np.sum(w1[cp_ok]))
        return radiality, spin1, spin1, "spin-1 (centroid-peak)"
    return radiality, float("nan"), radiality, "spin-2 (ellipse)"


def find_optical_center(
    image,
    nsigma=2.0,  # detection threshold, in sigma above background
    npixels=5,  # min connected pixels for a source
    box_size=50,  # box for 2D background estimation
    min_area=10,  # min source area (pix) for a valid source
    max_area=1e5,  # max source area (pix) for a valid source
    min_ellipticity=0.05,  # drop near-round sources (orientation is noise)
    min_field_radius=0.4,  # use only sources beyond this fraction of the max field
    #                        radius: coma grows with field radius, so margin stars
    #                        carry the clean signal (faster, less noise-prone)
    middle_third=False,  # if True, filter sources to the central third -- but coma
    #                      lives in OFF-axis stars, so that starves the fit; off by default
    clip_sigma=3.0,  # outlier rejection on axis-to-center residuals
    max_iter=5,
    min_sources=12,  # need enough spread stars; a few clustered ones overfit a center
    min_radiality=0.25,  # coma signal floor; 19 real no-coma frames scattered up to 0.18
    exclude_mask=None,  # pixels to keep out of detection (e.g. folding-mirror shadow)
    plot_results=False,
) -> OpticalCenterResult | None:
    """Estimate the optical center of a sky image from coma elongation.

    ``image`` is a FITS path or a 2D array.  ``exclude_mask`` (a boolean array
    True where pixels should be ignored) keeps regions such as the folding-mirror
    shadow out of source detection, so leak-through ghosts don't enter the fit.
    Note: optical-center calibration is meant to run on a *retracted* (clean)
    frame; `mirror_shadow.detect_mirror_shadow` should guard that precondition.
    ``exclude_mask`` is only a best-effort fallback for an unexpected shadow.

    Returns an :class:`OpticalCenterResult`, or ``None`` if too few usable
    sources were found, or if the elongation field is not radial enough to be
    coma (``radiality < min_radiality`` -- the frame has no usable coma signal,
    so the center is indeterminate rather than wrong-but-confident).
    """
    extracted = extract_sources(image, nsigma=nsigma, npixels=npixels, box_size=box_size, exclude_mask=exclude_mask)
    if extracted is None:
        return None
    return solve_optical_center(
        [extracted],
        min_area=min_area,
        max_area=max_area,
        min_ellipticity=min_ellipticity,
        min_field_radius=min_field_radius,
        middle_third=middle_third,
        clip_sigma=clip_sigma,
        max_iter=max_iter,
        min_sources=min_sources,
        min_radiality=min_radiality,
        plot_results=plot_results,
    )


def solve_optical_center(
    extractions: list[dict],
    *,
    min_area=10,
    max_area=1e5,
    min_ellipticity=0.05,
    min_field_radius=0.4,
    middle_third=False,
    clip_sigma=3.0,
    max_iter=5,
    min_sources=12,
    min_radiality=0.25,
    plot_results=False,
) -> OpticalCenterResult | None:
    """Fit the optical center from one or more frames' extracted sources.

    ``extractions`` are :func:`extract_sources` outputs.  With several frames
    their sources are **pooled into a single weighted fit** rather than fitting
    each frame and averaging centres -- per-frame centres scatter by ~10^2 px
    (the design's motivation for N frames in the first place), and an average of
    scattered centres inherits that scatter, while pooling lets every source
    constrain one solution.

    Pooling is legitimate even across *different pointings*: the optical center
    is a property of the optics and detector, not of the sky, so each star --
    wherever the mount was aimed -- contributes one line through the same
    detector point.  The frames must share a detector geometry, though; mixed
    shapes are refused rather than silently mixing coordinate frames.

    Single-frame behaviour is IDENTICAL to the pre-refactor
    ``find_optical_center`` (verified against real frames to 9 decimals).
    """
    if not extractions:
        return None
    shapes = {tuple(e["shape"]) for e in extractions}
    if len(shapes) > 1:
        logger.error(f"solve_optical_center: mixed frame shapes {sorted(shapes)}; refusing to pool")
        return None
    ny, nx_img = extractions[0]["shape"]

    x = np.concatenate([e["x"] for e in extractions])
    y = np.concatenate([e["y"] for e in extractions])
    theta = np.concatenate([e["theta"] for e in extractions])
    ellip = np.concatenate([e["ellipticity"] for e in extractions])
    area = np.concatenate([e["area"] for e in extractions])
    flux = np.concatenate([e["flux"] for e in extractions])
    peak_x = np.concatenate([e["peak_x"] for e in extractions])
    peak_y = np.concatenate([e["peak_y"] for e in extractions])
    n_detected = int(sum(e["n_detected"] for e in extractions))
    data_sub = extractions[0]["data_sub"]  # for plotting only

    # 4a) Filter spurious / unusable sources.  (coma.py filtered on the row
    # *count* by mistake -- `tbl["area"].size` -- so its mask was all-or-nothing.)
    keep = (area >= min_area) & (area <= max_area) & np.isfinite(theta) & (ellip >= min_ellipticity) & np.isfinite(flux)
    if min_field_radius > 0:
        # keep margin stars only, where coma (radial elongation) is pronounced
        rr = np.hypot(x - (nx_img - 1) / 2, y - (ny - 1) / 2)
        keep &= rr >= min_field_radius * np.hypot((nx_img - 1) / 2, (ny - 1) / 2)
    if middle_third:
        # optical axis expected within the central third of the frame
        keep &= (x > nx_img / 3) & (x < 2 * nx_img / 3)
        keep &= (y > ny / 3) & (y < 2 * ny / 3)

    x, y, theta, ellip, flux = x[keep], y[keep], theta[keep], ellip[keep], flux[keep]
    peak_x, peak_y = peak_x[keep], peak_y[keep]
    if len(x) < 3:
        print(f"Only {len(x)} usable sources after filtering; need >= 3.")
        return None

    # 5) Weighted convergence fit with iterative outlier rejection.
    # Weight by FLUX * ellipticity: the orientation uncertainty of a source
    # scales ~1/(SNR * ellipticity), so bright, clearly-elongated stars carry
    # the coma direction.  Weighting by ellipticity alone (the old scheme) gave
    # faint, noise-elongated stars equal say and washed out the coma signal on
    # real frames where it lives in a handful of bright off-axis stars.
    weights = flux * ellip
    mask = np.ones(len(x), dtype=bool)
    px = py = None
    residuals = np.zeros(len(x))
    for _ in range(max_iter):
        px, py, residuals = _solve_center(x[mask], y[mask], theta[mask], weights[mask])
        # recompute residuals for *all* sources against the current solution
        all_res = -np.sin(theta) * (px - x) + np.cos(theta) * (py - y)
        sigma = np.std(all_res[mask]) or 1.0
        new_mask = np.abs(all_res) <= clip_sigma * sigma
        if np.array_equal(new_mask, mask) or new_mask.sum() < 3:
            mask = new_mask if new_mask.sum() >= 3 else mask
            residuals = all_res
            break
        mask = new_mask
        residuals = all_res
    assert px is not None and py is not None  # the loop runs >= 1 iteration (max_iter >= 1)

    n_used = int(mask.sum())
    residual_rms = float(np.sqrt(np.mean(residuals[mask] ** 2)))

    # 6) Quality gates.
    # 6a) Enough spread sources: a handful of clustered sources (e.g. leak-through
    # ghosts in a shadow band) can be overfit by a center that looks radial.
    if n_used < min_sources:
        print(f"Only {n_used} usable sources in the final fit; need >= {min_sources} for a reliable center.")
        return None

    # 6b) Radiality gate: confirm the inlier elongation field is genuinely
    # radial coma about the fitted center (see _coma_radiality).
    radiality, radiality_spin1, gate_val, gate_name = _coma_radiality(
        x[mask],
        y[mask],
        theta[mask],
        flux[mask],
        peak_x[mask],
        peak_y[mask],
        weights[mask],
        px,
        py,
        min_sources,
    )
    if gate_val < min_radiality:
        print(
            f"No usable coma signal: {gate_name} radiality={gate_val:.2f} < {min_radiality} "
            "(elongations are not radially outward -> optical center indeterminate)."
        )
        return None

    result = OpticalCenterResult(
        center_x=float(px),
        center_y=float(py),
        n_sources=n_used,
        n_detected=n_detected,
        residual_rms=residual_rms,
        radiality=radiality,
        radiality_spin1=radiality_spin1,
        image_shape=(ny, nx_img),
        x=x,
        y=y,
        theta=theta,
        weight=weights,
    )

    if plot_results:
        _plot(data_sub, result, mask)

    return result


@dataclass
class ComaSlope:
    """The measured coma gradient and the low-coma disk it implies."""

    slope: float  # k: ellipticity per pixel of field radius
    low_coma_radius: float | None  # coma_tolerance / k, px; None when k is untrustworthy
    coma_tolerance: float  # the ellipticity budget the radius was derived from
    n_sources: int
    residual_rms: float  # of (e - k*r), in ellipticity units
    r_max: float  # largest field radius sampled -- the radius is EXTRAPOLATED beyond this


def fit_coma_slope(
    x,
    y,
    ellipticity,
    weights,
    center,
    coma_tolerance: float,
    min_sources: int = 12,
    max_radius_factor: float = 3.0,
) -> ComaSlope | None:
    """Fit ``e = k*r`` through the origin and derive the low-coma radius.

    Coma elongation grows ~linearly with field radius and vanishes on axis, so
    the fit is **forced through the origin** -- an intercept would be fitting
    seeing and centroid noise, not coma.  Weighted the same way the centre fit
    is (flux x ellipticity), because the same sources carry the signal.

    ``low_coma_radius = coma_tolerance / k`` is then the radius at which coma
    elongation reaches the tolerance budget -- the disk autofocus should stay
    inside.

    Returns ``None`` rather than a fabricated number when the slope cannot be
    trusted (non-positive, non-finite, or too few sources).  That is deliberate:
    ``OpticalCenterCalibration.low_coma_radius`` is nullable precisely so focus
    can fall back to its geometric disk instead of being handed a bogus one.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    e = np.asarray(ellipticity, dtype=float)
    w = np.asarray(weights, dtype=float)

    cx, cy = center
    r = np.hypot(x - cx, y - cy)
    good = np.isfinite(r) & np.isfinite(e) & np.isfinite(w) & (w > 0) & (r > 0)
    r, e, w = r[good], e[good], w[good]
    if len(r) < min_sources:
        logger.warning(f"coma slope: only {len(r)} usable sources; need >= {min_sources}")
        return None

    # Weighted least squares through the origin: k = sum(w*r*e) / sum(w*r^2).
    denom = float(np.sum(w * r * r))
    if denom <= 0:
        return None
    k = float(np.sum(w * r * e) / denom)
    if not np.isfinite(k) or k <= 0:
        logger.warning(f"coma slope: k={k} is not a usable gradient (expected > 0)")
        return None

    residual_rms = float(np.sqrt(np.average((e - k * r) ** 2, weights=w)))
    r_max = float(r.max())
    radius = coma_tolerance / k

    # A radius far beyond the sampled field is an extrapolation, not a
    # measurement: with a shallow k the formula happily returns a disk larger
    # than the detector.  Report it as unknown rather than as a huge number that
    # would silently disable the low-coma restriction it exists to impose.
    if radius > max_radius_factor * r_max:
        logger.warning(
            f"coma slope: low_coma_radius={radius:.0f}px exceeds "
            f"{max_radius_factor}x the sampled field radius ({r_max:.0f}px) -- "
            f"coma is too shallow to place the disk; recording None"
        )
        return ComaSlope(
            slope=k,
            low_coma_radius=None,
            coma_tolerance=coma_tolerance,
            n_sources=len(r),
            residual_rms=residual_rms,
            r_max=r_max,
        )

    return ComaSlope(
        slope=k,
        low_coma_radius=float(radius),
        coma_tolerance=coma_tolerance,
        n_sources=len(r),
        residual_rms=residual_rms,
        r_max=r_max,
    )


def _plot(data_sub, result, mask):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 8))
    vmin = np.percentile(data_sub, 5)
    vmax = np.percentile(data_sub, 99)
    ax.imshow(data_sub, origin="lower", cmap="gray", vmin=vmin, vmax=vmax)
    ax.set_title("Sources & estimated optical center")
    ax.set_xlabel("X (pix)")
    ax.set_ylabel("Y (pix)")

    ll = 40.0
    for i in range(len(result.x)):
        x0, y0, t = result.x[i], result.y[i], result.theta[i]
        color = "yellow" if mask[i] else "gray"
        ax.plot(x0, y0, "o", ms=3, color=color)
        dx, dy = ll * np.cos(t), ll * np.sin(t)
        ax.plot([x0 - dx, x0 + dx], [y0 - dy, y0 + dy], color=color, lw=1)

    ax.plot(result.center_x, result.center_y, "+", color="magenta", ms=18, mew=2)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    # here = os.path.dirname(os.path.abspath(__file__))
    # res = find_optical_center(os.path.join(here, "sample.fits"), plot_results=False)
    res = find_optical_center("D:/MAST/tmp/Samples/sample1.fits", plot_results=False)
    if res is not None:
        print(
            f"optical center = ({res.center_x:.2f}, {res.center_y:.2f}) pix  "
            f"| {res.n_sources}/{res.n_detected} sources  "
            f"| residual RMS = {res.residual_rms:.2f} pix  "
            f"| radiality spin2={res.radiality:.2f} spin1={res.radiality_spin1:.2f}  "
            f"| image {res.image_shape}"
        )
