"""Re-reference the guide lock to the target, in the one window where that is possible.

Acquisition converges to well inside a pixel -- measured residuals over the
2026-09-14 run were 0.09-0.26", or 0.3-1.0 px at 0.27"/px. The open loop between
its last correction and ``SettleDone`` then gives that precision back: the mount
tracks unguided for 9.9 s when PHD2's chosen star is usable and 56.6 s when it is
not, and whatever the guide loop finally holds is wherever the field drifted to.

The nudge closes that gap. Once guiding has settled it solves the guide frame
PHD2 is already taking, measures how far the fiber now sits from the target, and
puts the lock that far from where the guide star was in that frame. The lock moves
by the guide error at that moment plus the correction -- a few pixels against a
15 px search region -- and never by more than ``max_offset_px``.

One solve places the fiber to 0.56" radial (1 sigma, 280 frames on 2026-10-20), about
the size of the error being corrected, so the nudge solves ``solve_frames`` distinct
guide frames, moves the lock to the mean of their estimates, and refuses when they
scatter by more than ``max_spread_arcsec``.

It runs once per acquisition handover, after settle: on the mirror-first handover with
the fold mirror already in, on the bracketed one before the mirror moves. With the
mirror in, a mirror-induced shift of the image cannot be told from a real pointing
error -- and if the shift is optical, correcting it with the mount drives the
target off the fiber. That is why ``phd2.lock_nudge`` is off until the insertion
has been measured to leave the image in place.

One coordinate note, because the alternative is a trap. PHD2 validates a lock
position against the image it is currently delivering -- the limit frame when one
is in force -- while a plate solve yields full-sensor pixels. Sending a solved
*absolute* position would land one crop origin away (520 px, about 2.3 arcmin, on
the derived frame) and would *succeed*, because that is a valid image coordinate:
the same full-sensor-versus-cropped confusion as MAST_unit#234, in a place where
it moves the mount.

This nudge never converts a solved position. It computes an offset, which is the
same in both frames because they differ only by origin, and adds it to the guide
star's position measured in the very frame that was solved. PHD2 saves the frame
it delivers, so that position is already in PHD2's frame, whatever that is. There
is deliberately no sensor-to-image conversion here to get wrong.

The offset goes on the star, not on the lock. The solve says where the fiber sat
when the frame was taken, and at that moment the star sat wherever the guide error
had put it -- 0.7-3.2 px off the lock on 2026-10-20. Added to the lock instead,
that error would stay in the correction.

See MAST_unit#19.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits

from common.config.phd2 import LockNudgeConfig
from common.interfaces.solving import (
    ARCSEC_PER_DEGREE,
    SolvingSolution,
    offset_on_sky_arcsec,
    target_offset_arcsec,
)
from common.mast_logging import get_logger
from common.models.statuses import LockNudgeOutcome
from common.utils import Coord, function_name
from pointing_check import place_guide_frame, unplaced_frame_reason

logger = get_logger(__name__)

#: How often the confirmation loop looks for a new guide step. A guide frame arrives
#: every 4.6-9.1 s depending on the limit frame, so this only decides how promptly a
#: new one is noticed, not how long it takes to arrive.
CONFIRM_POLL_SECONDS: float = 1.0
#: How often frame collection looks for a new guide step before asking for the frame.
#: Asked sooner, `save_image` hands back the frame already used -- 94 MB at full size.
FRAME_POLL_SECONDS: float = 0.5

#: Half-width of the box the guide star is looked for in, around the lock: PHD2's
#: default search region, which is as far as the star can be from the lock and
#: still be the one PHD2 is guiding on.
STAR_SEARCH_HALF_WIDTH_PX: int = 15
#: A star is a peak this many sky-noise sigmas above the box's median.
STAR_MIN_SNR: float = 10.0
#: Pixels below this fraction of the peak are left out of the centroid, so the
#: sky's noise does not pull it toward the box's center.
STAR_CORE_FRACTION: float = 0.2
#: The median absolute deviation of Gaussian noise times this is its sigma.
MAD_TO_SIGMA: float = 1.4826


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
    spread_arcsec: float | None = None
    frames_used: int | None = None
    telemetry: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"NudgeOutcome(applied={self.applied}, reason={self.reason!r}, "
            f"offset_arcsec={self.offset_arcsec}, offset_px={self.offset_px}, "
            f"spread_arcsec={self.spread_arcsec}, frames_used={self.frames_used}, "
            f"lock_before={self.lock_before}, lock_after={self.lock_after}, "
            f"residual_px={self.residual_px})"
        )

    def to_status(self) -> LockNudgeOutcome:
        """The part an operator reads in `status.acquisition.nudge`; the telemetry stays in the log."""
        return LockNudgeOutcome(
            applied=self.applied,
            reason=self.reason,
            offset_arcsec=self.offset_arcsec,
            offset_px=self.offset_px,
            spread_arcsec=self.spread_arcsec,
            frames_used=self.frames_used,
            residual_px=self.residual_px,
        )


class NudgeRefusedError(Exception):
    """The nudge cannot go on with this guide session; the lock stays where PHD2 put it."""


class FrameUnusableError(Exception):
    """One guide frame gives no estimate; the collection moves on to the next."""


@dataclass(frozen=True)
class FrameEstimate:
    """One guide frame's say on where the lock should go, in that frame's pixels."""

    lock: tuple[float, float]
    star: tuple[float, float]
    offset_px: tuple[float, float]
    d_ra_arcsec: float
    d_dec_arcsec: float
    dec_degs: float
    arcsec_per_px: float
    matched_stars: int | None


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
    xi = (d_ra_arcsec / ARCSEC_PER_DEGREE) * math.cos(math.radians(dec_deg))
    eta = d_dec_arcsec / ARCSEC_PER_DEGREE

    dx_ds = (cd2_2 * xi - cd1_2 * eta) / det
    dy_ds = (-cd2_1 * xi + cd1_1 * eta) / det
    return dx_ds * downsample_factor, dy_ds * downsample_factor


def detector_pixel_scale_arcsec(solution: SolvingSolution, downsample_factor: int) -> float | None:
    """Arcseconds per detector pixel, from the solved CD matrix (per DOWNSAMPLED pixel)."""
    if solution.cd1_1 is None or solution.cd1_2 is None or solution.cd2_1 is None or solution.cd2_2 is None:
        return None
    det = solution.cd1_1 * solution.cd2_2 - solution.cd1_2 * solution.cd2_1
    return math.sqrt(abs(det)) * ARCSEC_PER_DEGREE / downsample_factor


def star_position(image_path: str, near: tuple[float, float]) -> tuple[float, float] | None:
    """The centroid of the star within PHD2's search region around `near`, in the saved
    frame's pixels; None if there is no star there."""
    data = np.asarray(fits.getdata(image_path), dtype=float)
    x0, y0 = round(near[0]), round(near[1])
    h = STAR_SEARCH_HALF_WIDTH_PX
    rows = slice(max(y0 - h, 0), min(y0 + h + 1, data.shape[0]))
    cols = slice(max(x0 - h, 0), min(x0 + h + 1, data.shape[1]))
    box = data[rows, cols]
    if box.size == 0:
        return None

    sky = float(np.median(box))
    noise = MAD_TO_SIGMA * float(np.median(np.abs(box - sky)))
    signal = box - sky
    peak = float(signal.max())
    if peak <= STAR_MIN_SNR * noise:
        return None

    weights = np.where(signal >= STAR_CORE_FRACTION * peak, signal, 0.0)
    ys, xs = np.mgrid[rows, cols]
    total = float(weights.sum())
    return float((weights * xs).sum()) / total, float((weights * ys).sum()) / total


