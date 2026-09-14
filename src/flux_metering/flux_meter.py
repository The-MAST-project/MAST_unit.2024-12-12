"""The flux meter behind an interface, plus a simulator.

The Zelux is not attached to every machine that has to run this code -- it was not attached
to any of them while this was written -- so the camera is injected rather than reached for.
That is the same reason `tests/test_spiral_search.py` injects its status source: it is what
makes the spiral loop, the stopping rules and the correlation testable without hardware.

`ThorCam` (flux_metering/thorcam/thorcam.py) implements this over the Thorlabs SDK;
`SimulatedFluxMeter` implements it over a 2-D Gaussian, so a whole run can be exercised on a
desk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from common.mast_logging import get_logger

logger = get_logger(__name__)


class FluxMeterError(Exception):
    """The flux meter could not be opened, configured, or read."""


@runtime_checkable
class FluxMeter(Protocol):
    """A camera that sees only the light coming out of the fibre.

    Deliberately small. Everything the procedure needs is here, and nothing else -- a
    narrow surface is what keeps the simulator honest, since a simulator that has to
    imitate a wide API stops being evidence that the real one works.
    """

    def configure(self, exposure_us: int, gain: int, black_level: int) -> None:
        """Apply the settings for this run. Raises `FluxMeterError` if the camera will not
        take them -- notably an exposure outside its supported range, which must fail loudly
        rather than be silently clamped.

        All three are INTEGERS, because the SDK's setters are `c_int`: a float reaches
        ctypes and raises `TypeError: int expected` rather than being rounded. On the
        CS165MU the ranges are gain (0, 480), black level (0, 511) and exposure
        (64, 26843418) us."""
        ...

    def expose(self) -> np.ndarray:
        """One frame, as a 2-D array."""
        ...

    @property
    def saturation_level(self) -> int:
        """The full-scale ADU, derived from the camera's bit depth.

        Read from the camera rather than configured, so that "saturated" cannot come to
        mean something different when the camera is reconfigured or replaced.
        """
        ...

    @property
    def description(self) -> str:
        """Model and serial, for the run's metadata."""
        ...

    @property
    def model(self) -> str:
        """The camera model, for the frame's `INSTRUME`.

        Separate from `description` rather than parsed out of it: `description` is a
        human-readable blob whose shape is free to change, and a FITS card built by
        splitting it would break silently the first time it did.
        """
        ...

    @property
    def serial_number(self) -> str:
        """The camera serial, for the frame's `CAMSN`. Which physical camera took this."""
        ...

    def close(self) -> None: ...


#: Below this FWHM a "detection" is not the fibre. The fibre output measures 12.4-13.3 px on
#: the real camera; a hot pixel or a cosmic ray measures about 1.1.
#:
#: This exists because the backend's result cannot be trusted to say how it found the source.
#: When segmentation finds nothing it falls back internally to a smoothed peak search, and
#: `detect_method` STILL reports `segment` -- the only trace is a line printed to stdout. On
#: run 0006 that fallback locked onto a hot pixel at (943, 162) in two of step 22's three
#: exposures and returned ~1338 counts as though it were the fibre, while the third exposure
#: of the same step measured the real thing at 92666.
#:
#: 3.0 px is the backend's own figure for "too concentrated to be real": SPIKE_PEAK_FRACTION
#: is set, in its words, "tight enough to also reject a source as narrow as 3 px FWHM". That
#: rejection only guards the segmentation path, so it is applied here to every path.
MIN_PLAUSIBLE_FWHM_PX = 3.0


@dataclass
class FluxMeasurement:
    """One frame, reduced. `net_counts` is None when there was nothing to measure.

    None rather than 0.0, and the distinction is the point: a frame the photometry could
    not measure and a frame that genuinely contains no light produce the same number under
    a plain sum, and the spiral's arg-max cannot tell them apart.
    """

    net_counts: float | None
    position_source: str
    counts_err: float | None = None
    snr: float | None = None
    x: float | None = None
    y: float | None = None
    radius_px: float | None = None
    bkg_level: float | None = None
    fwhm_px: float | None = None
    #: Inside the aperture, at `saturation_threshold`. This is the one that means the
    #: measurement is a lower limit.
    saturated_in_aperture: int = 0
    #: Anywhere in the frame, same threshold. A diagnostic: it is what shows a frame is
    #: clipped somewhere OTHER than the fibre, which is how a hot pixel announces itself.
    saturated_in_frame: int = 0
    saturation_threshold: int | None = None
    error: str | None = None

    @property
    def measured(self) -> bool:
        return self.net_counts is not None


