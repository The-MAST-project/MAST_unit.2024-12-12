"""Inline guiding validation (design §3.3): the absolute check the differential tracker cannot make.

The guide loop holds the field at the reference frame's position -- it has no absolute reference, so a
slow walk-off (accumulated bias, flexure, a reference frame itself slightly off the fibre) reads as
"on target" forever. Every so often the loop solves the current pointing and asks this: where does the
fibre actually point now, versus the lock (where the target belongs)?

- within tolerance -> ``VALID_OK``: nothing to do.
- a correctable error -> ``VALID_SHIFT`` with a **reference shift** in binned px: how far the target has
  walked from the fibre. The loop applies it by shifting the guide-star reference positions, so the
  next differential measure reads it as a drift and the normal policy nulls it (clamped, ramped). One
  correction authority (the loop); validation never commands the mount.
- too large to chase (``> alert_factor x`` tolerance), or repeated solve failures -> ``VALID_ALERT``:
  the field is likely lost -- flag it rather than slew the mount toward a possibly-bad solve.
- a single solve failure -> ``VALID_SOLVE_FAILED``: skip this round, keep guiding differentially.

Pure: it takes a solved `SolvingSolution` (or ``None`` for a failed solve) and decides. Running the
solve (its own exposure, through the unit solver) is the backend's hardware part; this is unit-tested
like `guide_cycle`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pixel_to_sky import PixelToSky

#: Default: an error beyond this many times the guiding tolerance is "lost", not a correction.
DEFAULT_ALERT_FACTOR = 1.5
#: Default: this many solves in a row failing is itself an alert (cloud, lost field).
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

VALID_OK = "valid_ok"  # the fibre is on target within tolerance
VALID_SHIFT = "valid_shift"  # a correctable absolute error; shift the reference by shift_px
VALID_ALERT = "valid_alert"  # out of position / repeated failures -- do not chase
VALID_SOLVE_FAILED = "valid_solve_failed"  # one solve failed; skip this round


@dataclass(frozen=True)
class ValidationResult:
    """What a validation round decided. ``error_arcsec`` is the absolute fibre error (set whenever a
    solve succeeded); ``shift_px`` (binned px) is the reference shift to apply, set only on
    ``VALID_SHIFT``; ``reason`` carries the alert / failure detail for the guide log."""

    decision: str
    error_arcsec: float | None = None
    shift_px: tuple[float, float] | None = None
    reason: str | None = None


def _separation_arcsec(a, b) -> float:
    """Angular separation between two `common.utils.Coord` (ra/dec Angles), in arcsec."""
    import astropy.units as u  # noqa: PLC0415
    from astropy.coordinates import SkyCoord  # noqa: PLC0415

    sa = SkyCoord(ra=a.ra, dec=a.dec)
    sb = SkyCoord(ra=b.ra, dec=b.dec)
    return float(sa.separation(sb).to(u.arcsec).value)


def _reference_shift_px(now: PixelToSky, lock, crpix: tuple[float, float]) -> tuple[float, float]:
    """How far, in binned px, the target has walked from the fibre in the current frame: the fibre
    pixel minus the pixel the lock now lands on (``fibre - WCS_now^-1(lock)``). Adding this to the
    guide-star reference positions makes the loop drive the target back onto the fibre."""
    ra_deg = lock.ra.deg
    dec_deg = lock.dec.deg
    px, py = now.wcs.all_world2pix([[ra_deg, dec_deg]], 1)[0]  # 1-based, matching crpix
    return (float(crpix[0] - px), float(crpix[1] - py))


class GuideValidator:
    """Decide, from a fresh solve of the current pointing, whether the fibre is still on target.

    Holds the reference `PixelToSky` (for the ``lock`` and the fibre ``crpix``) and the guiding
    tolerance. Stateful only in its consecutive-failure count.
    """

    def __init__(
        self,
        pixel_to_sky: PixelToSky,
        tolerance_arcsec: float,
        *,
        alert_factor: float = DEFAULT_ALERT_FACTOR,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ):
        self.pixel_to_sky = pixel_to_sky
        self.tolerance_arcsec = float(tolerance_arcsec)
        self.alert_factor = float(alert_factor)
        self.max_consecutive_failures = int(max_consecutive_failures)
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def validate(self, solution) -> ValidationResult:
        """Decide from ``solution`` (a succeeded `SolvingSolution`, or ``None`` for a failed solve)."""
        if solution is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.max_consecutive_failures:
                return ValidationResult(VALID_ALERT, reason=f"{self._consecutive_failures} solves failed in a row")
            return ValidationResult(VALID_SOLVE_FAILED, reason="solve failed")
        try:
            now = PixelToSky.from_solution(solution, crpix=self.pixel_to_sky.crpix)
        except ValueError as ex:  # a solve without a CD matrix is unusable, like a failure
            self._consecutive_failures += 1
            return ValidationResult(VALID_SOLVE_FAILED, reason=f"unusable solution: {ex}")
        self._consecutive_failures = 0

        error_arcsec = _separation_arcsec(self.pixel_to_sky.lock, now.lock)  # lock vs fibre-points-now
        if error_arcsec <= self.tolerance_arcsec:
            return ValidationResult(VALID_OK, error_arcsec=error_arcsec)
        if error_arcsec > self.alert_factor * self.tolerance_arcsec:
            return ValidationResult(VALID_ALERT, error_arcsec=error_arcsec, reason="out of position")
        shift_px = _reference_shift_px(now, self.pixel_to_sky.lock, self.pixel_to_sky.crpix)
        return ValidationResult(VALID_SHIFT, error_arcsec=error_arcsec, shift_px=shift_px)
