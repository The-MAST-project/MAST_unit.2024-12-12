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

from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits
from astropy.stats import SigmaClip
from photutils.background import Background2D, MedianBackground
from photutils.segmentation import SourceCatalog, detect_sources, detect_threshold


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
    # 1) Load image
    raw = image if isinstance(image, np.ndarray) else fits.getdata(image)
    data = np.asarray(raw, dtype=float)
    ny, nx_img = data.shape

    # 2) Background subtraction
    bkg = Background2D(
        data,
        box_size=(box_size, box_size),
        filter_size=(3, 3),
        sigma_clip=SigmaClip(sigma=3.0),
        bkg_estimator=MedianBackground(),  # type: ignore[arg-type]  (valid BackgroundBase)
    )
    data_sub = data - bkg.background

    # 3) Detect sources (excluding masked regions, e.g. the shadow band)
    threshold = detect_threshold(data_sub, nsigma=nsigma, mask=exclude_mask)
    segm = detect_sources(data_sub, threshold, npixels=npixels, mask=exclude_mask)
    if segm is None:
        print("No sources detected. Consider lowering nsigma or npixels.")
        return None

    # 4) Measure source properties
    cat = SourceCatalog(data_sub, segm, background=bkg.background)
    x = np.asarray(cat.xcentroid, dtype=float)
    y = np.asarray(cat.ycentroid, dtype=float)
    theta = np.asarray(cat.orientation.to("rad").value, dtype=float)
    ellip = np.asarray(cat.ellipticity, dtype=float)
    area = np.asarray(cat.area.value, dtype=float)
    flux = np.asarray(cat.segment_flux, dtype=float)
    # peak pixel (for coma's spin-1 centroid-vs-peak offset)
    peak_x = np.asarray(cat.maxval_xindex, dtype=float)
    peak_y = np.asarray(cat.maxval_yindex, dtype=float)
    n_detected = len(x)

    # 4a) Filter spurious / unusable sources.  (coma.py filtered on the row
    # *count* by mistake -- `tbl["area"].size` -- so its mask was all-or-nothing.)
    keep = (
        (area >= min_area) & (area <= max_area)
        & np.isfinite(theta) & (ellip >= min_ellipticity) & np.isfinite(flux)
    )
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
        x[mask], y[mask], theta[mask], flux[mask],
        peak_x[mask], peak_y[mask], weights[mask], px, py, min_sources,
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
