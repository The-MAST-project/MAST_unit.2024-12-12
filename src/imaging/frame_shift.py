"""Measure the pixel shift between two frames of the same field.

Used by the spiral search: the operator drives the mount until the star sits on the
optical axis (judged on the independent Ximea camera), and the shift between the
reference frame and the final one says, in pixels, where the optical axis lies relative
to where the field started.

Correlating the whole field rather than tracking one star means nothing has to be
detected, centroided or matched -- which is what makes this robust on a field the
operator has not vetted.

Three preparation steps matter, and each is there for a reason:

* **Crop to the usable field around the fibre.** The window is centred on the fibre
  position from ``guiding.rois[fcu_v2]``, not on the geometric centre of the sensor,
  and its size is ``usable_fraction`` of each axis. Two reasons: the optics have
  pronounced coma, so PSFs in the outer field are elongated and position-dependent and
  smear the correlation peak; and the edges are the most vignetted.
* **Subtract the background.** ``_bg_subtract`` models the sky gradient and vignetting
  on a coarse grid and removes them.
* **Flatten isolated spikes.** The mount moves between the two frames but the DETECTOR
  DOES NOT, so hot pixels, dust and amp glow sit at identical detector coordinates in
  both and correlate perfectly at zero shift. A 3x3 median replaces isolated spikes with
  their neighbourhood while barely touching stars, which at 0.26"/px and ~2" seeing are
  ~8 px across.

The same fixed pattern is why the correlation is **plain, not phase-normalised**
(``normalization=None``). Phase normalisation rescales every spatial frequency to unit
magnitude, so a one-pixel defect contributes as much as a bright star -- exactly
backwards here. On real MAST acquisition frames it lost outright: measured against a
star-matched reference over 19 pairs across six nights, phase correlation collapsed onto
the zero-lag fixed-pattern peak in 18 of 19 cases (median error 15.4 px), while plain
cross-correlation, which weights by actual signal power, collapsed in **none** (median
error 1.2 px). Anything above a 3x3 median -- larger medians, bandpass, apodization,
tighter windows, star-map correlation -- was tried and did not fix it; only the
normalisation did.

Sub-pixel accuracy is nominally 1/``upsample`` px, but seeing, SNR and tracking drift
over an operator-paced sequence dominate long before that. Two decimals is honest.

What predicts a bad answer, on the real-frame sample: **how many stars the field has**,
more than any property of the correlation itself. Of 18 pairs, those with fewer than ~30
matchable stars failed 5 times in 6, while those with 45 or more failed once in 13. If a
stronger guard is wanted than `confidence` (which only catches outright nonsense -- see
MIN_CONFIDENCE), counting sources in the reference frame is the measurement most likely
to provide it. Not done here: it costs a detection pass, and 18 pairs is too thin a basis
for a threshold that would refuse to answer.

Two things deliberately NOT done here:

* **Masking the folding-mirror shadow.** The pick-off stage's 45-deg mirror casts a wide,
  near-vertical band with graded edges when it is inserted (modelled on the ``calibration``
  branch, ``src/calibration/analysis/mirror_shadow.py``). It is fixed in the detector frame
  and so, like the hot pixels, contributes a zero-shift signal. It turns out not to need
  handling: it is smooth and wide, so ``_bg_subtract``'s 64-px background model absorbs it,
  and a simulated band is recovered exactly even at 60% depth. Masking would also be a bad
  trade -- ``phase_cross_correlation`` ignores ``upsample_factor`` when masks are supplied
  and falls back to whole-pixel shifts, costing the sub-pixel precision that is the point.
* **Plate-solving for field rotation.** The mount is equatorial and well polar-aligned, so
  over an operator-paced sequence rotation is negligible against seeing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.ndimage import median_filter
from scipy.ndimage import shift as ndi_shift
from skimage.registration import phase_cross_correlation

from common.mast_logging import get_logger

from .hfd import _bg_subtract

logger = get_logger(__name__)

#: Margins trimmed from each edge when nothing else specifies them. See the coma note above.
#: Chosen to keep about two thirds of each axis on the 8288x5644 sensor these units carry:
#: 8288 - 2*1400 = 5488 (66.2%) and 5644 - 2*950 = 3744 (66.3%). Stated as margins rather
#: than a fraction because that is what `guiding.rois[fcu_v2]` states, and because margins
#: stay meaningful on a sub-frame, where a fraction would silently mean a smaller area.
DEFAULT_MARGIN_HORIZONTAL = 1400
DEFAULT_MARGIN_VERTICAL = 950

#: Sub-pixel refinement passed to phase_cross_correlation.
DEFAULT_UPSAMPLE = 100

#: Below this post-registration correlation the answer is not worth acting on.
#:
#: Calibrated against star-matched truth on 18 real acquisition pairs, NOT from synthetic
#: frames -- a synthetic pair differs by a rigid shift and scores ~1.0, whereas real pairs
#: carry 3-10 px of differential motion across the field (the sky does not translate
#: rigidly between two solves), so no correct answer scores anywhere near 1.0. An earlier
#: value of 0.5, set from synthetic data, would have warned on EVERY real measurement.
#:
#: On that sample it separates cleanly: every correct answer scored >= 0.144, and all
#: three catastrophic failures (errors of 1500-1900 px) scored <= 0.036. It does NOT
#: catch moderate failures -- three pairs wrong by 9-12 px scored 0.05, 0.14 and 0.93 --
#: and nothing tested does, peak-to-runner-up ratio included (measured: good 2.68 vs bad
#: 2.65, indistinguishable). Treat this as a guard against nonsense, not a quality score.
MIN_CONFIDENCE = 0.10


@dataclass
class ShiftResult:
    """Outcome of one reference-vs-final comparison. All shifts are in pixels."""

    dx: float
    dy: float
    confidence: float
    """Pearson correlation between the reference and the registered final, over their
    overlap: ~1.0 when the frames genuinely match, ~0 when the answer is read off noise.

    Computed here rather than taken from ``phase_cross_correlation``, whose documented
    ``error`` return is a constant 1.0 under phase normalisation and discriminates
    nothing at all.

    KNOWN BLIND SPOT: it cannot detect fixed-pattern capture. If the correlation locks
    onto the detector pattern instead of the sky, the frames still agree well at that
    shift -- the pattern really is aligned -- and this scores ~0.94 while dx/dy are
    flatly wrong. It catches "these frames do not match", not "it matched the wrong
    thing". `at_origin` is the (weak) signal for the latter; plain cross-correlation is
    what actually prevents it."""
    margin_horizontal: int
    margin_vertical: int
    center_x: int
    center_y: int
    crop_shape: tuple[int, int]
    at_origin: bool
    """True if the peak landed on exactly (0, 0). Suspicious whenever the mount is
    known to have moved -- the classic signature of fixed-pattern noise winning."""

    def as_dict(self) -> dict:
        return asdict(self)


def margins_from_fraction(shape: tuple[int, int], usable_fraction: float) -> tuple[int, int]:
    """Margins equivalent to keeping `usable_fraction` of each axis.

    Margins are the single internal representation -- the configured ROI states them
    directly, so a caller-supplied fraction is converted rather than carried alongside.
    """
    ny, nx = shape
    return int(nx * (1.0 - usable_fraction) / 2), int(ny * (1.0 - usable_fraction) / 2)


def _window(
    shape: tuple[int, int], center_x: int, center_y: int, margin_horizontal: int, margin_vertical: int
) -> tuple[slice, slice]:
    """The usable window, centred on (center_x, center_y).

    The margins say how much of each edge is unusable, so the window's half-extents are
    what remains: `nx // 2 - margin_horizontal` and `ny // 2 - margin_vertical`. The
    window keeps that SIZE but sits on the given centre, which is the fibre rather than
    the middle of the sensor.

    Clipped to the sensor, so a centre near an edge yields a smaller window rather than
    an out-of-bounds one. Both frames are windowed identically, so clipping shifts the
    region but never the measurement.
    """
    ny, nx = shape
    half_x = max(1, nx // 2 - margin_horizontal)
    half_y = max(1, ny // 2 - margin_vertical)
    x0, x1 = max(0, center_x - half_x), min(nx, center_x + half_x)
    y0, y1 = max(0, center_y - half_y), min(ny, center_y + half_y)
    return slice(y0, y1), slice(x0, x1)


def _prepare(data: np.ndarray, window: tuple[slice, slice]) -> np.ndarray:
    """Crop to `window`, de-gradient, and flatten single-pixel spikes."""
    cropped = data[window].astype(np.float32)

    flattened = _bg_subtract(cropped)
    # Despeckle. A 3x3 median replaces an isolated spike with its neighbourhood while
    # leaving stars alone -- at 0.26"/px with ~2" seeing they are ~8 px across, far
    # wider than the kernel. Note this is the median ITSELF, not `data - median`: the
    # latter is a high-pass, which keeps single-pixel spikes and would make the problem
    # worse. tests/test_frame_shift.py pins the difference.
    return median_filter(flattened, size=3)


def measure_shift(
    reference: np.ndarray,
    final: np.ndarray,
    center_x: int,
    center_y: int,
    margin_horizontal: int = DEFAULT_MARGIN_HORIZONTAL,
    margin_vertical: int = DEFAULT_MARGIN_VERTICAL,
    upsample: int = DEFAULT_UPSAMPLE,
) -> ShiftResult:
    """Pixel shift that registers `final` onto `reference`.

    Sign convention -- the easy thing to get backwards, so it is pinned by a test:
    ``dx = +10`` means the field CONTENT sits 10 px further along +x in `final` than it
    did in `reference`. That is the operator's reading: "the sky moved this way".

    Note this is the NEGATIVE of what skimage hands back. ``phase_cross_correlation``
    returns the shift required to register the moving image back ONTO the reference, so
    content that moved by +12 comes back as -12; it is negated here so callers never
    have to remember that. skimage also works in (row, col) order, hence (y, x).

    Both frames must be the same shape and taken with the same exposure, gain and
    binning; the comparison is only meaningful between like and like.
    """
    if reference.shape != final.shape:
        raise ValueError(f"frames differ in shape: reference {reference.shape}, final {final.shape}")

    window = _window(reference.shape, center_x, center_y, margin_horizontal, margin_vertical)  # type: ignore[arg-type]
    ref_prepared = _prepare(reference, window)
    final_prepared = _prepare(final, window)

    (registration_y, registration_x), _error, _phase = phase_cross_correlation(
        ref_prepared, final_prepared, upsample_factor=upsample, normalization=None
    )
    # Negate: registration shift -> how the content actually moved.
    shift_y, shift_x = -registration_y, -registration_x

    result = ShiftResult(
        dx=round(float(shift_x), 2),
        dy=round(float(shift_y), 2),
        confidence=_confidence(ref_prepared, final_prepared, registration_y, registration_x),
        margin_horizontal=margin_horizontal,
        margin_vertical=margin_vertical,
        center_x=center_x,
        center_y=center_y,
        crop_shape=tuple(ref_prepared.shape),  # type: ignore[arg-type]
        at_origin=bool(shift_x == 0.0 and shift_y == 0.0),
    )
    if result.at_origin:
        logger.warning(
            "frame shift measured as exactly (0, 0): if the mount moved, this is more likely "
            "fixed-pattern noise winning the correlation than a real null result"
        )
    if result.confidence < MIN_CONFIDENCE:
        logger.warning(
            "frame shift confidence %.3f is below %.2f: the two frames do not correlate well, so "
            "dx=%.2f dy=%.2f should not be acted on. Usually too few stars, a cloud, or a frame "
            "taken at a different focus or exposure.",
            result.confidence,
            MIN_CONFIDENCE,
            result.dx,
            result.dy,
        )
    return result


def _confidence(reference: np.ndarray, final: np.ndarray, registration_y: float, registration_x: float) -> float:
    """How well the frames actually match once the measured shift is undone.

    The final frame is shifted back onto the reference and the two are correlated over
    the region that stayed in frame. A real match gives ~1.0; an answer read off noise
    gives ~0. Without this there is nothing at all to distinguish the two, since
    ``at_origin`` only catches the fixed-pattern case.
    """
    registered = ndi_shift(final, shift=(registration_y, registration_x), order=1, mode="constant", cval=np.nan)
    overlap = np.isfinite(registered)
    if overlap.sum() < 100:  # shifted almost entirely out of frame; nothing left to compare
        return 0.0
    correlation = np.corrcoef(reference[overlap].ravel(), registered[overlap].ravel())[0, 1]
    return 0.0 if np.isnan(correlation) else round(float(correlation), 4)


def max_reliable_shift(shape: tuple[int, int], margin_horizontal: int = DEFAULT_MARGIN_HORIZONTAL) -> float:
    """Rough upper bound, in pixels, on a shift this method can still measure.

    The sky common to both crops shrinks by the shift, so the correlation degrades as
    the overlap does. A third of the cropped width is a conservative working limit.
    """
    _ny, nx = shape
    return max(1.0, (nx - 2 * margin_horizontal)) / 3.0
