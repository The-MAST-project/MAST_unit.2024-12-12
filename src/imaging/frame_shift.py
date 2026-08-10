"""Measure the pixel shift between two frames of the same field.

Used by the spiral search: the operator drives the mount until the star sits on the
optical axis (judged on the independent Ximea camera), and the shift between the
reference frame and the final one says, in pixels, where the optical axis lies relative
to where the field started.

Correlating the whole field rather than tracking one star means nothing has to be
detected, centroided or matched -- which is what makes this robust on a field the
operator has not vetted.

Three preparation steps matter, and each is there for a reason:

* **Crop to the middle of the frame.** The optics have pronounced coma, so PSFs in the
  outer field are elongated and their shape varies with position -- they smear the
  correlation peak rather than sharpen it. The edges are also the most vignetted.
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
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.ndimage import median_filter
from skimage.registration import phase_cross_correlation

from common.mast_logging import get_logger

from .hfd import _bg_subtract

logger = get_logger(__name__)

#: Fraction of each axis kept before correlating. See the coma note above.
DEFAULT_CROP_FRACTION = 0.66

#: Sub-pixel refinement passed to phase_cross_correlation.
DEFAULT_UPSAMPLE = 100


@dataclass
class ShiftResult:
    """Outcome of one reference-vs-final comparison. All shifts are in pixels."""

    dx: float
    dy: float
    error: float
    """skimage's normalised RMS error for the registration; lower is better."""
    crop_fraction: float
    crop_shape: tuple[int, int]
    at_origin: bool
    """True if the peak landed on exactly (0, 0). Suspicious whenever the mount is
    known to have moved -- the classic signature of fixed-pattern noise winning."""

    def as_dict(self) -> dict:
        return asdict(self)


def _prepare(data: np.ndarray, crop_fraction: float) -> np.ndarray:
    """Crop to the central `crop_fraction`, de-gradient, and flatten single-pixel spikes."""
    ny, nx = data.shape
    half_y, half_x = int(ny * crop_fraction) // 2, int(nx * crop_fraction) // 2
    cy, cx = ny // 2, nx // 2
    cropped = data[cy - half_y : cy + half_y, cx - half_x : cx + half_x].astype(np.float32)

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
    crop_fraction: float = DEFAULT_CROP_FRACTION,
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

    ref_prepared = _prepare(reference, crop_fraction)
    final_prepared = _prepare(final, crop_fraction)

    (registration_y, registration_x), error, _phase = phase_cross_correlation(
        ref_prepared, final_prepared, upsample_factor=upsample
    )
    # Negate: registration shift -> how the content actually moved.
    shift_y, shift_x = -registration_y, -registration_x

    result = ShiftResult(
        dx=round(float(shift_x), 2),
        dy=round(float(shift_y), 2),
        error=round(float(error), 4),
        crop_fraction=crop_fraction,
        crop_shape=tuple(ref_prepared.shape),  # type: ignore[arg-type]
        at_origin=bool(shift_x == 0.0 and shift_y == 0.0),
    )
    if result.at_origin:
        logger.warning(
            "frame shift measured as exactly (0, 0): if the mount moved, this is more likely "
            "fixed-pattern noise winning the correlation than a real null result"
        )
    return result


def max_reliable_shift(shape: tuple[int, int], crop_fraction: float = DEFAULT_CROP_FRACTION) -> float:
    """Rough upper bound, in pixels, on a shift this method can still measure.

    The sky common to both crops shrinks by the shift, so the correlation degrades as
    the overlap does. A third of the cropped width is a conservative working limit.
    """
    _ny, nx = shape
    return (nx * crop_fraction) / 3.0
