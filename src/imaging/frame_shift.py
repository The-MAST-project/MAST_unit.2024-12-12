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
  both and correlate perfectly at zero shift. Phase correlation is especially prone to
  this: a single-pixel spike is delta-like, so its spectrum is flat and it plants a peak
  at exactly (0, 0), which on a sparse field can beat the real one. Subtracting a
  3x3-median-filtered copy flattens those spikes while barely touching stars, which at
  0.26"/px and ~2" seeing are ~8 px across.

Sub-pixel accuracy is nominally 1/``upsample`` px, but seeing, SNR and tracking drift
over an operator-paced sequence dominate long before that. Two decimals is honest.

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

#: Fraction of each sensor axis kept before correlating. See the coma note above.
DEFAULT_USABLE_FRACTION = 0.66

#: Sub-pixel refinement passed to phase_cross_correlation.
DEFAULT_UPSAMPLE = 100

#: Below this post-registration correlation the answer is not worth acting on.
MIN_CONFIDENCE = 0.5


@dataclass
class ShiftResult:
    """Outcome of one reference-vs-final comparison. All shifts are in pixels."""

    dx: float
    dy: float
    confidence: float
    """Pearson correlation between the reference and the registered final, over their
    overlap: ~1.0 when the frames genuinely match, ~0 when the answer is noise.

    This is computed here rather than taken from ``phase_cross_correlation``, whose
    documented ``error`` return is a constant 1.0 under the default
    ``normalization="phase"`` -- it discriminates nothing. tests/test_frame_shift.py
    pins that this one does."""
    usable_fraction: float
    center_x: int
    center_y: int
    crop_shape: tuple[int, int]
    at_origin: bool
    """True if the peak landed on exactly (0, 0). Suspicious whenever the mount is
    known to have moved -- the classic signature of fixed-pattern noise winning."""

    def as_dict(self) -> dict:
        return asdict(self)


def _window(shape: tuple[int, int], center_x: int, center_y: int, usable_fraction: float) -> tuple[slice, slice]:
    """The usable window: `usable_fraction` of each axis, centred on (center_x, center_y).

    Clipped to the sensor, so a centre near an edge yields a smaller window rather than
    an out-of-bounds one. Both frames are windowed identically, so clipping shifts the
    region but never the measurement.
    """
    ny, nx = shape
    half_x, half_y = int(nx * usable_fraction) // 2, int(ny * usable_fraction) // 2
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
    usable_fraction: float = DEFAULT_USABLE_FRACTION,
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

    window = _window(reference.shape, center_x, center_y, usable_fraction)  # type: ignore[arg-type]
    ref_prepared = _prepare(reference, window)
    final_prepared = _prepare(final, window)

    (registration_y, registration_x), _error, _phase = phase_cross_correlation(
        ref_prepared, final_prepared, upsample_factor=upsample
    )
    # Negate: registration shift -> how the content actually moved.
    shift_y, shift_x = -registration_y, -registration_x

    result = ShiftResult(
        dx=round(float(shift_x), 2),
        dy=round(float(shift_y), 2),
        confidence=_confidence(ref_prepared, final_prepared, registration_y, registration_x),
        usable_fraction=usable_fraction,
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


def max_reliable_shift(shape: tuple[int, int], usable_fraction: float = DEFAULT_USABLE_FRACTION) -> float:
    """Rough upper bound, in pixels, on a shift this method can still measure.

    The sky common to both crops shrinks by the shift, so the correlation degrades as
    the overlap does. A third of the cropped width is a conservative working limit.
    """
    _ny, nx = shape
    return (nx * usable_fraction) / 3.0
