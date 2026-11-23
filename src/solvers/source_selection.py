"""Which sources of a frame to hand to the plate solver, and why the others were dropped.

Stage 0 of `mastrometry_source_selection_design.md` (mast-claude-config, `plans/`): the pure
selection, with no solver and no unit. A frame goes in, a `Selection` comes out -- every detected
source with its measurements and its fate -- so the same function can be run on saved FITS files
(`tools/source_survey.py`) before anything is wired into `MastrometryDotNet`.

The steps, as in the design:

1. **Detect** with `calibration.analysis.optical_center.extract_sources` (the unit's one detection
   path): 50 px background mesh, 2 sigma, 5 px minimum. Nothing is filtered there.
2. **Measure** each source: a *fixed-window* centroid (SExtractor's `winpos`, sigma = seeing
   FWHM / 2.355, one sigma for the whole frame), its matched-filter SNR, raw peak, half-maximum
   FWHM, distance from the optical centre, and the fold-mirror shadow deficit at the source.
3. **Hard-reject** what biases a fit while still being matched: shadow, saturated, hot pixel,
   blended, truncated by an edge, too faint, bad centroid.
4. **Rank** the survivors by expected positional error, **select** them (inside the low-coma
   radius, widened until `n_target`; a grid quota; capped at `n_max`), and decide whether the frame
   can be solved (the fit is order 1).

All coordinates are 0-based array coordinates of the array given (`x` is the column), the pixel
*centre* at an integer. The caller crops a region of interest first and adds its origin back.

A fixed window matters because a first-moment centroid depends on the star's brightness once coma
is present, and a window does not (design, "Findings 1"). Those numbers are from simulation; the
survey is how they get checked on real frames.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits
from scipy.spatial import cKDTree

from calibration.analysis.optical_center import extract_sources
from common.mast_logging import get_logger

logger = get_logger(__name__)

#: In the order a reason is reported when a source fails more than one test. `faint` is first so that
#: the many noise blobs a 2 sigma detection finds are called noise, not blends or edge sources; every
#: failed test is still recorded in `Selection.all_reasons`.
REJECT_REASONS = ("faint", "shadow", "saturated", "hot_pixel", "blended", "truncated", "bad_centroid")
#: A survivor the selection did not take: beyond the radius, over a cell's quota, or past `n_max`.
SPARE = "spare"
KEPT = "kept"

FWHM_PER_SIGMA = 2.3548200450309493
#: Stamp half-size for the first, seeing-free pass that measures peak and half-maximum width.
PRELIMINARY_HALF_SIZE = 15
#: The `--tweak-order` handed to the solver. Always 1: only the position at CRPIX matters, and
#: production hands the solver the fibre pixel as CRPIX; the distortion toward the corners does not
#: (decided 2026-11-18).
FIT_ORDER = 1
#: Sources brighter than this (preliminary SNR) are the seeing sample.
SEEING_SAMPLE_MIN_SNR = 30.0


@dataclass(frozen=True)
class SelectionParams:
    """The design's starting values; they are to be tuned on real frames (Test plan, Step 1)."""

    # detection, as `extract_sources`
    nsigma: float = 2.0
    npixels: int = 5
    box_size: int = 50
    #: detect and measure against the *local* noise, not one figure for the frame (see `extract_sources`)
    local_noise: bool = True
    # seeing: measured on the frame unless given; the default is 2 arcsec at 0.2616 arcsec/px
    seeing_fwhm_px: float | None = None
    default_fwhm_px: float = 7.6
    # hard rejects
    max_shadow_deficit: float = 0.05
    sat_frac: float = 0.8
    #: The saturation level. None: from the FITS header (SATURATE, MAXADU, DATAMAX, else what BITPIX,
    #: BSCALE and BZERO allow), else the dtype's maximum for an integer frame, else not tested.
    full_scale: float | None = None
    min_area: int = 5
    #: the window centroid's convergence: a step below `centroid_tolerance` px, within
    #: `centroid_max_iterations` passes. A comatic star (a head with a tail) iterates slowly, and the
    #: strict defaults reject it as a bad centroid.
    centroid_tolerance: float = 2e-4
    centroid_max_iterations: int = 16
    sharp_fwhm_frac: float = 0.5
    min_sep_fwhm: float = 3.0
    #: a source this much wider than the seeing is a merged pair (or not a star); tested only where
    #: the width is reliable (SNR from SEEING_SAMPLE_MIN_SNR)
    max_fwhm_ratio: float = 1.35
    blend_flux_ratio: float = 0.10
    #: a neighbour counts toward a blend only if it is itself a source (a noise blob cannot bias a star)
    neighbour_min_snr: float = 8.0
    #: a source rejected as a hot pixel is not a neighbour: it is no star, and one 25 px away does not
    #: bias a star's window. On a raw frame with ~1000 of them it made most stars "blended".
    blend_ignores_hot: bool = True
    edge_margin_sigmas: float = 3.0
    edge_margin_extra_px: float = 5.0
    min_snr: float = 15.0
    relaxed_min_snr: float = 10.0  # used instead, when fewer than n_target would survive
    max_centroid_shift_fwhm: float = 0.5
    # selection
    n_target: int = 30
    n_max: int = 150
    coma_px: float = 2.0
    radius_growth: float = 1.25
    grid: tuple[int, int] = (4, 3)  # cells across x, across y
    # fewer kept than this and the frame is not solved; the fit is always `FIT_ORDER`
    fail_below: int = 8
    #: a fraction of the shorter side, the low-coma radius when none is calibrated
    geometric_low_coma_fraction: float = 0.25


