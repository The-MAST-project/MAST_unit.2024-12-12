"""The PHD2 guide algorithm as a pure, stateful policy: hysteresis, a min-move dead-band and a
max-correction clamp, per axis, in arcsec (design §4, §5 -- ported from PHD2 as a small policy, not
its process).

``update(error_ra_arcsec, error_dec_arcsec)`` returns the correction ``(ra, dec)`` to send, or
``None`` when both axes fall inside the dead-band (nothing worth moving for). The order is PHD2's
(§5): hysteresis filtering, then MinMove, then the clamp. Pure -- no mount, no config, no WCS -- so
the loop stays testable; the caller wires the config values in and sends the result to the mount.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class _Axis:
    """One axis of the policy. ``_history`` is the weighted (low-pass) error PHD2's hysteresis keeps."""

    aggressiveness: float
    hysteresis: float
    min_move_arcsec: float
    max_correction_arcsec: float
    _history: float = field(default=0.0, init=False)

    def update(self, error: float) -> float:
        # Hysteresis: blend the fresh error with its running weighted history -- a low-pass that stops
        # the loop chasing single-frame jitter. The history advances every cycle, dead-band or not.
        self._history = (1.0 - self.hysteresis) * error + self.hysteresis * self._history
        correction = self.aggressiveness * self._history
        # MinMove dead-band: below it, do not move -- do not chase noise (§5, applied after hysteresis).
        if abs(correction) < self.min_move_arcsec:
            return 0.0
        # Clamp: no single correction exceeds the cap, so one bad centroid cannot throw the fibre (§4).
        if abs(correction) > self.max_correction_arcsec:
            return math.copysign(self.max_correction_arcsec, correction)
        return correction

    def reset(self) -> None:
        self._history = 0.0


@dataclass
class CorrectionPolicy:
    """Hysteresis + min-move dead-band + clamp, per axis. Defaults are PHD2's v1 values (§4)."""

    aggressiveness_ra: float = 0.7
    aggressiveness_dec: float = 0.7
    hysteresis_ra: float = 0.1
    hysteresis_dec: float = 0.1
    min_move_ra_arcsec: float = 0.0
    min_move_dec_arcsec: float = 0.0
    max_correction_arcsec: float = 2.0

    def __post_init__(self) -> None:
        self._ra = _Axis(self.aggressiveness_ra, self.hysteresis_ra, self.min_move_ra_arcsec, self.max_correction_arcsec)
        self._dec = _Axis(
            self.aggressiveness_dec, self.hysteresis_dec, self.min_move_dec_arcsec, self.max_correction_arcsec
        )

    def update(self, error_ra_arcsec: float, error_dec_arcsec: float) -> tuple[float, float] | None:
        """The correction ``(ra, dec)`` in arcsec to send, or ``None`` when both axes are in the
        dead-band. ``error`` is target-minus-current, as `PixelToSky.correction` returns it, so the
        correction has the same sign as the error."""
        correction_ra = self._ra.update(error_ra_arcsec)
        correction_dec = self._dec.update(error_dec_arcsec)
        if correction_ra == 0.0 and correction_dec == 0.0:
            return None
        return correction_ra, correction_dec

    def reset(self) -> None:
        """Forget the hysteresis history (e.g. after a re-reference or a pause)."""
        self._ra.reset()
        self._dec.reset()