def _why_not_the_fibre(data: np.ndarray, found: dict, spike_peak_fraction: float) -> str | None:
    """Why this detection is not the fibre output, or None if it plausibly is.

    Two tests, because neither alone is enough.

    **Width.** The fibre measures 12.4-13.3 px FWHM on the real camera; a hot pixel measures
    about 1.1. That is what caught run 0006's step 22.

    **Concentration.** The width test fails on a clean frame: `measure_fwhm` gives up when it
    finds no positive peak above the background and returns the CONFIGURED GUESS -- 11 px,
    comfortably above any width floor -- so a spike on a flat field passes as a plausible
    source. Asking how much of the light sits in one pixel does not degrade that way.

    The fraction is the backend's own `SPIKE_PEAK_FRACTION` and its own reasoning: a Gaussian
    of FWHM f puts about 0.88/f^2 of its counts in the peak pixel, ~0.007 for this fibre,
    while an isolated spike puts all of them there. The backend applies it only to
    segmentation detections; its internal peak-search fallback bypasses it entirely, which is
    exactly the path that produced the bad measurement.
    """
    fwhm = float(found["fwhm_pix"])
    x, y = float(found["x"]), float(found["y"])
    if fwhm < MIN_PLAUSIBLE_FWHM_PX:
        return (
            f"rejected a detection at ({x:.0f}, {y:.0f}) with FWHM {fwhm:.1f} px, below the "
            f"{MIN_PLAUSIBLE_FWHM_PX:g} px floor: too narrow to be the fibre"
        )

    net = float(found["net_counts"])
    if net <= 0:
        return None  # nothing to be concentrated; the caller reads net_counts itself

    radius = float(found["radius_pix"])
    ny, nx = data.shape
    yy, xx = np.ogrid[0:ny, 0:nx]
    inside = (xx - x) ** 2 + (yy - y) ** 2 <= radius**2
    if not inside.any():
        return None
    peak = float(data[inside].max()) - float(found["bkg_level"])
    fraction = peak / net
    if fraction > spike_peak_fraction:
        return (
            f"rejected a detection at ({x:.0f}, {y:.0f}): {100 * fraction:.0f}% of its light is "
            f"in one pixel (limit {100 * spike_peak_fraction:.0f}%), so it is a spike, not the fibre"
        )
    return None


def measure_frame(frame: np.ndarray, last_position: tuple[float, float] | None = None) -> FluxMeasurement:
    """Aperture photometry on one ThorCam frame.

    Replaces a plain sum over the whole frame. That sum was defensible in principle -- the
    ThorCam sees only the fibre output against black -- but on run 0006 it measured the sky
    brightening rather than the fibre: its curve rose monotonically across all 23 steps,
    tracking a background that drifted 2.518 -> 2.596 counts/px, which over 1,555,200 pixels
    is more counts than the entire range it reported. It picked a different arg-max cell
    from the aperture. See section 18.2 of flux_metering_design.md.

    **Never raises.** `measure_single_image` raises when it detects nothing, and on a spiral
    that is the common case rather than an error: most of a walk is far from the peak, where
    the fibre is dark. Three outcomes instead:

    - `detected` -- the source was found in this frame.
    - `inherited` -- nothing was found, so the frame was re-measured at `last_position`,
      through the same aperture as every other step. The flux curve stays continuous and
      comparable rather than gaining a hole.
    - `none` -- nothing found and no previous position, so there is no measurement.
      `net_counts` is None.

    The fallback is ours and takes precedence over the backend's own smoothed-peak search,
    which cannot tell a hot pixel from a faint fibre: on step 22 of run 0006 it locked onto
    one at (943, 162), FWHM 1.1 px, and reported 1338 counts that were not the fibre.
    """
    import warnings

    from photutils.utils.exceptions import NoDetectionsWarning

    from flux_metering.aperture_photometry_single import (
        SATURATION_ADU,
        SPIKE_PEAK_FRACTION,
        measure_single_image,
    )

    data = np.asarray(frame, dtype=float)
    in_frame = int(np.count_nonzero(data >= SATURATION_ADU))

    def reduced(result: dict, source: str) -> FluxMeasurement:
        return FluxMeasurement(
            net_counts=float(result["net_counts"]),
            position_source=source,
            counts_err=float(result["counts_err"]),
            snr=float(result["snr"]),
            x=float(result["x"]),
            y=float(result["y"]),
            radius_px=float(result["radius_pix"]),
            bkg_level=float(result["bkg_level"]),
            fwhm_px=float(result["fwhm_pix"]),
            saturated_in_aperture=int(result["n_saturated"]),
            saturated_in_frame=in_frame,
            saturation_threshold=SATURATION_ADU,
        )

    # photutils warns when it finds nothing. That is not news here -- it is the condition
    # this function exists to handle, and most of a spiral meets it -- so it is silenced
    # rather than emitted once per dark frame into the night's log.
    rejection: str | None = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NoDetectionsWarning)
            found = measure_single_image(data, verbose=False)
        rejection = _why_not_the_fibre(data, found, SPIKE_PEAK_FRACTION)
        if rejection is None:
            return reduced(found, "detected")
        # Found something, but not the fibre. Fall through to the same path as finding
        # nothing: this is the backend's internal peak search having locked onto a spike.
        logger.warning(f"flux metering: {rejection}")
    except Exception as detection_failed:  # noqa: BLE001 -- a dark cell is not a run failure
        rejection = str(detection_failed)

    def unmeasured() -> FluxMeasurement:
        return FluxMeasurement(
            net_counts=None,
            position_source="none",
            saturated_in_frame=in_frame,
            saturation_threshold=SATURATION_ADU,
            error=rejection,
        )

    if last_position is None:
        return unmeasured()

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NoDetectionsWarning)
            inherited = reduced(measure_single_image(data, position=last_position, verbose=False), "inherited")
        inherited.error = rejection  # why the frame's own detection was not used
        return inherited
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"flux metering: could not measure at the inherited position {last_position}: {ex}")
        rejection = str(ex)
        return unmeasured()