@dataclass
class Selection:
    """Every detected source, its measurements and its fate, plus what the frame as a whole gave."""

    shape: tuple[int, int]
    params: SelectionParams
    fwhm_seeing_px: float = float("nan")
    sigma_window_px: float = float("nan")
    noise_px: float = float("nan")
    optical_center: tuple[float, float] = (float("nan"), float("nan"))
    low_coma_radius_px: float = float("nan")
    radius_used_px: float = float("nan")
    full_scale: float | None = None
    #: where `full_scale` came from: "params", a header keyword, "BITPIX", "dtype", or None (not tested)
    full_scale_source: str | None = None
    saturation_tested: bool = False
    snr_relaxed: bool = False
    shadow_present: bool = False
    exclusion_half_width_px: float | None = None
    fit_order: int | None = None
    status: str = "ok"  # "ok", "too_few_sources", "no_sources"
    # one entry per detected source, in `extract_sources` order
    x: np.ndarray = field(default_factory=lambda: np.empty(0))
    y: np.ndarray = field(default_factory=lambda: np.empty(0))
    x_first_moment: np.ndarray = field(default_factory=lambda: np.empty(0))
    y_first_moment: np.ndarray = field(default_factory=lambda: np.empty(0))
    flux: np.ndarray = field(default_factory=lambda: np.empty(0))
    snr: np.ndarray = field(default_factory=lambda: np.empty(0))
    peak: np.ndarray = field(default_factory=lambda: np.empty(0))
    fwhm: np.ndarray = field(default_factory=lambda: np.empty(0))
    area: np.ndarray = field(default_factory=lambda: np.empty(0))
    radius: np.ndarray = field(default_factory=lambda: np.empty(0))
    shadow_deficit: np.ndarray = field(default_factory=lambda: np.empty(0))
    shadow_distance: np.ndarray = field(default_factory=lambda: np.empty(0))
    sigma: np.ndarray = field(default_factory=lambda: np.empty(0))
    fate: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=object))  # KEPT, SPARE, or a reject reason
    all_reasons: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=object))  # every failed test, "|"-joined

    @property
    def n_detected(self) -> int:
        return len(self.x)

    @property
    def kept(self) -> np.ndarray:
        return self.fate == KEPT

    def counts(self) -> dict[str, int]:
        """Detected, kept, spare, and each reject reason (by first reason)."""
        out = {"detected": self.n_detected, "kept": int(np.sum(self.fate == KEPT)), "spare": int(np.sum(self.fate == SPARE))}
        for reason in REJECT_REASONS:
            out[reason] = int(np.sum(self.fate == reason))
        return out

    def xylist(self) -> np.ndarray:
        """The kept sources as an (n, 3) array of x, y, flux, brightest first -- what the solver gets."""
        index = np.flatnonzero(self.kept)
        order = index[np.argsort(-self.flux[index], kind="stable")]
        return np.column_stack([self.x[order], self.y[order], self.flux[order]])


