"""Re-reference the guide lock to the target, in the one window where that is possible.

Acquisition converges to well inside a pixel -- measured residuals over the
2026-09-14 run were 0.09-0.26", or 0.3-1.0 px at 0.27"/px. The open loop between
its last correction and ``SettleDone`` then gives that precision back: the mount
tracks unguided for 9.9 s when PHD2's chosen star is usable and 56.6 s when it is
not, and whatever the guide loop finally holds is wherever the field drifted to.

The nudge closes that gap. Once guiding has settled it solves the guide frame
PHD2 is already taking, measures how far the fiber now sits from the target, and
moves the lock position by that much. The correction is around a pixel against a
15 px search region, so the lock cannot break.

It runs between settle and the fold-mirror insertion, and nowhere else. That is
the last moment the target is observable: once the stage reaches SPEC the target
is being delivered into the fiber. Nothing is nudged after the mirror moves,
because a mirror-induced change in the guide star's apparent position cannot be
told from a real pointing change without an independent measurement -- and if the
shift is optical, correcting it with the mount drives the target off the fiber.

One coordinate note, because the alternative is a trap. PHD2 validates a lock
position against the image it is currently delivering -- the limit frame when one
is in force -- while a plate solve yields full-sensor pixels. Sending a solved
*absolute* position would land one crop origin away (520 px, about 2.3 arcmin, on
the derived frame) and would *succeed*, because that is a valid image coordinate:
the same full-sensor-versus-cropped confusion as MAST_unit#234, in a place where
it moves the mount.

This nudge never forms an absolute position. It computes an offset, which is the
same in both frames because they differ only by origin, and adds it to the lock
position PHD2 itself reported -- already in PHD2's frame, whatever that is. There
is deliberately no sensor-to-image conversion here to get wrong.

See MAST_unit#19.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

from astropy.coordinates import Angle

from common.config.phd2 import LockNudgeConfig
from common.interfaces.solving import SolvingSolution
from common.mast_logging import get_logger
from common.utils import Coord, function_name

logger = get_logger(__name__)


@dataclass
class NudgeOutcome:
    """What the nudge did, and enough of why to read it back off a log."""

    applied: bool
    reason: str
    offset_arcsec: float | None = None
    offset_px: tuple[float, float] | None = None
    lock_before: tuple[float, float] | None = None
    lock_after: tuple[float, float] | None = None
    residual_px: float | None = None
    telemetry: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"NudgeOutcome(applied={self.applied}, reason={self.reason!r}, "
            f"offset_arcsec={self.offset_arcsec}, offset_px={self.offset_px}, "
            f"lock_before={self.lock_before}, lock_after={self.lock_after}, "
            f"residual_px={self.residual_px})"
        )


def sky_offset_to_detector_pixels(
    solution: SolvingSolution,
    d_ra_arcsec: float,
    d_dec_arcsec: float,
    dec_deg: float,
    downsample_factor: int,
) -> tuple[float, float] | None:
    """A sky offset, in detector pixels, through the solved CD matrix.

    The CD matrix is used rather than ``rotation_angle_degs`` because rotation
    alone cannot say whether the field is mirrored, and a wrong parity flips the
    sign of the RA component while leaving its magnitude right.

    Two unit conversions are load-bearing:

    - ``CD`` maps pixels to *standard coordinates*, so the RA offset must carry
      its ``cos(dec)`` factor. ``solve_and_correct`` deliberately omits that
      factor because the mount's own offset command wants plain RA; here it is
      required.
    - ``CD`` is per DOWNSAMPLED pixel (the backend bins by ``downsample_factor``
      before solving), so the result is scaled back up to detector pixels. The
      factor is passed in rather than imported so this arithmetic can be exercised
      without dragging in a solver backend.
    """
    cd = (solution.cd1_1, solution.cd1_2, solution.cd2_1, solution.cd2_2)
    if any(c is None for c in cd):
        return None
    cd1_1, cd1_2, cd2_1, cd2_2 = (float(c) for c in cd)  # type: ignore[arg-type]

    det = cd1_1 * cd2_2 - cd1_2 * cd2_1
    if det == 0.0:
        return None

    # standard coordinates, degrees
    xi = (d_ra_arcsec / 3600.0) * math.cos(math.radians(dec_deg))
    eta = d_dec_arcsec / 3600.0

    dx_ds = (cd2_2 * xi - cd1_2 * eta) / det
    dy_ds = (-cd2_1 * xi + cd1_1 * eta) / det
    return dx_ds * downsample_factor, dy_ds * downsample_factor


def _await_confirmation(connector, conf: LockNudgeConfig) -> float | None:
    """Wait for `confirm_frames` guide frames under `confirm_px`; return the last distance.

    `set_lock_position` starts no settling of its own, so there is no SettleDone
    to wait on -- PHD2 skips the state machine's settle bookkeeping entirely for a
    lock move. Counting frames here is the substitute, and it is deliberately
    cheap: the nudge is about a pixel against a residual several times that.
    """
    if conf.confirm_frames <= 0:
        return None

    deadline = time.time() + conf.confirm_timeout
    seen = 0
    last = None
    while time.time() < deadline:
        time.sleep(1)
        distance = connector.avg_dist
        if distance is None:
            continue
        last = float(distance)
        seen = seen + 1 if last <= conf.confirm_px else 0
        if seen >= conf.confirm_frames:
            return last
    logger.warning(f"{function_name()}: confirmation timed out after {conf.confirm_timeout}s, last distance={last}")
    return last


def nudge_lock_to_target(unit, connector, target: Coord, conf: LockNudgeConfig) -> NudgeOutcome:
    """Move the lock position so the fiber sits on `target`. Called only before the mirror insert."""
    op = function_name()

    if not conf.enabled:
        return NudgeOutcome(applied=False, reason="disabled")

    lock_before = connector.get_lock_position()
    if lock_before is None:
        return NudgeOutcome(applied=False, reason="PHD2 holds no lock position")

    image_path = None
    try:
        image_path = connector.save_image()
        solver_backend = unit.solver._backend
        result = solver_backend.solve(
            unit=unit,
            phase="spec",
            full_frame_input_image_path=image_path,
            target=target,
        )
    except Exception as ex:
        logger.error(f"{op}: could not solve the guide frame: {ex!r}")
        return NudgeOutcome(applied=False, reason=f"solve raised: {ex!r}")
    finally:
        # save_image() hands the caller a file it owns
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except OSError as ex:
                logger.debug(f"{op}: could not remove {image_path}: {ex!r}")

    if not result or not result.succeeded or result.solution is None:
        return NudgeOutcome(applied=False, reason="guide frame did not solve")

    solution = result.solution

    # Same wrap-safe form solve_and_correct uses, so the two agree on what "off target" means.
    d_ra_deg = (target.ra.deg - solution.ra_hours * 15) % 360
    if d_ra_deg > 180:
        d_ra_deg -= 360
    d_ra_arcsec = d_ra_deg * 3600
    d_dec_arcsec = target.dec.arcsecond - Angle(solution.dec_rads, unit="rad").arcsecond

    from solvers.mastrometry import DOWNSAMPLE_FACTOR

    offset = sky_offset_to_detector_pixels(solution, d_ra_arcsec, d_dec_arcsec, solution.dec_degs, DOWNSAMPLE_FACTOR)
    if offset is None:
        return NudgeOutcome(applied=False, reason="solution carries no usable CD matrix")

    # A star sits at sky = ref + CD * (pixel - refpix). Moving the lock by +D drives the
    # star to lock+D, i.e. the field shifts by +D in the image, which is a pointing change
    # of -CD*D. The pointing change wanted is +(target - solved), so D = -CD^-1 * (target
    # - solved): the lock moves AGAINST the sky offset.
    #
    # The sign is the one thing here that cannot be settled by reading: the bench check is
    # that `residual_px` after the nudge is smaller than `offset_px` before it. A flipped
    # sign doubles it instead, which the telemetry below makes obvious on the first run.
    dx, dy = -offset[0], -offset[1]
    magnitude_px = math.hypot(dx, dy)
    magnitude_arcsec = math.hypot(d_ra_arcsec * math.cos(math.radians(solution.dec_degs)), d_dec_arcsec)

    telemetry = {
        "offset_arcsec": magnitude_arcsec,
        "d_ra_arcsec": d_ra_arcsec,
        "d_dec_arcsec": d_dec_arcsec,
        "offset_px": (dx, dy),
        "lock_before": lock_before,
        "matched_stars": solution.matched_stars,
        "limit_frame_in_force": repr(connector.limit_frame_in_force),
    }

    if magnitude_px > conf.max_offset_px:
        logger.warning(
            f"{op}: refusing a {magnitude_px:.1f} px nudge (max {conf.max_offset_px} px); "
            f"a residual this large is not a pointing trim -- re-acquire instead"
        )
        return NudgeOutcome(
            applied=False,
            reason=f"offset {magnitude_px:.1f} px exceeds max_offset_px {conf.max_offset_px}",
            offset_arcsec=magnitude_arcsec,
            offset_px=(dx, dy),
            lock_before=lock_before,
            telemetry=telemetry,
        )

    lock_after = (lock_before[0] + dx, lock_before[1] + dy)
    connector.set_lock_position(lock_after[0], lock_after[1])
    logger.info(
        f'{op}: nudged the lock by ({dx:+.2f}, {dy:+.2f}) px ({magnitude_arcsec:.2f}") from {lock_before} to {lock_after}'
    )

    residual = _await_confirmation(connector, conf)
    telemetry["lock_after"] = lock_after
    telemetry["residual_px"] = residual
    return NudgeOutcome(
        applied=True,
        reason="nudged",
        offset_arcsec=magnitude_arcsec,
        offset_px=(dx, dy),
        lock_before=lock_before,
        lock_after=lock_after,
        residual_px=residual,
        telemetry=telemetry,
    )