def saturated_pixels(frame: np.ndarray, saturation_level: int) -> int:
    """How many pixels are at or above full scale.

    A count, not a boolean, and the caller compares it against a threshold rather than
    zero: the field is black, so a single hot pixel or one cosmic ray would otherwise mark
    every frame of a 30-minute run as saturated.
    """
    return int(np.count_nonzero(np.asarray(frame) >= saturation_level))


class SimulatedFluxMeter:
    """A flux meter whose reading peaks at a chosen offset from the spiral origin.

    Exists so the parts of a run that do not involve light -- the spiral walk, the ring
    stopping rule, the arg-max, the products, the result -- can be exercised end to end.
    The caller drives `at_cell` as the mount moves; the reading follows a 2-D Gaussian
    about `peak_cell`, which is what the search is supposed to find.

    `saturate_above` models the one failure the design deliberately does not gate on, so a
    test can assert that a saturated run still finishes and still reports `argmax_saturated`.
    """

    def __init__(
        self,
        peak_cell: tuple[int, int] = (0, 0),
        sigma_cells: float = 2.0,
        # Comfortably under 10-bit full scale, so the DEFAULT simulator does not saturate.
        # Saturation is a case a test opts into by raising this, not one it has to work
        # around: a default that clips would make every arg-max assertion a coin toss among
        # the clipped cells, which is precisely the failure `argmax_saturated` reports.
        peak_counts: float = 800.0,
        background: float = 3.0,
        # 256x256, not the ThorCam's 1440x1080: large enough to hold the 36 px extraction
        # aperture and a background region around it, small enough that a spiral test
        # exposing several hundred frames stays fast. 64x64 -- what this was -- cannot hold
        # that aperture at all; it was sized for a whole-frame sum, which needs no geometry.
        shape: tuple[int, int] = (256, 256),
        # The spot's Gaussian sigma. 5.4 px is FWHM 12.7, which is what the real fibre
        # output measures: 12.4-13.3 px across every frame of run 0006. It matters because
        # the aperture radius is a fixed 36 px, so this sets the enclosed-flux fraction the
        # photometry sees -- a spot half the true width would make the simulator agree with
        # the aperture for the wrong reason.
        spot_sigma_px: float = 5.4,
        # 10-bit, like the CS165MU Zelux this stands in for. It was 12, and that made the
        # simulator disagree with the photometry about what saturation IS: the aperture code
        # counts pixels at or above 1022, the rail observed on the real sensor, so a 12-bit
        # simulated frame peaking at 3000 was reported as hundreds of saturated pixels while
        # being nowhere near its own full scale.
        bit_depth: int = 10,
        noise: float = 0.0,
        seed: int = 0,
    ):
        self.peak_cell = peak_cell
        self.sigma_cells = sigma_cells
        self.peak_counts = peak_counts
        self.background = background
        self.shape = shape
        self.spot_sigma_px = spot_sigma_px
        self._saturation = (1 << bit_depth) - 1
        self.noise = noise
        self._rng = np.random.default_rng(seed)
        self.at_cell: tuple[int, int] = (0, 0)
        self.exposure_us: int | None = None
        self.gain: float | None = None
        self.black_level: int | None = None
        self.closed = False

    def configure(self, exposure_us: int, gain: int, black_level: int) -> None:
        if exposure_us <= 0:
            raise FluxMeterError(f"exposure_us must be positive, got {exposure_us}")
        self.exposure_us, self.gain, self.black_level = exposure_us, gain, black_level

    def expose(self) -> np.ndarray:
        dx = self.at_cell[0] - self.peak_cell[0]
        dy = self.at_cell[1] - self.peak_cell[1]
        coupling = math.exp(-(dx * dx + dy * dy) / (2.0 * self.sigma_cells**2))

        ny, nx = self.shape
        yy, xx = np.mgrid[0:ny, 0:nx]
        cy, cx = (ny - 1) / 2.0, (nx - 1) / 2.0
        spot = np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * self.spot_sigma_px**2))

        frame = self.background + self.peak_counts * coupling * spot
        if self.noise:
            frame = frame + self._rng.normal(0.0, self.noise, size=frame.shape)
        return np.clip(frame, 0, self._saturation).astype(np.uint16)

    @property
    def saturation_level(self) -> int:
        return self._saturation

    @property
    def description(self) -> str:
        return f"SimulatedFluxMeter(peak_cell={self.peak_cell}, sigma_cells={self.sigma_cells})"

    @property
    def model(self) -> str:
        return "SimulatedFluxMeter"

    @property
    def serial_number(self) -> str:
        return "simulated"

    def close(self) -> None:
        self.closed = True
