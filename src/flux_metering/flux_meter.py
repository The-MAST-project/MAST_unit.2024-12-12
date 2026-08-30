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
from typing import Protocol, runtime_checkable

import numpy as np


class FluxMeterError(Exception):
    """The flux meter could not be opened, configured, or read."""


@runtime_checkable
class FluxMeter(Protocol):
    """A camera that sees only the light coming out of the fibre.

    Deliberately small. Everything the procedure needs is here, and nothing else -- a
    narrow surface is what keeps the simulator honest, since a simulator that has to
    imitate a wide API stops being evidence that the real one works.
    """

    def configure(self, exposure_us: int, gain: float, black_level: int) -> None:
        """Apply the settings for this run. Raises `FluxMeterError` if the camera will not
        take them -- notably an exposure outside its supported range, which must fail loudly
        rather than be silently clamped."""
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

    def close(self) -> None: ...


def frame_flux(frame: np.ndarray, black_level: int) -> float:
    """Total light in the frame, above the black-level pedestal.

    A plain sum is the right estimator here and would not be on a star field: the ThorCam
    sees ONLY the target's light through the fibre, with black all around it, so there is
    no neighbour to exclude and no aperture to choose. Choosing one would only add a
    parameter that could be set wrong.
    """
    return float(np.sum(np.asarray(frame, dtype=float) - black_level))


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
        # Comfortably under 12-bit full scale, so the DEFAULT simulator does not saturate.
        # Saturation is a case a test opts into by raising this, not one it has to work
        # around: a default that clips would make every arg-max assertion a coin toss among
        # the clipped cells, which is precisely the failure `argmax_saturated` reports.
        peak_counts: float = 3000.0,
        background: float = 3.0,
        shape: tuple[int, int] = (64, 64),
        bit_depth: int = 12,
        noise: float = 0.0,
        seed: int = 0,
    ):
        self.peak_cell = peak_cell
        self.sigma_cells = sigma_cells
        self.peak_counts = peak_counts
        self.background = background
        self.shape = shape
        self._saturation = (1 << bit_depth) - 1
        self.noise = noise
        self._rng = np.random.default_rng(seed)
        self.at_cell: tuple[int, int] = (0, 0)
        self.exposure_us: int | None = None
        self.gain: float | None = None
        self.black_level: int | None = None
        self.closed = False

    def configure(self, exposure_us: int, gain: float, black_level: int) -> None:
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
        spot = np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * 3.0**2))

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

    def close(self) -> None:
        self.closed = True
