"""One guide cycle: from a frame to a decision, composing the measurement, the pixel->sky map and the
correction policy. The testable brain of the guide loop -- no camera, no mount, no threads.

Each cycle: measure the frame against the fixed guide-star set (seeded at the running estimate of
where the field now sits), gate on the per-star scatter, turn the pixel drift into a sky error at the
fibre, and run it through the policy. The result says what to do and why, for the loop to act on and
the guide log to record.

The running ``predicted_offset`` is the field's last measured drift: because every correction is
clamped and the frame-to-frame change (residual drift plus a correction's effect) stays well inside
the measurement search box, seeding the next frame's star search from the last position keeps the
stars in reach without a global re-find. A frame that is held (high scatter) or lost does not advance
it -- an untrusted measurement must not move the seed.

The hardware around this -- asking the ZWO imager for a binning-2 frame in memory, sending the
correction to the mount, the cadence, the guide log -- is the `GuiderInterface` shell that wraps a
`GuideCycle`; it is wired and validated on a unit. This part is pure and unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass

from .correction_policy import CorrectionPolicy
from .core import MastGuiderCore, Measurement, NoMeasurementError
from .pixel_to_sky import PixelToSky

#: Per-star robust scatter (px) above which a frame's stars disagree too much to trust -- cloud or a
#: seeing event -- so the cycle holds rather than correct on it (§11.2).
DEFAULT_MAX_SCATTER_PX = 1.0

# The decisions a cycle can reach.
APPLIED = "applied"  # a correction is to be sent to the mount
DEADBAND = "deadband"  # measured and trusted, but the correction is below the min-move threshold
HELD_SCATTER = "held_scatter"  # the stars disagree too much; do not correct on this frame
STAR_LOST = "star_lost"  # too few guide stars measured to give an offset at all


@dataclass(frozen=True)
class CycleResult:
    """What a cycle decided, for the loop to act on and the guide log to record.

    ``correction_arcsec`` (plain RA, Dec) is set only when ``decision == APPLIED``; ``error_arcsec``
    is the sky error at the fibre before the policy (set whenever the frame was measured and trusted);
    ``measurement`` and ``scatter_px`` carry the per-frame health.
    """

    decision: str
    measurement: Measurement | None = None
    error_arcsec: tuple[float, float] | None = None
    correction_arcsec: tuple[float, float] | None = None
    scatter_px: float | None = None


class GuideCycle:
    """Compose the measurement core, the pixel->sky map and the policy into one ``cycle(frame)``."""

    def __init__(
        self,
        guider: MastGuiderCore,
        pixel_to_sky: PixelToSky,
        policy: CorrectionPolicy,
        *,
        max_scatter_px: float = DEFAULT_MAX_SCATTER_PX,
    ):
        self.guider = guider
        self.pixel_to_sky = pixel_to_sky
        self.policy = policy
        self.max_scatter_px = float(max_scatter_px)
        self._predicted: tuple[float, float] = (0.0, 0.0)

    @property
    def predicted_offset(self) -> tuple[float, float]:
        """The field's last trusted drift (binned px), which seeds the next frame's star search."""
        return self._predicted

    def cycle(self, frame) -> CycleResult:
        """Measure ``frame``, decide, and return the result. Does not touch the mount -- the caller
        sends ``correction_arcsec`` when the decision is ``APPLIED``."""
        try:
            measurement = self.guider.measure(frame, predicted_offset=self._predicted)
        except NoMeasurementError:
            return CycleResult(STAR_LOST)  # seed left where it was; a lost frame must not move it
        if measurement.scatter_px > self.max_scatter_px:
            return CycleResult(HELD_SCATTER, measurement=measurement, scatter_px=measurement.scatter_px)
        # Trusted: advance the seed to where the field now is, then turn the drift into a sky error.
        self._predicted = (measurement.dx, measurement.dy)
        error = self.pixel_to_sky.correction(measurement.dx, measurement.dy)
        correction = self.policy.update(error[0], error[1])
        if correction is None:
            return CycleResult(DEADBAND, measurement=measurement, error_arcsec=error, scatter_px=measurement.scatter_px)
        return CycleResult(
            APPLIED,
            measurement=measurement,
            error_arcsec=error,
            correction_arcsec=correction,
            scatter_px=measurement.scatter_px,
        )
