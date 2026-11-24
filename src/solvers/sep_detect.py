"""Source detection with `sep` (SExtractor's core as a C library): the same contract as `extract_sources`, much faster.

`calibration.analysis.optical_center.extract_sources` estimates the sky with photutils' ``Background2D`` and finds sources
with ``detect_sources`` and ``SourceCatalog``. On a 2x2 solve grid most of the selection's time was that: interpolating the
background and noise meshes back to the frame, then the segmentation. `sep` does the same steps in C. Measured on mast08
(`docs/mastrometry_stages.md`, section 9.7) it is 2 to 14 times faster as a detector and 1.5 to 7 times faster as a whole
selection, and chooses the same stars.

What is the same: a 2 sigma threshold, 5 connected pixels, the background mesh of ``box_size`` with a 3x3 filter, the noise
taken locally, 8-connected sources, the first moment as the position. What is **off** so that the detections match the
photutils path rather than improve on it: the detection filter, deblending and cleaning (sources near a bright star's
wings). Each is a way to find more or fewer sources and would shift every threshold the selection was tuned on.

What differs: the background estimator is SExtractor's (a clipped mode and a spline) and not photutils' sigma-clipped median,
so the local noise, and with it which faint sources pass, moves a little.
"""

from __future__ import annotations

import numpy as np
import sep

#: The smallest noise (ADU) a threshold is taken against.
NOISE_FLOOR = 1e-3


def extract_sources_sep(
    image, nsigma: float = 2.0, npixels: int = 5, box_size: int = 50, exclude_mask=None, local_noise: bool = False
) -> dict | None:
    """The detections of `image`, as `extract_sources` returns them (see there); None when nothing was detected.

    `local_noise` thresholds at ``nsigma`` times the local background RMS; off, at ``nsigma`` times the frame's one figure.
    """
    data = np.ascontiguousarray(image, dtype=np.float32)
    ny, nx = data.shape
    mask = None if exclude_mask is None else np.ascontiguousarray(exclude_mask, dtype=bool)
    background = sep.Background(data, mask=mask, bw=min(box_size, nx), bh=min(box_size, ny), fw=3, fh=3)
    back = background.back()
    # a floor, far below any real noise (ADU): a frame with none, or float32's rounding of a flat one, is not all sources
    rms = np.maximum(background.rms(), NOISE_FLOOR)
    subtracted = data - back
    objects, segmentation = sep.extract(
        subtracted,
        nsigma if local_noise else nsigma * max(float(background.globalrms), NOISE_FLOOR),
        err=rms if local_noise else None,
        mask=mask,
        minarea=npixels,
        filter_kernel=None,
        deblend_nthresh=1,
        clean=False,
        segmentation_map=True,
    )
    if len(objects) == 0:
        return None
    return {
        "data_sub": subtracted.astype(float),
        "background_rms": rms.astype(float),
        "shape": (ny, nx),
        "x": objects["x"].astype(float),
        "y": objects["y"].astype(float),
        "theta": objects["theta"].astype(float),
        "ellipticity": (1.0 - objects["b"] / np.maximum(objects["a"], 1e-9)).astype(float),
        "area": objects["npix"].astype(float),
        "flux": objects["flux"].astype(float),
        "peak_x": objects["xpeak"].astype(float),
        "peak_y": objects["ypeak"].astype(float),
        "n_detected": len(objects),
        # with deblending off, the object `i` is the segmentation's label `i + 1`
        "segment_map": segmentation,
        "labels": np.arange(1, len(objects) + 1),
    }
