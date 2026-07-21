"""Detection-free focus metrics -- "how sharp is this frame", with no stars found.

Every metric the focus pipeline currently uses depends on detecting sources, and
on-sky that is exactly what keeps failing (2026-07-21):

* cross-matched HFD needs the SAME star found in most frames of a sweep -- it
  returned 0 consistent stars at every threshold once the sweep spanned enough
  defocus, because a star is a 15px point at one end and a ~150px annulus at the
  other, and segmentation centroids of a fragmenting ring wander tens of pixels;
* per-frame HFD tracks the detected star COUNT rather than focus (33 stars ->
  10.3px, 152 stars -> 13.2px), because fewer detections means only the
  brightest, most compact sources passed threshold;
* donut diameter only exists once the stars ARE annuli.

The metrics here need no detection, no threshold, no cross-frame identity, and
no assumption about morphology, so they stay meaningful across the whole range
where the others break.  They are for **coarse acquisition** -- finding which
few-thousand-tick region focus lives in -- not for the final solution: they give
a relative score, not a physical diameter with a tolerance.  Hand over to the
HFD V-curve once inside one morphological regime.

**The principle.**  Sharp stars carry high spatial frequencies; defocus spreads
the same flux over more pixels and smooths those edges away.  Squared-gradient
(Brenner) energy therefore falls as defocus grows, and is normalised by total
flux so it tracks sharpness rather than brightness or transparency.

**A concentration metric was tried and REJECTED -- do not re-add it.**  The
obvious companion statistic, ``sum(I^2)/sum(I)^2`` (flux packed into fewest
pixels), rests on total flux being constant so that focus merely redistributes
it.  Measured on the 2026-07-21 sweep, both halves of that failed: total flux
swung ~30% frame to frame, and the metric ranked the MOST defocused frame
(11450, carrying ~100-150px annuli) as the sharpest, 7.1 against 3.2 at 9050
where sources are compact ~14px.  It was not the background model -- swapping
the cheap block median for ``Background2D`` moved it by <2% (3.230 -> 3.218 and
7.116 -> 7.254).  Whatever it responds to on this system, it is not focus.

**Known limitation -- saturation.**  Near focus a bright star's core clips at
the ADU ceiling and stops sharpening, flattening the metric exactly where it
should peak.  Saturated pixels are excluded (``max_value``) and
``saturated_fraction`` is reported, so a caller can see when that correction is
doing heavy lifting rather than trust the score blindly.

**Validation status.**  ``gradient`` falls monotonically from 8.70 at 9050 to
7.21 at 11450, agreeing with the independent donut-morphology evidence that
focus lies below 9050.  That is supporting evidence, NOT proof: we have no
frames near focus, so the claim "it peaks AT focus" is still untested.  It is
trustworthy for choosing a direction; treat a peak it reports as a hypothesis
until an HFD V-curve confirms it.
"""

from __future__ import annotations

import logging

import numpy as np

from calibration.analysis.hfd import _apply_crop, _disk_crop_box, _load
from calibration.logging_context import init_calibration_log

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)


def _coarse_background(data: np.ndarray, block: int = 128) -> np.ndarray:
    """Block-median background -- cheap, and enough for a concentration metric.

    Deliberately NOT ``Background2D``: this runs on every frame of an
    acquisition scan where speed is the point, and the metric only needs the
    sky pedestal removed, not a photometric-grade background model.
    """
    ny, nx = data.shape
    by, bx = ny // block, nx // block
    if by < 2 or bx < 2:
        return np.full_like(data, float(np.median(data)))
    trimmed = data[: by * block, : bx * block]
    tiles = trimmed.reshape(by, block, bx, block)
    coarse = np.median(tiles, axis=(1, 3))
    # Nearest-neighbour upsample back to full size, then pad the trimmed edges.
    upsampled = np.repeat(np.repeat(coarse, block, axis=0), block, axis=1)
    out = np.full_like(data, float(np.median(coarse)))
    out[: by * block, : bx * block] = upsampled
    return out


def frame_sharpness(
    image,
    center=None,  # low-coma disk, as for frame_hfd; crops before measuring
    radius=None,
    max_value: float = 63000.0,  # saturated pixels are excluded (see module doc)
    block: int = 128,
) -> dict:
    """Score one frame's sharpness. Higher = sharper = closer to focus.

    Returns ``gradient`` (the score) plus diagnostics: ``saturated_fraction``
    and ``total_flux``, both of which invalidate a comparison when they move.
    ``total_flux`` in particular is what exposed the rejected concentration
    metric -- a ~30% swing across a sweep means frames are not comparable on
    flux-based grounds, so it is reported rather than hidden.
    """
    data = _load(image)
    box = _disk_crop_box(data.shape, center, radius, 0.0, block) if center is not None else None
    data = _apply_crop(data, box)

    sub = data - _coarse_background(data, block)
    good = data < max_value
    saturated_fraction = float(1.0 - good.mean())

    vals = np.where(good, sub, 0.0)
    positive = np.clip(vals, 0.0, None)  # negatives are sky noise, not signal
    total = float(positive.sum())
    if total <= 0:
        logger.debug("frame has no positive flux after background subtraction")
        return {
            "gradient": float("nan"),
            "saturated_fraction": saturated_fraction,
            "total_flux": 0.0,
        }

    # Squared-gradient (Brenner) energy: sharp stars carry steep edges, defocus
    # smooths them away.  Normalised by total flux squared (and rescaled by the
    # pixel count to keep the number O(1)) so it tracks sharpness rather than
    # brightness -- transparency changes must not look like focus changes.
    gy = np.diff(positive, axis=0)
    gx = np.diff(positive, axis=1)
    gradient = float(((gy**2).sum() + (gx**2).sum()) / (total**2) * positive.size)

    return {
        "gradient": gradient,
        "saturated_fraction": saturated_fraction,
        "total_flux": total,
    }