def _load(image) -> np.ndarray:
    if isinstance(image, np.ndarray):
        return image
    return np.asarray(fits.getdata(Path(image)))


def _noise_at_sources(detected: dict, params: SelectionParams, frame_noise: float) -> np.ndarray:
    """The noise to measure each source against: the local background RMS at its peak, or the frame's figure."""
    n = detected["n_detected"]
    if not params.local_noise:
        return np.full(n, frame_noise)
    rms = detected["background_rms"]
    return np.maximum(rms[np.asarray(detected["peak_y"], dtype=int), np.asarray(detected["peak_x"], dtype=int)], 1e-9)


def window_centroid(
    data: np.ndarray,
    x: float,
    y: float,
    sigma_w: float,
    *,
    radius_sigmas: float = 4.0,
    max_iterations: int = 16,
    tolerance: float = 2e-4,
) -> tuple[float, float, float, float, bool]:
    """A Gaussian-windowed centroid of the star near (`x`, `y`): SExtractor's `winpos`.

    Each pass moves the centre by `2 * sum(w f dx) / sum(w f)` with `w` a Gaussian of `sigma_w`
    about the present centre. The window does not grow with the star, so a comatic or bright star
    is not measured differently from a faint one (a first moment is).

    Returns `(x, y, mass, sum_w2, converged)`: `mass` is `sum(w f)` and `sum_w2` is `sum(w^2)` at
    the final centre, which give the matched-filter SNR `mass / (noise * sqrt(sum_w2))`. `data`
    must be background-subtracted.
    """
    ny, nx = data.shape
    half = int(math.ceil(radius_sigmas * sigma_w)) + 2
    cx0, cy0 = int(round(x)), int(round(y))
    x_lo, x_hi = max(0, cx0 - half), min(nx, cx0 + half + 1)
    y_lo, y_hi = max(0, cy0 - half), min(ny, cy0 + half + 1)
    if x_hi - x_lo < 3 or y_hi - y_lo < 3:
        return float("nan"), float("nan"), 0.0, 0.0, False
    stamp = data[y_lo:y_hi, x_lo:x_hi]
    xs = np.arange(x_lo, x_hi, dtype=float)[None, :]
    ys = np.arange(y_lo, y_hi, dtype=float)[:, None]
    xc, yc = float(x), float(y)
    limit2 = (radius_sigmas * sigma_w) ** 2
    mass = sum_w2 = 0.0
    converged = False
    for _ in range(max_iterations):
        dx, dy = xs - xc, ys - yc
        r2 = dx * dx + dy * dy
        w = np.where(r2 <= limit2, np.exp(-r2 / (2.0 * sigma_w**2)), 0.0)
        mass = float((w * stamp).sum())
        sum_w2 = float((w * w).sum())
        if mass <= 0.0:
            return float("nan"), float("nan"), mass, sum_w2, False
        step_x = 2.0 * float((w * stamp * dx).sum()) / mass
        step_y = 2.0 * float((w * stamp * dy).sum()) / mass
        xc, yc = xc + step_x, yc + step_y
        if abs(step_x) < tolerance and abs(step_y) < tolerance:
            converged = True
            break
        if not (x_lo - 1 <= xc <= x_hi and y_lo - 1 <= yc <= y_hi):
            return float("nan"), float("nan"), mass, sum_w2, False  # walked off the stamp
    return xc, yc, mass, sum_w2, converged


