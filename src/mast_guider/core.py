"""Choose a fixed set of guide stars from a reference frame and measure how far later frames
have drifted from it, as one translation at the fibre plus a health signal.

Clean-room from the in-house guiding design (`plans/inhouse_guiding_design.md`, §3 and §11), on
top of the unit's own source selection (`solvers/source_selection.py`), validated offline on
operational frames (~30-60 clean guide stars around the fibre, a stable sub-pixel core):

- **The guide stars are chosen once, from the reference frame** (`select_sources` -- the same hard
  rejects, shadow exclusion and low-coma preference), and tracked. They are **not** re-selected per
  frame: seeing moves stars across the SNR cut, so a re-selected set makes the estimate jump, while a
  fixed set is weighted smoothly (§11.1).
- **Each later frame is measured against that fixed set** with the selection's *own* estimator, the
  windowed centroid (`window_centroid`, SExtractor winpos), seeded at each star's predicted position.
  The same estimator on the reference and on every frame makes the measurement differential, so a
  star's coma / centroid bias -- the same in both -- cancels (§11.2).
- **The per-star displacements are combined into one translation** by a robust, SNR^2-weighted,
  sigma-clipped mean (§11.2, §11.8 Q2). On operational frames the guide stars cluster *around* the
  fibre within the low-coma radius, so the field rotation's lever arm to the fibre is small and a
  translation-only fit is well-justified (the §11.4 shadow-on-fibre pathology does not arise; see the
  fleet survey). Rotation remains a later refinement (§11.6, §11.8 Q3).
- **The spread of the per-star displacements is the health signal** (§11.2): a frame whose stars
  disagree beyond measurement noise (cloud, seeing event) is held, not corrected -- the caller gates
  on `Measurement.scatter_px`.

Pure: ndarray in, measurement out. No mount, no config, no disk, so it is unit-testable on synthetic
frames. Turning the pixel translation into a mount move (pixel -> sky via the plate solution's WCS at
the fibre, the correction policy, `mount_offset`) is a separate layer; this is the measurement half.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from solvers.source_selection import KEPT, SelectionParams, select_sources, window_centroid

#: Robust-scatter scale: sigma = this * median-absolute-deviation (for a normal distribution).
MAD_TO_SIGMA = 1.4826
#: Half-size, pixels, of the search box around a star's predicted position: a centroid that lands
#: outside it moved further than expected (a lost or mis-identified star) and is dropped. 16 px is
#: ~4.2" at the unit scale -- generous for a closed-loop drift, enough to re-find after a hold.
DEFAULT_SEARCH_BOX_PX = 16.0
#: Per-star offsets more than this many robust sigma from the median are set aside before the mean.
DEFAULT_CLIP_SIGMAS = 3.0
#: A floor on the clip scale: stars nearer the median than this are never set aside, however tightly
#: the rest agree, so a momentarily quiet field does not reject good stars on rounding-level spread.
MIN_SCATTER_PX = 0.05
#: Matched-filter SNR below which a star is taken to be lost this frame (no light above the noise):
#: its window holds only sky, so its centroid is noise about the seed, not a measurement. The set is
#: chosen at a far higher SNR (`min_snr`), so this only catches real loss -- cloud, an occulted star.
DEFAULT_MIN_TRACK_SNR = 4.0


class NoMeasurementError(ValueError):
    """Too few guide stars were measured (or agreed) to give an offset, so no translation is returned
    rather than one resting on a single star or none -- a frame with no usable star can never read as
    'on target'."""


@dataclass(frozen=True)
class GuideStars:
    """The fixed guide-star set chosen from the reference frame.

    ``x``/``y`` are the reference centroids in the reference frame's array coordinates, measured by the
    same windowed centroid used on every later frame. ``snr`` is each star's SNR at the reference; the
    combine weights are ``snr**2`` (a star's position error falls as 1/SNR). ``sigma_window`` is the
    winpos Gaussian sigma (the seeing), one figure for the set.
    """

    x: np.ndarray
    y: np.ndarray
    snr: np.ndarray
    sigma_window: float
    shape: tuple[int, int]  # (ny, nx) of the reference frame
    optical_center: tuple[float, float]

    @property
    def n(self) -> int:
        return int(len(self.x))


@dataclass(frozen=True)
class Measurement:
    """How far a frame has drifted from the reference.

    ``dx``/``dy`` is the field's translation in pixels (where the stars are now, less where they were),
    ``error_px`` its per-axis uncertainty from the spread of the stars (NaN with one star), and
    ``scatter_px`` the robust scatter of the per-star offsets -- the health signal the caller gates on.
    ``offsets`` holds every star's own offset, in the reference's star order, NaN for a lost star, and
    ``used`` says which went into the mean.
    """

    dx: float
    dy: float
    error_px: tuple[float, float]
    scatter_px: float
    offsets: np.ndarray  # (n, 2)
    used: np.ndarray  # (n,) bool

    @property
    def n_used(self) -> int:
        return int(self.used.sum())

    @property
    def n_lost(self) -> int:
        """Stars that gave no offset this frame: no centroid, or one outside the search box."""
        return int((~np.isfinite(self.offsets).all(axis=1)).sum())

    @property
    def magnitude_px(self) -> float:
        return math.hypot(self.dx, self.dy)


def _as_float_array(image) -> np.ndarray:
    """A C-contiguous native-endian float32 copy of ``image`` (an array or a FITS path), as ``sep``
    wants it -- FITS frames are often big-endian, which the C extension rejects."""
    if isinstance(image, np.ndarray):
        data = image
    else:  # a path
        from astropy.io import fits  # noqa: PLC0415

        data = fits.getdata(image)
    return np.ascontiguousarray(np.asarray(data, dtype=np.float32))


def _background_subtracted(data: np.ndarray, params: SelectionParams) -> tuple[np.ndarray, float]:
    """``data`` with its sky removed and the background RMS, the same estimate the selection's detector
    uses (`solvers/sep_detect.extract_sources_sep`): a ``sep`` mesh background, so a sloping sky (a
    glow, the shadow's edge) is taken out, not just a level. The window centroid needs this -- it
    weights the light by a Gaussian, and a background left in biases the centre toward the brighter
    side; the RMS gives each star's matched-filter SNR, which says whether a star is there at all."""
    import sep  # noqa: PLC0415

    ny, nx = data.shape
    bkg = sep.Background(data, bw=min(params.box_size, nx), bh=min(params.box_size, ny), fw=3, fh=3)
    return data - bkg.back(), float(bkg.globalrms)


def _coarse_peak(sub: np.ndarray, px: float, py: float, half: float) -> tuple[float, float]:
    """The brightest point of the background-subtracted stamp of half-size ``half`` about the predicted
    position -- a coarse seed for the windowed centroid, which is only a *local* refiner (its Gaussian
    window reaches ~4 sigma, so it cannot by itself re-find a star that drifted further). Lightly
    smoothed first so a hot pixel does not win. Kept below the guide stars' min-separation by the caller
    (``search_box_px``), so the only source in the stamp is the star itself -- the correspondence stays
    unambiguous. Returns the predicted point unchanged when the stamp is too small to search."""
    from scipy.ndimage import gaussian_filter  # noqa: PLC0415

    ny, nx = sub.shape
    h = int(math.ceil(half))
    x0, x1 = max(0, int(round(px)) - h), min(nx, int(round(px)) + h + 1)
    y0, y1 = max(0, int(round(py)) - h), min(ny, int(round(py)) + h + 1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return px, py
    smoothed = gaussian_filter(sub[y0:y1, x0:x1], 1.0)
    iy, ix = np.unravel_index(int(np.argmax(smoothed)), smoothed.shape)
    return float(x0 + ix), float(y0 + iy)


def _combine(offsets: np.ndarray, weights: np.ndarray, clip_sigmas: float, min_stars: int) -> Measurement:
    """One translation from the per-star offsets: a robust median sets the centre, stars more than
    ``clip_sigmas`` robust sigma from it are set aside, and the rest are averaged with ``snr**2``
    weights; the kept stars' own spread gives the uncertainty and the scatter (§11.2, §11.8 Q2)."""
    found = np.isfinite(offsets).all(axis=1)
    if int(found.sum()) < max(min_stars, 1):
        raise NoMeasurementError(f"{int(found.sum())} of {len(offsets)} guide stars measured, need {min_stars}")
    median = np.median(offsets[found], axis=0)
    scatter = MAD_TO_SIGMA * np.median(np.abs(offsets[found] - median), axis=0)
    used = np.zeros(len(offsets), dtype=bool)
    used[found] = (np.abs(offsets[found] - median) <= clip_sigmas * np.maximum(scatter, MIN_SCATTER_PX)).all(axis=1)
    n_used = int(used.sum())
    if n_used < max(min_stars, 1):
        raise NoMeasurementError(f"{n_used} of {len(offsets)} guide stars agree, need {min_stars}")
    w = weights[used] / weights[used].sum()
    mean = (w[:, None] * offsets[used]).sum(axis=0)
    # The weights are each star's inverse variance up to one unknown scale, which the stars' own
    # scatter about the mean supplies. One star has no scatter: unknown, returned NaN, not zero.
    if n_used > 1:
        error = np.sqrt((w[:, None] * (offsets[used] - mean) ** 2).sum(axis=0) / (n_used - 1))
    else:
        error = np.full(2, math.nan)
    return Measurement(
        dx=float(mean[0]),
        dy=float(mean[1]),
        error_px=(float(error[0]), float(error[1])),
        scatter_px=float(math.hypot(scatter[0], scatter[1])),
        offsets=offsets,
        used=used,
    )


class MastGuiderCore:
    """Choose a fixed guide-star set from a reference frame, then measure each later frame's drift.

    Call :meth:`reference` once with the frame whose target sits on the fibre; then :meth:`measure`
    per frame. Both take an image array (or a FITS path). The guider keeps no mount, config or disk
    state -- only the chosen stars -- so it is pure and unit-testable.
    """

    def __init__(
        self,
        *,
        params: SelectionParams | None = None,
        search_box_px: float = DEFAULT_SEARCH_BOX_PX,
        clip_sigmas: float = DEFAULT_CLIP_SIGMAS,
        min_track_snr: float = DEFAULT_MIN_TRACK_SNR,
        min_stars: int = 1,
    ):
        self.params = params if params is not None else SelectionParams()
        self.search_box_px = float(search_box_px)
        self.clip_sigmas = float(clip_sigmas)
        self.min_track_snr = float(min_track_snr)
        self.min_stars = int(min_stars)
        self._reference: GuideStars | None = None

    @property
    def reference_stars(self) -> GuideStars | None:
        """The fixed guide-star set, or None until :meth:`reference` has run."""
        return self._reference

    def reference(
        self,
        image,
        *,
        shadow=None,
        optical_center: tuple[float, float] | None = None,
        low_coma_radius: float | None = None,
        header=None,
    ) -> GuideStars:
        """Choose the guide stars from ``image`` (the reference: target on the fibre).

        Runs the unit's source selection, keeps the stars it marked ``KEPT``, and re-measures each with
        the *same* windowed centroid used on every later frame -- so the reference centroids and the
        per-frame ones are produced alike and their difference is a clean displacement (§11.2). Raises
        :class:`NoMeasurementError` if the frame yields fewer than ``min_stars`` guide stars.
        """
        sel = select_sources(
            image,
            shadow=shadow,
            optical_center=optical_center,
            low_coma_radius=low_coma_radius,
            params=self.params,
            header=header,
        )
        keep = sel.fate == KEPT
        if int(np.count_nonzero(keep)) < self.min_stars:
            raise NoMeasurementError(f"reference selection kept {int(keep.sum())} stars, need >= {self.min_stars}")
        sub, _noise = _background_subtracted(_as_float_array(image), self.params)
        sigma_w = float(sel.sigma_window_px)
        xs, ys, snrs = [], [], []
        for xi, yi, si in zip(sel.x[keep], sel.y[keep], sel.snr[keep], strict=True):
            xc, yc, _mass, _sw2, ok = window_centroid(sub, float(xi), float(yi), sigma_w)
            if ok and np.isfinite(xc) and np.isfinite(yc):
                xs.append(xc)
                ys.append(yc)
                snrs.append(float(si))
        if len(xs) < self.min_stars:
            raise NoMeasurementError(f"reference re-centroiding kept {len(xs)} stars, need >= {self.min_stars}")
        self._reference = GuideStars(
            x=np.asarray(xs, dtype=float),
            y=np.asarray(ys, dtype=float),
            snr=np.asarray(snrs, dtype=float),
            sigma_window=sigma_w,
            shape=tuple(sel.shape),
            optical_center=tuple(sel.optical_center),
        )
        return self._reference

    def measure(self, image, *, predicted_offset: tuple[float, float] = (0.0, 0.0)) -> Measurement:
        """The drift of ``image`` from the reference, as one translation ``(dx, dy)`` plus the per-star
        scatter. ``predicted_offset`` seeds each star's search -- the loop's running estimate of where
        the field now sits -- so the search box can stay small even after the field has moved. Raises
        :class:`NoMeasurementError` if too few stars are measured or agree.
        """
        ref = self._reference
        if ref is None:
            raise RuntimeError("reference() must be called before measure()")
        sub, noise = _background_subtracted(_as_float_array(image), self.params)
        ox, oy = float(predicted_offset[0]), float(predicted_offset[1])
        half = self.search_box_px
        offsets = np.full((ref.n, 2), math.nan)
        for i in range(ref.n):
            px, py = ref.x[i] + ox, ref.y[i] + oy  # predicted position this frame
            sx, sy = _coarse_peak(sub, px, py, half)  # brightest point in the search box: the star
            xc, yc, mass, sum_w2, ok = window_centroid(sub, sx, sy, ref.sigma_window)
            if not ok or not (np.isfinite(xc) and np.isfinite(yc)):
                continue  # walked off the window
            snr = mass / (noise * math.sqrt(sum_w2)) if (noise > 0.0 and sum_w2 > 0.0) else 0.0
            if snr < self.min_track_snr:
                continue  # no light above the noise: the star is lost, not at (xc, yc) -- that is sky
            if abs(xc - px) > half or abs(yc - py) > half:
                continue  # the refined centre is outside the search box: a different source, not ours
            offsets[i] = (xc - ref.x[i], yc - ref.y[i])
        return _combine(offsets, ref.snr**2, self.clip_sigmas, self.min_stars)