def _await_confirmation(connector, conf: LockNudgeConfig) -> float | None:
    """Wait for `confirm_frames` guide steps under `confirm_px`; return the last step's distance.

    `set_lock_position` starts no settling of its own, so there is no SettleDone
    to wait on -- PHD2 skips the state machine's settle bookkeeping entirely for a
    lock move. Counting guide steps here is the substitute. Each step counts once.
    The first one after the move does not count: it is from the frame that was
    exposing during the move, the unmoved star against the new lock. So the residual
    is measured on the new lock. (Counting polls of the smoothed `AvgDist` instead
    confirmed about 2 s after the move, on the old lock's distance.)
    """
    if conf.confirm_frames <= 0:
        return None

    deadline = time.monotonic() + conf.confirm_timeout
    step, _ = connector.latest_guide_step
    in_flight = step + 1
    seen = 0
    last = None
    while time.monotonic() < deadline:
        time.sleep(CONFIRM_POLL_SECONDS)
        latest, distance = connector.latest_guide_step
        if latest == step:
            continue
        step = latest
        if step <= in_flight:
            continue
        last = distance
        seen = seen + 1 if last <= conf.confirm_px else 0
        if seen >= conf.confirm_frames:
            return last
    logger.warning(f"{function_name()}: confirmation timed out after {conf.confirm_timeout}s, last distance={last}")
    return last