def half_maximum_fwhm(stamp: np.ndarray, peak: float, own: np.ndarray | None = None) -> float:
    """FWHM of the star in `stamp` from the count of pixels at or above half its peak: 2 sqrt(n / pi).

    `own` is a boolean mask of the star's own segment. Given, only those pixels are counted, so a
    neighbour in the stamp does not widen the star.
    """
    if peak <= 0:
        return 0.0
    above = stamp >= 0.5 * peak
    if own is not None:
        above &= own
    return 2.0 * math.sqrt(float(np.count_nonzero(above)) / math.pi)


#: Header keywords that state the saturation level directly, in the order they are tried.
FULL_SCALE_KEYWORDS = ("SATURATE", "MAXADU", "DATAMAX")


def full_scale_from_header(header) -> tuple[float | None, str | None]:
    """The saturation level a FITS header gives, and which part of it: `(None, None)` if none.

    A keyword (`FULL_SCALE_KEYWORDS`) wins. Otherwise, for a frame stored as integers, the largest
    value those can hold once BSCALE and BZERO are applied (BITPIX 16, BZERO 32768 gives 65535). That
    is right whatever dtype the data comes back as, which is the point for a frame that was scaled or
    binned into floats. A frame stored as floats (BITPIX < 0) says nothing: its header gives no level.
    """
    if header is None:
        return None, None
    for key in FULL_SCALE_KEYWORDS:
        try:
            value = float(header[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value, key
    bitpix = header.get("BITPIX")
    if bitpix in (8, 16, 32, 64):
        stored_max = 255 if bitpix == 8 else 2 ** (bitpix - 1) - 1  # 8 is unsigned in FITS, the others signed
        try:
            value = float(header.get("BSCALE", 1.0)) * stored_max + float(header.get("BZERO", 0.0))
        except (TypeError, ValueError):
            return None, None
        if math.isfinite(value) and value > 0:
            return value, "BITPIX"
    return None, None


def _shadow_deficit(shadow, x: np.ndarray, y: np.ndarray, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Per source: the fraction of light the fold mirror blocks there, and the distance across the band.

    The distance is `s - offset`, with `s` the perpendicular coordinate that `ShadowModel` uses
    (from the image centre, along the band normal). The deficit is the model's measured
    cross-section where there is one, else a trapezoid from its umbra and penumbra widths.
    """
    n = len(x)
    if shadow is None or not shadow.present:
        return np.zeros(n), np.full(n, np.inf)
    ny, nx = shape
    cx, cy = (nx - 1) / 2.0, (ny - 1) / 2.0
    s = -(x - cx) * math.sin(shadow.angle) + (y - cy) * math.cos(shadow.angle)
    distance = s - shadow.offset
    if shadow.s is not None and shadow.profile is not None and len(shadow.s) > 1:
        deficit = np.interp(s, shadow.s, shadow.profile, left=0.0, right=0.0)
    else:
        d = np.abs(distance)
        span = max(shadow.penumbra_half_width - shadow.umbra_half_width, 1e-9)
        deficit = shadow.depth * np.clip((shadow.penumbra_half_width - d) / span, 0.0, 1.0)
    return np.clip(deficit, 0.0, 1.0), distance


def _resolve_full_scale(params: SelectionParams, header, raw: np.ndarray) -> tuple[float | None, str | None]:
    """The saturation level and where it came from: the parameter, then the header, then an integer dtype."""
    if params.full_scale is not None:
        return params.full_scale, "params"
    from_header = full_scale_from_header(header)
    if from_header[0] is not None:
        return from_header
    if np.issubdtype(raw.dtype, np.integer):
        return float(np.iinfo(raw.dtype).max), "dtype"
    return None, None


def select_sources(
    image,
    *,
    shadow=None,
    optical_center: tuple[float, float] | None = None,
    low_coma_radius: float | None = None,
    params: SelectionParams | None = None,
    exclusion_half_width: float | None = None,
    header=None,
) -> Selection:
    """Detect, measure, reject and select the sources of `image` (a 2D array or a FITS path).

    `shadow` is a `ShadowModel` for a frame with the fold mirror in, `None` for one with it out.
    `optical_center` and `low_coma_radius` come from the unit's calibration; unset, the centre is
    the frame's and the radius a fraction of its shorter side. `exclusion_half_width`, given,
    replaces the shadow *deficit* test with a plain rule -- a source within that distance of the
    band's centreline is rejected -- which is how the survey sweeps the width of the exclusion.

    Deterministic: the same frame and arguments give the same `Selection`.
    """
    params = params or SelectionParams()
    raw = _load(image)
    ny, nx = raw.shape
    data = np.asarray(raw, dtype=float)
    result = Selection(shape=(ny, nx), params=params, shadow_present=bool(shadow is not None and shadow.present))
    result.exclusion_half_width_px = exclusion_half_width

    center = optical_center if optical_center is not None else ((nx - 1) / 2.0, (ny - 1) / 2.0)
    result.optical_center = (float(center[0]), float(center[1]))
    result.low_coma_radius_px = (
        float(low_coma_radius) if low_coma_radius else float(params.geometric_low_coma_fraction * min(nx, ny))
    )

    if header is None and not isinstance(image, np.ndarray):
        header = fits.getheader(Path(image))
    full_scale, result.full_scale_source = _resolve_full_scale(params, header, raw)
    result.full_scale = full_scale
    result.saturation_tested = full_scale is not None

    detected = extract_sources(
        data, nsigma=params.nsigma, npixels=params.npixels, box_size=params.box_size, local_noise=params.local_noise
    )
    if detected is None or detected["n_detected"] == 0:
        result.status = "no_sources"
        return result
    sub = detected["data_sub"]
    n = detected["n_detected"]

    # the frame's noise figure (reported), and the noise at each source (used): the local background RMS
    # at its peak, or the frame's figure when not measuring locally
    noise = 1.4826 * float(np.median(np.abs(sub - np.median(sub))))
    result.noise_px = noise = max(noise, 1e-9)
    noise_at = _noise_at_sources(detected, params, noise)

    # -- preliminary pass: raw peak and half-maximum FWHM, to find the seeing ------------------------
    peak_x = np.asarray(detected["peak_x"], dtype=int)
    peak_y = np.asarray(detected["peak_y"], dtype=int)
    raw_peak = data[peak_y, peak_x]
    prelim_fwhm = np.zeros(n)
    prelim_snr = np.zeros(n)
    half = PRELIMINARY_HALF_SIZE
    segment_map, labels = detected["segment_map"], detected["labels"]
    for i in range(n):
        rows = slice(max(0, peak_y[i] - half), peak_y[i] + half + 1)
        columns = slice(max(0, peak_x[i] - half), peak_x[i] + half + 1)
        peak = float(sub[peak_y[i], peak_x[i]])
        # only the source's own segment: a neighbour in the stamp must not widen it
        prelim_fwhm[i] = half_maximum_fwhm(sub[rows, columns], peak, segment_map[rows, columns] == labels[i])
        prelim_snr[i] = detected["flux"][i] / (noise_at[i] * math.sqrt(max(detected["area"][i], 1.0)))

    r_first = np.hypot(detected["x"] - center[0], detected["y"] - center[1])
    if params.seeing_fwhm_px is not None:
        fwhm_seeing = float(params.seeing_fwhm_px)
    else:
        unsaturated = np.ones(n, bool) if full_scale is None else raw_peak < params.sat_frac * full_scale
        sample = (prelim_snr >= SEEING_SAMPLE_MIN_SNR) & unsaturated & (prelim_fwhm > 1.0)
        inside = sample & (r_first <= result.low_coma_radius_px)
        pool = inside if np.count_nonzero(inside) >= 5 else sample
        fwhm_seeing = float(np.median(prelim_fwhm[pool])) if np.count_nonzero(pool) >= 5 else params.default_fwhm_px
    sigma_w = fwhm_seeing / FWHM_PER_SIGMA
    result.fwhm_seeing_px = fwhm_seeing
    result.sigma_window_px = sigma_w

    # -- the fixed-window measurement -----------------------------------------------------------
    xw = np.full(n, np.nan)
    yw = np.full(n, np.nan)
    snr = np.zeros(n)
    flux_w = np.zeros(n)
    converged = np.zeros(n, bool)
    for i in range(n):
        xc, yc, mass, sum_w2, ok = window_centroid(
            sub,
            detected["x"][i],
            detected["y"][i],
            sigma_w,
            max_iterations=params.centroid_max_iterations,
            tolerance=params.centroid_tolerance,
        )
        xw[i], yw[i], converged[i], flux_w[i] = xc, yc, ok, mass
        if sum_w2 > 0:
            snr[i] = mass / (noise_at[i] * math.sqrt(sum_w2))

    x_use = np.where(np.isfinite(xw), xw, detected["x"])
    y_use = np.where(np.isfinite(yw), yw, detected["y"])
    radius = np.hypot(x_use - center[0], y_use - center[1])

    # -- the shadow -------------------------------------------------------------------------------
    deficit, distance = _shadow_deficit(shadow, x_use, y_use, (ny, nx))
    if exclusion_half_width is not None and result.shadow_present:
        shadow_fail = np.abs(distance) <= exclusion_half_width
    else:
        shadow_fail = deficit > params.max_shadow_deficit

    # -- the hard rejects ---------------------------------------------------------------------------
    saturated = raw_peak >= params.sat_frac * full_scale if full_scale is not None else np.zeros(n, dtype=bool)
    hot = (prelim_fwhm < params.sharp_fwhm_frac * fwhm_seeing) | (np.asarray(detected["area"]) < params.min_area)

    min_sep = params.min_sep_fwhm * fwhm_seeing
    # two stars close enough that the detector merged them are one broad source, with no neighbour to find
    blended = (snr >= SEEING_SAMPLE_MIN_SNR) & (prelim_fwhm > params.max_fwhm_ratio * fwhm_seeing)
    if n > 1:
        tree = cKDTree(np.column_stack([x_use, y_use]))
        flux = np.asarray(detected["flux"], dtype=float)
        # a source counts as a neighbour if it is itself a source: bright enough, and (if asked) no hot pixel
        neighbour = (snr >= params.neighbour_min_snr) & ~(hot if params.blend_ignores_hot else False)
        for i, j in sorted(tree.query_pairs(min_sep)):
            if neighbour[j] and flux[j] > params.blend_flux_ratio * flux[i]:
                blended[i] = True
            if neighbour[i] and flux[i] > params.blend_flux_ratio * flux[j]:
                blended[j] = True

    margin = params.edge_margin_sigmas * sigma_w + params.edge_margin_extra_px
    truncated = (x_use < margin) | (x_use > nx - 1 - margin) | (y_use < margin) | (y_use > ny - 1 - margin)

    def reasons_with(min_snr: float) -> dict[str, np.ndarray]:
        shift = np.hypot(x_use - detected["x"], y_use - detected["y"])
        # A shift between the window centroid and the first moment is what coma itself makes off
        # axis, so it is a fault only inside the low-coma radius. Outside, a star that did not
        # converge is still bad, and the coma term of `sigma` ranks the rest down.
        shifted = (shift > params.max_centroid_shift_fwhm * fwhm_seeing) & (radius <= result.low_coma_radius_px)
        bad_centroid = ~converged | ~np.isfinite(xw) | shifted
        return {
            "shadow": shadow_fail,
            "saturated": saturated,
            "hot_pixel": hot,
            "blended": blended,
            "truncated": truncated,
            "faint": snr < min_snr,
            "bad_centroid": bad_centroid,
        }

    failed = reasons_with(params.min_snr)
    survivors = ~np.any([failed[r] for r in REJECT_REASONS], axis=0)
    if np.count_nonzero(survivors) < params.n_target:
        relaxed = reasons_with(params.relaxed_min_snr)
        relaxed_survivors = ~np.any([relaxed[r] for r in REJECT_REASONS], axis=0)
        if np.count_nonzero(relaxed_survivors) > np.count_nonzero(survivors):
            failed, survivors = relaxed, relaxed_survivors
            result.snr_relaxed = True

    fate = np.empty(n, dtype=object)
    all_reasons = np.empty(n, dtype=object)
    for i in range(n):
        names = [r for r in REJECT_REASONS if failed[r][i]]
        all_reasons[i] = "|".join(names)
        fate[i] = names[0] if names else SPARE

    # -- rank the survivors by expected positional error, and select ---------------------------------
    sigma_psf = fwhm_seeing / FWHM_PER_SIGMA
    sigma_noise = 1.1 * sigma_psf / np.maximum(snr, 1e-9)
    r_lc = result.low_coma_radius_px
    sigma_coma = np.where(radius <= r_lc, 0.0, (radius - r_lc) ** 2 / r_lc**2 * params.coma_px)
    sigma = np.hypot(sigma_noise, sigma_coma)

    candidates = np.flatnonzero(survivors)
    chosen = _choose(candidates, radius, sigma, x_use, y_use, (ny, nx), r_lc, params, result)
    fate[chosen] = KEPT

    result.x, result.y = x_use, y_use
    result.x_first_moment = np.asarray(detected["x"], dtype=float)
    result.y_first_moment = np.asarray(detected["y"], dtype=float)
    result.flux, result.snr, result.peak = flux_w, snr, raw_peak
    result.fwhm, result.area = prelim_fwhm, np.asarray(detected["area"], dtype=float)
    result.radius, result.shadow_deficit, result.shadow_distance = radius, deficit, distance
    result.sigma, result.fate, result.all_reasons = sigma, fate, all_reasons

    n_kept = len(chosen)
    if n_kept < params.fail_below:
        result.status = "too_few_sources"
    result.fit_order = None if n_kept < params.fail_below else FIT_ORDER
    return result


def _choose(
    candidates: np.ndarray,
    radius: np.ndarray,
    sigma: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    shape: tuple[int, int],
    low_coma_radius: float,
    params: SelectionParams,
    result: Selection,
) -> np.ndarray:
    """From the surviving sources: inside the low-coma radius, widened until `n_target` are *chosen*,
    with a grid quota, lowest expected error first, at most `n_max`. Returns the indices chosen.

    `n_target` counts what the quota leaves, not what lies inside the radius: stars crowded into one
    cell do not count for more than the cell's share.
    """
    if candidates.size == 0:
        result.radius_used_px = low_coma_radius
        return candidates
    ny, nx = shape
    cells_x, cells_y = params.grid
    per_cell = max(1, int(math.ceil(params.n_max / (cells_x * cells_y))))

    def choose_within(reach: float) -> np.ndarray:
        inside = candidates[radius[candidates] <= reach]
        cell = np.minimum((x[inside] / nx * cells_x).astype(int), cells_x - 1) + cells_x * np.minimum(
            (y[inside] / ny * cells_y).astype(int), cells_y - 1
        )
        taken: list[int] = []
        for c in np.unique(cell):
            members = inside[cell == c]
            taken.extend(members[np.argsort(sigma[members], kind="stable")][:per_cell].tolist())
        chosen = np.array(taken, dtype=int)
        if chosen.size > params.n_max:
            chosen = chosen[np.argsort(sigma[chosen], kind="stable")][: params.n_max]
        return np.sort(chosen)

    max_radius = float(np.max(radius[candidates]))
    reach = low_coma_radius
    chosen = choose_within(reach)
    while chosen.size < params.n_target and reach < max_radius:
        reach *= params.radius_growth
        chosen = choose_within(reach)
    result.radius_used_px = reach
    return chosen
