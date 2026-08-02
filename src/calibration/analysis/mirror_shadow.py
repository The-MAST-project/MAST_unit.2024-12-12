"""Folding-mirror shadow: detection and marking.

The unit's pick-off stage carries a 45-deg folding mirror on a linear
translation stage in the converging beam (design ref: unit self-calibration,
section "Pick-off stage geometry").  Retracted, the field is clean; inserted,
the mirror occults a band of sky and casts a roughly rectangular shadow with
graded (penumbral) edges.  The stage path is not aligned with the detector
x-axis, so the band is usually *tilted*.  The mirror is not fully opaque, so
bright stars leak through and leave artifacts inside the band.

This module:
  * detects the shadow band, or reports its absence, in a single science image
    (an optional retracted ``reference`` frame enables the cleaner ratio mode),
  * models it as a tilted band -- a centerline (angle + offset) plus umbra and
    penumbra half-widths,
  * marks it as umbra / penumbra masks for downstream use.

The centerline you'd otherwise compute separately is just ``(angle, offset)``.

Still to come (separate functions, stubbed below): darkening the band, and
masking the bright-star leak-through artifacts before the image is passed
down the pipeline.

Earlier sketches superseded here: ``src/science/find_vertical_obstruction.py``
(column sums -> vertical only) and the ``mask_linear_shadow`` half of
``src/tools/vigneting/vignetting.py`` (argmin of an X-profile -> vertical only).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import scipy.ndimage as ndi
from astropy.io import fits
from astropy.modeling.fitting import LinearLSQFitter
from astropy.modeling.models import Polynomial2D


@dataclass
class ShadowModel:
    """A folding-mirror shadow modelled as a tilted band on the detector.

    The band's long centerline has orientation ``angle`` (rad from +x) and
    passes at signed perpendicular distance ``offset`` (pix) from the image
    center.  ``umbra_half_width`` / ``penumbra_half_width`` give its cross
    section.  ``depth`` (peak fractional flux deficit) and ``prominence``
    (trough prominence over noise) are the quality figures behind ``present``.
    """

    present: bool
    image_shape: tuple  # (ny, nx)

    angle: float = 0.0  # long-axis orientation, rad from +x, in (-pi/2, pi/2]
    offset: float = 0.0  # signed perp distance from image center to centerline (pix)
    umbra_half_width: float = 0.0
    penumbra_half_width: float = 0.0

    depth: float = 0.0  # peak fractional flux deficit (0..1)
    prominence: float = 0.0  # trough prominence / noise (detection SNR)
    baseline: float = 0.0  # deficit floor away from the band

    # perpendicular cross-section kept for plotting / inspection (not persisted)
    s: np.ndarray | None = field(default=None, repr=False)  # perpendicular coordinate (pix)
    profile: np.ndarray | None = field(default=None, repr=False)  # mean deficit vs s

    @property
    def tilt_deg(self) -> float:
        return float(np.degrees(self.angle))

    def _grids(self):
        ny, nx = self.image_shape
        cx, cy = (nx - 1) / 2.0, (ny - 1) / 2.0
        yy, xx = np.mgrid[0:ny, 0:nx]
        # perpendicular coordinate: projection onto the band normal (-sin a, cos a)
        s = -(xx - cx) * np.sin(self.angle) + (yy - cy) * np.cos(self.angle)
        return s

    def masks(self):
        """Return ``(umbra_mask, penumbra_mask)`` boolean arrays for the band.

        Penumbra excludes the umbra (the two are disjoint), matching the
        convention in ``find_vertical_obstruction``.  Both are empty when the
        shadow is absent.
        """
        ny, nx = self.image_shape
        if not self.present:
            empty = np.zeros((ny, nx), dtype=bool)
            return empty, empty.copy()
        d = np.abs(self._grids() - self.offset)
        umbra = d <= self.umbra_half_width
        penumbra = (d <= self.penumbra_half_width) & ~umbra
        return umbra, penumbra

    def mask(self, include_penumbra=True):
        """Union boolean mask of band pixels (umbra, and penumbra by default)."""
        umbra, penumbra = self.masks()
        return (umbra | penumbra) if include_penumbra else umbra

    def centerline_endpoints(self):
        """Endpoints of the long centerline clipped to the image box, or None."""
        if not self.present:
            return None
        ny, nx = self.image_shape
        cx, cy = (nx - 1) / 2.0, (ny - 1) / 2.0
        nrm = np.array([-np.sin(self.angle), np.cos(self.angle)])  # band normal
        d = np.array([np.cos(self.angle), np.sin(self.angle)])  # along the band
        c = np.array([cx, cy]) + self.offset * nrm  # a point on the centerline
        return _clip_line_to_box(c, d, nx, ny)


def detect_mirror_shadow(
    image,
    reference=None,
    near_vertical_deg=25.0,  # search within this of vertical; the pick-off stage
    angle_step_deg=2.0,  #       axis lands near the detector y-axis on this hardware
    star_kernel=5,  # median-filter size (decimated px) to suppress stars
    min_depth=0.04,  # peak fractional deficit (vs sky continuum) to call present
    min_prominence=6.0,  # dip depth / profile noise to call present
    min_penumbra_half_width=60.0,  # the mirror band is wide; reject thin troughs (pix)
    edge_margin=120.0,  # reject centerlines hugging the frame edge (pix)
    umbra_frac=0.5,  # of peak depth: >= this is umbra
    penumbra_frac=0.1,  # of peak depth: >= this is (pen)umbra
    bin_px=16.0,  # perpendicular binning (full-res pix)
    continuum_frac=0.5,  # sky-continuum closing element, as a fraction of the profile
    search_downsample=8,  # decimation factor (speed)
) -> ShadowModel:
    """Detect a folding-mirror shadow band, or report its absence.

    ``image`` is a FITS path or a 2D array.  A retracted ``reference`` frame
    (same field, comparably exposed) is divided out first when supplied.

    The band is found by suppressing stars (median filter), projecting onto the
    band-normal over a **near-vertical** angle range, and referencing each
    perpendicular profile to a sky *continuum* (a wide median, window >> band) so
    the band shows as a localized dip.  This replaces the earlier global-poly
    illumination, which **absorbed** wide bands (a real ~8% shadow read as
    ~0.6%).  The near-vertical restriction matches every observed shadow on this
    hardware and keeps the search from locking onto spurious off-axis features;
    widen ``near_vertical_deg`` (toward 90) if a genuinely tilted band appears.

    Always returns a :class:`ShadowModel`; check ``.present``.
    """
    data = _load(image)
    ny, nx = data.shape
    cx, cy = (nx - 1) / 2.0, (ny - 1) / 2.0

    # 1) Star-suppressed sky map on a decimated copy.  Against a reference frame,
    #    divide it out first so the band is a dip in the (otherwise ~flat) ratio.
    ds = max(1, int(search_downsample))
    base = data
    if reference is not None:
        ref = _load(reference)
        if ref.shape != data.shape:
            raise ValueError(f"reference shape {ref.shape} != image shape {data.shape}")
        base = data / np.maximum(ref, 1.0)
    sup = ndi.median_filter(base[::ds, ::ds].astype(float), size=star_kernel)
    yyd, xxd = np.mgrid[0:ny:ds, 0:nx:ds]
    xcd = xxd.ravel() - cx
    ycd = yyd.ravel() - cy
    w = sup.ravel()

    # 2) Near-vertical sweep: project, reference to a wide-median sky continuum,
    #    and score the deepest localized dip (depth / noise).
    edge_bins = int(edge_margin / bin_px)
    angles = np.deg2rad(_near_vertical_angles(near_vertical_deg, angle_step_deg))
    best = None
    for a in angles:
        s_c, prof, counts = _project(xcd, ycd, w, a, bin_px)
        res = _band_dip(s_c, prof, counts, continuum_frac, edge_bins)
        if res is not None and (best is None or res["score"] > best["score"]):
            best = {**res, "angle": a}

    if best is None:
        return ShadowModel(present=False, image_shape=(ny, nx))

    # 3) Cross-section at the best angle: umbra/penumbra runs on the dip profile
    #    (frac, baseline 0); centerline = midpoint of the penumbra-to-penumbra run.
    frac, s_c, peak_i = best["frac"], best["s"], best["peak_i"]
    depth = float(frac[peak_i])
    u_lo, u_hi = _run_bounds(frac, peak_i, umbra_frac * depth)
    p_lo, p_hi = _run_bounds(frac, peak_i, penumbra_frac * depth)
    center = 0.5 * (p_lo + p_hi)
    offset = float(np.interp(center, np.arange(len(s_c)), s_c))
    umbra_half = 0.5 * (u_hi - u_lo) * bin_px
    penumbra_half = 0.5 * (p_hi - p_lo) * bin_px
    score = float(best["score"])

    present = depth >= min_depth and score >= min_prominence and penumbra_half >= min_penumbra_half_width

    return ShadowModel(
        present=bool(present),
        image_shape=(ny, nx),
        angle=float(best["angle"]),
        offset=offset,
        umbra_half_width=float(umbra_half),
        penumbra_half_width=float(penumbra_half),
        depth=depth,
        prominence=score,
        baseline=0.0,
        s=s_c,
        profile=frac,
    )


def darken_shadow(
    image,
    model: ShadowModel,
    background=None,
    fill=None,
    add_noise=True,
    include_penumbra=True,
    collar_px=30,
    rng=None,
):
    """Neutralise the shadow band so leak-through artifacts don't reach the pipeline.

    The folding mirror is not fully opaque, so bright stars under it survive as
    *dimmed ghosts* and the sky itself is suppressed.  The band's science is
    compromised either way, so rather than try to un-vignette it we replace the
    whole band with an estimate of the sky that should be there -- erasing both
    the dip and the ghosts -- leaving a frame the downstream extractor / solver
    reads as clean sky.  This is the "mask the leak-through before the pipeline"
    step; ``model.mask()`` is the corresponding boolean mask.

    Parameters
    ----------
    image : FITS path or 2D array.
    model : ShadowModel from :func:`detect_mirror_shadow`.
    background : optional sky to fill with (scalar or array); default re-fits the
        same low-order illumination used in detection (which, by construction,
        does not dip inside the band -- so it *is* the missing sky).
    fill : if given (scalar or ``np.nan``), fill the band with this constant
        instead of the sky estimate -- ``np.nan`` for mask-aware downstreams,
        ``0`` for hard blanking.  Overrides ``background`` / ``add_noise``.
    add_noise : match sky shot-noise so the patch isn't a tell-tale flat region.
    include_penumbra : darken the penumbra too (partially-dimmed stars there have
        wrong photometry); set False to darken only the umbra.

    Returns a new float image (unchanged copy if ``model.present`` is False).
    """
    data = _load(image)
    out = data.copy()
    if not model.present:
        return out

    mask = model.mask(include_penumbra=include_penumbra)
    if fill is not None:
        out[mask] = fill
        return out

    if background is None:
        # Refit the sky with the band excluded so the poly interpolates the true
        # sky across it, rather than the band-biased fit detection used.
        background = _fit_illumination(data, exclude_mask=mask)
    bg = background if np.ndim(background) else np.full(data.shape, float(background))

    # A low-order poly is too stiff to nail the *local* sky level, so match the
    # fill to the sky in a thin collar hugging the band: correct the residual
    # offset, and draw the fill noise from the collar's pixel-to-pixel scatter
    # (poly residuals still carry vignetting structure and overstate the noise).
    collar = ndi.binary_dilation(mask, iterations=int(collar_px)) & ~mask
    resid = (data - bg)[collar]
    med = np.median(resid)
    mad = 1.4826 * np.median(np.abs(resid - med)) or 1.0
    inliers = resid[np.abs(resid - med) < 3 * mad]
    offset_corr = float(np.median(inliers)) if inliers.size else 0.0
    out[mask] = bg[mask] + offset_corr

    if add_noise:
        highpass = data - ndi.median_filter(data, size=5)
        sky_sigma = 1.4826 * np.median(np.abs(highpass[collar])) or 1.0
        if rng is None:
            rng = np.random.default_rng()
        out[mask] += rng.normal(0.0, sky_sigma, size=int(mask.sum()))

    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _project(xc, yc, w, angle, bin_width):
    """Project deficit weights ``w`` onto the band-normal axis at ``angle``.

    Returns ``(s_centers, profile, counts)`` -- the per-bin mean deficit
    (``profile``) and pixel counts versus perpendicular coordinate ``s``.
    """
    s = -xc * np.sin(angle) + yc * np.cos(angle)
    s_lo = s.min()
    idx = ((s - s_lo) / bin_width).astype(np.intp)
    nbins = int(idx.max()) + 1
    counts = np.bincount(idx, minlength=nbins).astype(float)
    sums = np.bincount(idx, weights=w, minlength=nbins)
    profile = np.where(counts > 0, sums / np.maximum(counts, 1), 0.0)
    s_centers = s_lo + (np.arange(nbins) + 0.5) * bin_width
    return s_centers, profile, counts


def _near_vertical_angles(near_vertical_deg, step):
    """Angles (deg) within ``near_vertical_deg`` of vertical (tilt = +/-90)."""
    lo = np.arange(-90.0, -90.0 + near_vertical_deg + 1e-9, step)
    hi = np.arange(90.0 - near_vertical_deg, 90.0 + 1e-9, step)
    return np.unique(np.concatenate([lo, hi]))


def _band_dip(s_centers, profile, counts, cont_frac, edge_bins):
    """Score the deepest localized dip in a star-suppressed perpendicular profile.

    References ``profile`` to a sky continuum via a grey-scale morphological
    *closing* with a structuring element spanning ``cont_frac`` of the profile:
    closing fills dips narrower than the element (the band, of any width up to
    that span) up to the surrounding sky while preserving broad trends
    (vignetting), so the band shows as a positive
    ``frac = (continuum - profile)/continuum`` dip.  This works for both wide-
    deep and wide-shallow bands and scales with frame size (a fixed wide median
    instead went near-global on small frames and got poisoned by a deep band).
    Returns ``dict(score, depth, frac, s, peak_i)`` or ``None``.
    """
    valid = counts >= 0.3 * counts.max()
    if valid.sum() < 5:
        return None
    pf = np.where(valid, profile, np.median(profile[valid]))
    se = max(3, int(cont_frac * len(pf)) | 1)
    cont = ndi.grey_closing(pf, size=se)
    frac = np.where(valid, (cont - profile) / np.maximum(cont, 1e-9), 0.0)

    vi = np.flatnonzero(valid)
    srch = valid.copy()
    srch[: vi[0] + edge_bins] = False
    srch[vi[-1] - edge_bins + 1 :] = False
    if not srch.any():
        return None
    noise = 1.4826 * np.median(np.abs(frac[valid] - np.median(frac[valid]))) or 1e-3
    peak_i = int(np.argmax(np.where(srch, frac, -np.inf)))
    depth = float(frac[peak_i])
    return {"score": depth / noise, "depth": depth, "frac": frac, "s": s_centers, "peak_i": peak_i}


def _load(image) -> np.ndarray:
    """Accept a FITS path or an array; return a float 2D image."""
    data = image if isinstance(image, np.ndarray) else fits.getdata(image)
    data = np.asarray(data, dtype=float)
    if data.ndim != 2:
        raise ValueError(f"expected a 2D image, got shape {data.shape}")
    return data


def _fit_illumination(img, order=2, n_iter=3, sigma=3.0, subsample=4, exclude_mask=None):
    """Robust low-order 2D illumination model (sigma-clipped polynomial).

    The clip rejects stars (positive residual) and, weakly, the shadow band, so
    the fit converges on the smooth, un-shadowed sky.  Pass ``exclude_mask`` (the
    detected band) once it is known: a wide, deep band biases even a clipped
    low-order poly downward, so excluding it outright lets the poly interpolate
    the true sky across the gap -- essential when the result is used to *fill*
    the band in :func:`darken_shadow`.
    """
    ny, nx = img.shape
    yy, xx = np.mgrid[0:ny:subsample, 0:nx:subsample]
    xf = xx.ravel().astype(float)
    yf = yy.ravel().astype(float)
    zf = img[::subsample, ::subsample].ravel().astype(float)

    good = np.isfinite(zf)
    if exclude_mask is not None:
        good &= ~exclude_mask[::subsample, ::subsample].ravel()
    fitter = LinearLSQFitter()
    model = Polynomial2D(degree=order)
    for _ in range(n_iter):
        model = fitter(Polynomial2D(degree=order), xf[good], yf[good], zf[good])
        resid = zf - model(xf, yf)
        std = np.std(resid[good]) or 1.0
        good = np.isfinite(zf) & (np.abs(resid) <= sigma * std)

    gy, gx = np.mgrid[0:ny, 0:nx]
    illum = model(gx.astype(float), gy.astype(float))
    # Guard against a poly dipping to <= 0 somewhere; deficit divides by this.
    floor = np.nanmedian(img) * 1e-3
    return np.maximum(illum, floor if floor > 0 else 1.0)


def _run_bounds(profile, peak_i, level):
    """Bounds ``(lo, hi)`` in bins of the contiguous run around ``peak_i`` above ``level``."""
    n = len(profile)
    lo = peak_i
    while lo - 1 >= 0 and profile[lo - 1] >= level:
        lo -= 1
    hi = peak_i
    while hi + 1 < n and profile[hi + 1] >= level:
        hi += 1
    return lo, hi


def _clip_line_to_box(c, d, nx, ny):
    """Clip the infinite line ``c + t*d`` to ``[0,nx-1] x [0,ny-1]``; endpoints or None."""
    ts = []
    for p0, dd, lo, hi, oa, ob, olo, ohi in (
        (c[0], d[0], 0.0, nx - 1.0, c[1], d[1], 0.0, ny - 1.0),
        (c[1], d[1], 0.0, ny - 1.0, c[0], d[0], 0.0, nx - 1.0),
    ):
        if dd == 0:
            continue
        for edge in (lo, hi):
            t = (edge - p0) / dd
            other = oa + t * ob
            if olo - 1e-6 <= other <= ohi + 1e-6:
                ts.append(t)
    if len(ts) < 2:
        return None
    t0, t1 = min(ts), max(ts)
    p_a = c + t0 * d
    p_b = c + t1 * d
    return (float(p_a[0]), float(p_a[1])), (float(p_b[0]), float(p_b[1]))


def plot_shadow(image, model: ShadowModel, vmin=None, vmax=None):
    """Show the image with umbra/penumbra contours and the centerline overlaid."""
    import matplotlib.pyplot as plt

    data = _load(image)
    if vmin is None:
        vmin = np.percentile(data, 5)
    if vmax is None:
        vmax = np.percentile(data, 99)

    fig, (ax, axp) = plt.subplots(1, 2, figsize=(13, 6))
    ax.imshow(data, origin="lower", cmap="gray", vmin=vmin, vmax=vmax)
    title = (
        f"shadow {model.tilt_deg:+.1f} deg, depth {model.depth:.2f}, prom {model.prominence:.1f}"
        if model.present
        else f"no shadow (depth {model.depth - model.baseline:.2f}, prom {model.prominence:.1f})"
    )
    ax.set_title(title)
    ax.set_xlabel("X (pix)")
    ax.set_ylabel("Y (pix)")

    if model.present:
        umbra, penumbra = model.masks()
        ax.contour(penumbra, levels=[0.5], colors="yellow", linewidths=1.0)
        ax.contour(umbra, levels=[0.5], colors="red", linewidths=1.2)
        ends = model.centerline_endpoints()
        if ends is not None:
            (x0, y0), (x1, y1) = ends
            ax.plot([x0, x1], [y0, y1], "-", color="cyan", lw=1.2)

    if model.profile is not None:
        axp.plot(model.s, model.profile, "-", color="k", lw=1)
        axp.axhline(model.baseline, color="gray", ls=":", lw=1)
        if model.present:
            axp.axvline(model.offset, color="cyan", lw=1)
            axp.axvspan(
                model.offset - model.umbra_half_width,
                model.offset + model.umbra_half_width,
                color="red",
                alpha=0.2,
            )
            axp.axvspan(
                model.offset - model.penumbra_half_width,
                model.offset + model.penumbra_half_width,
                color="yellow",
                alpha=0.15,
            )
        axp.set_title("perpendicular cross-section")
        axp.set_xlabel("perp. coordinate s (pix)")
        axp.set_ylabel("mean flux deficit")

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "D:/MAST/tmp/Samples/sample1.fits"
    m = detect_mirror_shadow(path)
    if m.present:
        print(
            f"shadow: tilt={m.tilt_deg:+.2f} deg  offset={m.offset:+.1f} pix  "
            f"umbra=+/-{m.umbra_half_width:.1f}  penumbra=+/-{m.penumbra_half_width:.1f}  "
            f"depth={m.depth:.3f}  prominence={m.prominence:.1f}"
        )
    else:
        print(f"no shadow detected (depth={m.depth - m.baseline:.3f}, prominence={m.prominence:.1f})")
    plot_shadow(path, m)