def _estimate_from_frame(unit, connector, image_path: str, near: tuple[float, float], target: Coord) -> FrameEstimate:
    """Where the lock should go according to one saved guide frame, in its own pixels.

    The frame is solved in its own coordinates: a limit-frame crop carries its origin,
    found as the pointing check finds it, or mastrometry places its sensor-pixel ROI on
    the crop and raises.
    """
    header = fits.getheader(image_path)
    width, height = header["NAXIS1"], header["NAXIS2"]
    placement = place_guide_frame(connector, width, height)
    if placement is None:
        raise NudgeRefusedError(unplaced_frame_reason(width, height))

    star = star_position(image_path, near)
    if star is None:
        raise FrameUnusableError("no star near the lock in the guide frame")

    try:
        result = unit.solver._backend.solve(
            unit=unit,
            phase="spec",
            full_frame_input_image_path=image_path,
            keep_artifacts=False,
            target=target,
            **placement.solve_kwargs(),
        )
    except Exception as ex:
        logger.exception(f"{function_name()}: could not solve the guide frame")
        raise FrameUnusableError(f"solve raised: {ex!r}") from ex
    if not result or not result.succeeded or result.solution is None:
        raise FrameUnusableError("guide frame did not solve")

    solution = result.solution
    # The same helper solve_and_correct uses, so the two cannot disagree about what
    # "off target" means.
    d_ra_arcsec, d_dec_arcsec = target_offset_arcsec(target, solution)

    from solvers.mastrometry import DOWNSAMPLE_FACTOR

    offset = sky_offset_to_detector_pixels(solution, d_ra_arcsec, d_dec_arcsec, solution.dec_degs, DOWNSAMPLE_FACTOR)
    arcsec_per_px = detector_pixel_scale_arcsec(solution, DOWNSAMPLE_FACTOR)
    if offset is None or arcsec_per_px is None:
        raise FrameUnusableError("solution carries no usable CD matrix")

    # A star sits at sky = ref + CD * (pixel - refpix). A lock at star+D drives the star
    # there, i.e. the field shifts by +D in the image from where it was solved, which is a
    # pointing change of -CD*D. The pointing change wanted is +(target - solved), so
    # D = -CD^-1 * (target - solved): the lock moves AGAINST the sky offset.
    #
    # The sign is the one thing here that cannot be settled by reading: the bench check is
    # that `residual_px` after the nudge is smaller than `offset_px` before it. A flipped
    # sign doubles it instead, which the telemetry makes obvious on the first run.
    dx, dy = -offset[0], -offset[1]
    return FrameEstimate(
        lock=(star[0] + dx, star[1] + dy),
        star=star,
        offset_px=(dx, dy),
        d_ra_arcsec=d_ra_arcsec,
        d_dec_arcsec=d_dec_arcsec,
        dec_degs=solution.dec_degs,
        arcsec_per_px=arcsec_per_px,
        matched_stars=solution.matched_stars,
    )


def _collect_estimates(
    unit, connector, target: Coord, conf: LockNudgeConfig, near: tuple[float, float], is_current: Callable[[], bool]
) -> list[FrameEstimate]:
    """`solve_frames` estimates from distinct guide frames, or NudgeRefusedError.

    While guiding, `save_image` returns the most recent guide frame, so it is asked once
    per new guide step, and a frame whose `DATE-OBS` was already used does not count.
    The guide session is checked between frames: a session stopped or replaced mid-way
    -- a manual one, say -- is never nudged.
    """
    op = function_name()
    deadline = time.monotonic() + conf.frames_timeout
    estimates: list[FrameEstimate] = []
    used: set[str] = set()
    step: int | None = None
    skipped = ""
    while len(estimates) < conf.solve_frames:
        if not is_current():
            raise NudgeRefusedError("the guide session ended while guide frames were being collected")
        if time.monotonic() >= deadline:
            raise NudgeRefusedError(
                f"only {len(estimates)} of {conf.solve_frames} guide frames gave an estimate "
                f"in {conf.frames_timeout:.0f} s" + (f" (last skipped: {skipped})" if skipped else "")
            )
        latest, _ = connector.latest_guide_step
        if latest == step:
            time.sleep(FRAME_POLL_SECONDS)
            continue
        step = latest

        image_path = connector.save_image()
        try:
            taken = fits.getheader(image_path)["DATE-OBS"]
            if taken in used:
                continue
            used.add(taken)
            estimates.append(_estimate_from_frame(unit, connector, image_path, near, target))
        except FrameUnusableError as ex:
            skipped = str(ex)
            logger.info(f"{op}: skipping the guide frame of {taken}: {ex}")
        finally:
            # save_image() hands the caller a file it owns
            try:
                os.remove(image_path)
            except OSError as ex:
                logger.debug(f"{op}: could not remove {image_path}: {ex!r}")
    return estimates


def nudge_lock_to_target(
    unit, connector, target: Coord, conf: LockNudgeConfig, is_current: Callable[[], bool]
) -> NudgeOutcome:
    """Move the lock position so the fiber sits on `target`, once guiding has settled.

    `is_current` says whether the guide session the nudge was started for is still the
    one running.
    """
    op = function_name()

    if not conf.enabled:
        return NudgeOutcome(applied=False, reason="disabled")

    lock_before = connector.get_lock_position()
    if lock_before is None:
        return NudgeOutcome(applied=False, reason="PHD2 holds no lock position")

    try:
        estimates = _collect_estimates(unit, connector, target, conf, lock_before, is_current)
    except NudgeRefusedError as ex:
        logger.warning(f"{op}: not nudging: {ex}")
        return NudgeOutcome(applied=False, reason=str(ex), lock_before=lock_before)

    count = len(estimates)
    lock_after = (sum(e.lock[0] for e in estimates) / count, sum(e.lock[1] for e in estimates) / count)
    offset_px = (sum(e.offset_px[0] for e in estimates) / count, sum(e.offset_px[1] for e in estimates) / count)
    d_ra_arcsec = sum(e.d_ra_arcsec for e in estimates) / count
    d_dec_arcsec = sum(e.d_dec_arcsec for e in estimates) / count
    dec_degs = sum(e.dec_degs for e in estimates) / count
    arcsec_per_px = sum(e.arcsec_per_px for e in estimates) / count
    spread_px = math.sqrt(
        sum((e.lock[0] - lock_after[0]) ** 2 + (e.lock[1] - lock_after[1]) ** 2 for e in estimates) / count
    )
    spread_arcsec = spread_px * arcsec_per_px
    move_px = math.hypot(lock_after[0] - lock_before[0], lock_after[1] - lock_before[1])
    magnitude_arcsec = offset_on_sky_arcsec(d_ra_arcsec, d_dec_arcsec, dec_degs)

    telemetry = {
        "offset_arcsec": magnitude_arcsec,
        "d_ra_arcsec": d_ra_arcsec,
        "d_dec_arcsec": d_dec_arcsec,
        "offset_px": offset_px,
        "spread_arcsec": spread_arcsec,
        "lock_before": lock_before,
        "estimates": [(e.lock, e.star, e.d_ra_arcsec, e.d_dec_arcsec, e.matched_stars) for e in estimates],
        "limit_frame": repr(connector.get_limit_frame()),
    }
    measured = {
        "offset_arcsec": magnitude_arcsec,
        "offset_px": offset_px,
        "spread_arcsec": spread_arcsec,
        "frames_used": count,
        "lock_before": lock_before,
        "telemetry": telemetry,
    }

    if spread_arcsec > conf.max_spread_arcsec:
        logger.warning(f'{op}: the {count} estimates scatter by {spread_arcsec:.2f}"; leaving the lock alone')
        return NudgeOutcome(
            applied=False,
            reason=f'spread {spread_arcsec:.2f}" exceeds max_spread_arcsec {conf.max_spread_arcsec}',
            **measured,
        )

    # Bounds what the lock actually does, star term included: a "star" that is a hot
    # pixel or a cosmic ray in a frame where the guide star dropped out sits anywhere in
    # the search box.
    if move_px > conf.max_offset_px:
        logger.warning(
            f"{op}: refusing to move the lock {move_px:.1f} px (max {conf.max_offset_px} px); "
            f"a move this large is not a pointing trim -- re-acquire instead"
        )
        return NudgeOutcome(
            applied=False, reason=f"lock move {move_px:.1f} px exceeds max_offset_px {conf.max_offset_px}", **measured
        )

    # The last solve can take long enough for the session to end under it.
    if not is_current():
        return NudgeOutcome(applied=False, reason="the guide session ended before the lock was moved", **measured)

    connector.set_lock_position(lock_after[0], lock_after[1])
    logger.info(
        f"{op}: moved the lock from {lock_before} to {lock_after}, the mean of {count} guide frames "
        f'(offset ({offset_px[0]:+.2f}, {offset_px[1]:+.2f}) px, {magnitude_arcsec:.2f}"; spread {spread_arcsec:.2f}")'
    )

    residual = _await_confirmation(connector, conf)
    telemetry["lock_after"] = lock_after
    telemetry["residual_px"] = residual
    return NudgeOutcome(applied=True, reason="nudged", lock_after=lock_after, residual_px=residual, **measured)
