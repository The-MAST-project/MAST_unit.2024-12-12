"""Focus calibration -- the live HFD drive behind ``POST /calibrate/focuser``.

The *analysis* is pure and lives in :mod:`calibration.analysis`
(``hfd.assess_focus_regime``, ``donut.plan_donut_jump``,
``vcurve.analyze_focus_samples``); this module drives real hardware through the
sweep that feeds it, and persists the result.

Flow, in the order the physics dictates::

    stage.home() + slew/track      hardware the phase MAKES HAPPEN on entry
        |
    Phase 0  one frame at the seed -> "near" | "far" | "empty"
        |            |                |
        |          "far"           "empty"
        |            |                |
        |     donut slope jump   coarse stepping
        |            |                |
        +------------+----------------+
        |
    Phase 1  N-point V-curve about the seed, analysed JOINTLY over a
             consistent star set, restricted to the low-coma disk
        |
    not bracketed? -> extrapolate the HFD arm, re-centre, retry (max_tries)
        |
    persist calibration.products.focuser, move to best focus

**What this deliberately does NOT do:** touch
``focuser.known_as_good_position``.  That is the operational value the ps3cli
path owns; promoting a calibrated focus into it is a separate, later decision.
A calibration run is therefore side-effect-free with respect to normal observing.

Design reference: mast-claude-config ``plans/self-contained-hfd-autofocus.md``
and ``plans/calibration_orchestration.md``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

import numpy as np
from astropy.io import fits

from calibration.analysis.hfd import assess_focus_regime
from calibration.analysis.models import HFDAutofocusStatus
from calibration.analysis.vcurve import analyze_donut_samples, analyze_focus_samples
from calibration.logging_context import init_calibration_log
from calibration.phases.artifacts import move_to_shared, plot_vcurve, save_status
from calibration.phases.slewing import slew_and_settle
from calibration.phases.temperature import get_ambient_temperature, get_mirror_temperature
from common.activities import FocuserActivities, StageActivities, UnitActivities
from common.config import Config
from common.config.calibration import (
    CalibrationConfig,
    FocuserCalibration,
    FocuserCalibrationSettings,
)
from common.interfaces.imager import ImagerSettings
from common.utils import time_stamp

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)

#: Cap on a single focuser move.  Generous -- the widest sweep moves seen on sky
#: (~1000-4000 ticks) complete well inside 30s -- so this only fires on a genuine
#: stall, never on a slow-but-working move.
FOCUSER_MOVE_TIMEOUT_SECONDS = 120.0


class FocuserMoveError(Exception):
    """The focuser did not reach a commanded position (stalled, or aborted)."""


class FocuserCalibrator:
    """Drives the HFD focus calibration loop on a live unit."""

    def __init__(self, unit: Unit):
        self.unit = unit
        self.errors: list[str] = []
        self.regime: str | None = None
        self.tries_used: int = 0
        #: Frames exposed this run -- prefixes the filename to keep it unique.
        self._frame_seq: int = 0

    # ------------------------------------------------------------------ entry
    def calibrate(  # noqa: C901
        self,
        *,
        settings: FocuserCalibrationSettings | None = None,
        ra_j2000_hours: float | None = None,
        dec_j2000_degs: float | None = None,
        folder: str | None = None,
    ) -> HFDAutofocusStatus | None:
        """Run the full triage -> sweep -> fit -> persist loop.

        Returns the :class:`HFDAutofocusStatus` (check
        ``.analysis_result.has_solution``), or ``None`` if a precondition failed
        before any sweep -- see ``self.errors``.
        """
        op = "FocuserCalibrator.calibrate"
        self.errors = []
        st = settings or FocuserCalibrationSettings()
        unit = self.unit
        conf, imager, focuser = unit.unit_conf, unit.imager, unit.focuser
        pw, stage = unit.pw, unit.stage

        # pw / mount / stage are NOT required: without them we simply skip the
        # slew, the tracking check and the temperature read, and calibrate at
        # the current pointing.  Only the three things the sweep cannot do
        # without are mandatory.
        if conf is None or imager is None or focuser is None:
            return self._fail(f"{op}: unit not fully initialised (conf/imager/focuser)")
        if st.images % 2 != 1:
            return self._fail(f"{op}: images={st.images} must be odd")

        memory = imager.can_image_to_memory and imager.can_send_image_ready_event
        if not memory and folder is None:
            return self._fail(f"{op}: file-only imager needs a 'folder' for the frames")

        # TODO(safety): no is_safe interlock yet.  A weather/safety flip during a
        # run will neither abort nor stow -- the loop only honours the operator
        # abort via the Calibrating* activity flags.  Add an is_safe check to
        # _still_calibrating() (and a stow on transition) before unattended use.

        # Timestamp is taken HERE, at the call site, so the record carries
        # (temperature, read_time) and a stale reading can be rejected later.
        temperature = get_mirror_temperature(pw) if pw is not None else None
        temperature_read_time = time_stamp()
        if pw is not None:
            logger.debug(f"{op}: mirror={temperature} ambient={get_ambient_temperature(pw)} read_at={temperature_read_time}")
        else:
            logger.debug(f"{op}: no PWI4 -- temperature unavailable (recorded as None)")

        # Bound BEFORE the try so the finally can always read it: several exit
        # paths inside (failed slew, aborted acquisition) return before the
        # sweep ever assigns a status, and the artifacts must still be written.
        status: HFDAutofocusStatus | None = None
        series = imager.start_exposure_series(purpose="focus-calibration")
        try:
            # --- hardware the phase makes happen -----------------------------
            if stage is not None:
                logger.debug(f"{op}: stage.home() -- retract the mirror, clear the field")
                stage.home()
                self._wait_stage()
            if not self._goto(ra_j2000_hours, dec_j2000_degs):
                return self._abort(f"{op}: aborted during slew")

            seed = self._seed_position(conf)
            if seed is None:
                return self._fail(f"{op}: no seed position -- no calibration product and the focuser reports no position")
            logger.info(f"{op}: seed position {seed}, regime triage next")

            # --- Phase 0: triage, and acquisition if we are far out ----------
            seed = self._acquire_near_focus(seed, st, folder)
            if seed is None:
                return self._abort(f"{op}: could not reach a focusable regime ({self.regime})")

            # --- Phase 1: V-curve, re-centring while tries remain ------------
            status = None
            solved = False  # a solution that also PASSED the plausibility gate
            for attempt in range(st.max_tries):
                self.tries_used = attempt + 1
                if not self._still_calibrating():
                    return self._abort(f"{op}: stopped before sweep {self.tries_used}")
                logger.info(
                    f"{op}: sweep {self.tries_used}/{st.max_tries} centred on {seed} ({st.images} x {st.spacing} ticks)"
                )
                samples = self._sweep(seed, st, folder)
                if samples is None:
                    return self._abort(f"{op}: stopped during sweep {self.tries_used}")
                if len(samples) < 3:
                    self._log_error(f"{op}: only {len(samples)} frames acquired")
                    break

                center, radius = self._low_coma_zone(conf, samples[0][1], st)
                status = analyze_focus_samples(
                    samples,
                    tolerance_frac=st.tolerance_frac,
                    center=center,
                    radius=radius,
                )
                result = status.analysis_result
                logger.info(f"{op}: {status.message}")
                if result is not None and result.has_solution:
                    dmin = result.best_focus_star_diameter
                    if dmin is not None and dmin > st.max_best_hfd_px:
                        # A sweep over donuts can produce a spurious interior
                        # minimum that passes the bracketing gate.  A vertex this
                        # broad is not focus -- keep acquiring rather than
                        # persist a confident wrong answer.
                        self._log_error(
                            f"{op}: rejecting implausible solution at "
                            f"{result.best_focus_position:.0f} -- Dmin={dmin:.1f}px "
                            f"exceeds max_best_hfd_px={st.max_best_hfd_px}"
                        )
                        # Keep `status` (its samples still say which way focus
                        # lies) but do not mark it solved -- fall through to
                        # re-centring, and never persist this vertex.
                    else:
                        solved = True
                        break

                if attempt == st.max_tries - 1:
                    break

                # Too few usable samples means we are not merely off-centre --
                # the field has gone (stars smeared below detection, or we
                # started further out than the sweep can see).  Extrapolating
                # from one point is meaningless, so go back to Phase-0
                # acquisition and re-acquire the regime instead of giving up.
                n_valid = len([s for s in (result.focus_samples or []) if s.is_valid]) if result else 0
                if n_valid < 2:
                    logger.info(f"{op}: only {n_valid} valid sample(s) -- re-running acquisition")
                    reacquired = self._acquire_near_focus(seed, st, folder)
                    if reacquired is None:
                        break
                    seed = reacquired
                    continue

                # How to move next depends on WHY this sweep failed, and the two
                # cases need opposite treatment:
                #
                #  * a vertex was found but rejected as implausible -- we are
                #    near focus and merely imprecise (e.g. the sweep still
                #    straddled donuts).  Re-sweep CENTRED ON THE VERTEX: a small,
                #    local refinement.  Extrapolating here is actively harmful,
                #    because a bracketed V has no monotonic arm to extrapolate
                #    and the fit throws the seed far away.
                #  * no vertex at all (not bracketed) -- the sweep is a monotonic
                #    arm pointing at focus, so extrapolate along it.
                if result is not None and result.has_solution:
                    new_seed = self._clamp(int(round(result.best_focus_position)), st)  # type: ignore[arg-type]
                    logger.debug(f"{op}: rejected vertex -- re-sweeping centred on it ({new_seed})")
                    if new_seed == seed:
                        self._log_error(f"{op}: vertex unchanged at {new_seed} and still implausible")
                        break
                else:
                    new_seed = self._recentre(status, seed, st)
                if new_seed is None:
                    break
                logger.debug(f"{op}: re-centring {seed} -> {new_seed} and retrying")
                seed = new_seed

            # --- persist -----------------------------------------------------
            # `solved` -- not merely has_solution -- so a vertex rejected by the
            # plausibility gate is never written to the DB.
            result = status.analysis_result if (status and solved) else None
            if result is None:
                self._log_error(f"{op}: no focus solution after {self.tries_used} sweep(s)")
                return status

            best = int(round(result.best_focus_position))  # type: ignore[arg-type]
            best = self._clamp(best, st)
            logger.info(
                f"{op}: best focus {best} (Dmin={result.best_focus_star_diameter:.2f}px, "
                f"tolerance={result.tolerance:.1f} ticks, "
                f"{result.n_consistent_stars} consistent stars)"
            )
            self._persist(result, best, temperature, temperature_read_time)
            self._move_focuser(best, st)
            return status
        finally:
            imager.end_exposure_series(series)
            # Artifacts on EVERY exit path -- success, failure, or abort.  A
            # failed run's frames are precisely the ones worth replaying (every
            # diagnosis of 2026-07-21 came from saved frames, not from logs), so
            # the plot, the result file and the move off the volatile RAM disk
            # must not be conditional on solving.
            final = status or HFDAutofocusStatus(message="run produced no analysis", errors=list(self.errors))
            plot_vcurve(folder, final.analysis_result)
            save_status(folder, final)
            move_to_shared(folder)

    # ------------------------------------------------------- Phase 0 / Phase 2
    def _acquire_near_focus(self, seed, st, folder) -> int | None:
        """Triage one frame at ``seed``; when far out, get back to near-focus.

        Returns a seed known to be in (or near) the point-source regime, or
        ``None`` if acquisition failed.
        """
        op = "_acquire_near_focus"
        self._move_focuser(seed, st)
        image = self._expose(st, folder, tag=f"FOCUS{seed:05d}_probe")
        if image is None:
            self._log_error(f"{op}: probe exposure failed")
            return None

        self.regime = assess_focus_regime(image, near_hfd_max=st.near_hfd_max_px)
        logger.info(f"{op}: Phase-0 verdict at {seed}: '{self.regime}'")
        if self.regime == "near":
            return seed

        if self.regime == "empty":
            seed = self._cold_start(seed, st, folder)
            if seed is None:
                return None

        # "far": donuts.  One differential move calibrates the diameter-vs-defocus
        # slope AND resolves the sign (inside vs outside focus look identical in
        # size alone), so the jump lands near focus in one move.
        return self._donut_jump(seed, st, folder)

    def _cold_start(self, seed, st, folder) -> int | None:
        """Step coarsely until *something* extracts, then hand over to the donut path."""
        op = "_cold_start"
        for i in range(1, st.coarse_max_steps + 1):
            # alternate out/in, growing -- focus may lie on either side
            delta = st.coarse_step_ticks * ((i + 1) // 2) * (1 if i % 2 else -1)
            probe = self._clamp(seed + delta, st)
            logger.debug(f"{op}: coarse step {i}/{st.coarse_max_steps} -> {probe}")
            self._move_focuser(probe, st)
            image = self._expose(st, folder, tag=f"FOCUS{probe:05d}_coarse")
            if image is None or not self._still_calibrating():
                return None
            regime = assess_focus_regime(image, near_hfd_max=st.near_hfd_max_px)
            if regime != "empty":
                logger.info(f"{op}: structure found at {probe} ('{regime}')")
                self.regime = regime
                return probe
        self._log_error(f"{op}: nothing extracted within {st.coarse_max_steps} coarse steps")
        return None

    def _donut_jump(self, seed, st, folder) -> int | None:
        """Two donut frames -> slope + sign -> one calibrated jump toward focus."""
        op = "_donut_jump"
        probe = self._clamp(seed + st.donut_probe_ticks, st)
        if probe == seed:
            probe = self._clamp(seed - st.donut_probe_ticks, st)
        samples = []
        for pos in (seed, probe):
            self._move_focuser(pos, st)
            image = self._expose(st, folder, tag=f"FOCUS{pos:05d}_donut")
            if image is None or not self._still_calibrating():
                return None
            samples.append((float(pos), image))

        jump = analyze_donut_samples(samples)
        if not jump.has_solution or jump.best_focus_estimate is None:
            # Not fatal: the sweep may still bracket from here, and the V-curve
            # has its own not-bracketed retry.  Say so rather than failing.
            self._log_error(f"{op}: no donut solution ({jump.message}); continuing from {seed}")
            return seed
        target = self._clamp(int(round(jump.best_focus_estimate)), st)
        logger.info(f"{op}: donut jump {seed} -> {target} ({jump.message})")
        return target

    # ------------------------------------------------------------- Phase 1
    def _sweep(self, seed, st, folder) -> list[tuple[float, object]] | None:
        """N exposures centred on ``seed``, always approached from below.

        Centred means a point sits ON the seed: ``first = seed - (N-1)/2 * spacing``.
        (Using N/2 instead would offset the whole sweep by half a step.)
        """
        first = self._clamp(seed - ((st.images - 1) // 2) * st.spacing, st)
        # Backlash: come up to the first position from below so every sweep is
        # traversed in one consistent direction.
        self._move_focuser(self._clamp(first - st.backlash_ticks, st), st)

        samples: list[tuple[float, object]] = []
        for i in range(st.images):
            if not self._still_calibrating():
                return None
            pos = self._clamp(first + i * st.spacing, st)
            self._move_focuser(pos, st)
            image = self._expose(st, folder, tag=f"FOCUS{pos:05d}")
            if image is None:
                self._log_error(f"_sweep: no image at {pos}")
                continue
            samples.append((float(pos), image))
        return samples

    def _recentre(self, status, seed, st) -> int | None:
        """Where to centre the next sweep when this one did not bracket focus.

        Simply re-centring on the lowest sample advances only half a sweep span
        per try, so a seed a few hundred ticks out exhausts ``max_tries`` before
        arriving.  Instead extrapolate: near focus HFD grows roughly linearly
        with defocus, so the swept arm points at the vertex.  Fit a line to the
        valid samples and follow it to its floor, undershooting so we land short
        of focus rather than past it -- landing short still brackets next sweep.

        Falls back to the lowest-HFD position when the arm is flat or the fit is
        unusable.
        """
        result = status.analysis_result if status else None
        if result is None:
            return None
        good = [
            s
            for s in (result.focus_samples or [])
            if s.is_valid and s.focus_position is not None and s.hfd_pixels is not None and np.isfinite(s.hfd_pixels)
        ]
        if len(good) < 2:
            self._log_error("_recentre: fewer than 2 valid samples to re-centre on")
            return None

        order = np.argsort([float(s.focus_position) for s in good])
        positions = np.array([float(good[i].focus_position) for i in order])
        hfds = np.array([float(good[i].hfd_pixels) for i in order])
        i_min = int(np.argmin(hfds))
        lowest = int(round(positions[i_min]))

        # Which way does focus lie?  The minimum sitting at an edge is what
        # "not bracketed" means, and the edge tells us the direction.  An
        # INTERIOR minimum with no usable fit is not an arm at all -- there is
        # nothing to extrapolate along, so just re-sweep around it.
        if i_min == 0:
            direction = -1
        elif i_min == len(positions) - 1:
            direction = +1
        else:
            logger.debug(f"_recentre: interior minimum at {lowest} but no fit -- re-sweeping there")
            new_seed = self._clamp(lowest, st)
            return None if new_seed == seed else new_seed

        span = float(positions[-1] - positions[0])
        target = None
        if len(good) >= 3 and span > 0:
            slope, intercept = np.polyfit(positions, hfds, 1)
            if abs(slope) > 1e-9:
                zero = -intercept / slope  # where the linear arm reaches HFD 0
                step = (zero - lowest) * (1.0 - st.recentre_undershoot_frac)
                step = float(np.clip(step, -st.max_recentre_ticks, st.max_recentre_ticks))
                candidate = int(round(lowest + step))
                # Only trust the extrapolation if it agrees with the direction
                # the data already tells us.  A nearly-flat or noisy arm can fit
                # a slope whose zero-crossing lies the WRONG WAY entirely, which
                # would fling the focuser away from focus.
                if np.sign(candidate - lowest) == direction:
                    target = candidate
                    logger.debug(f"_recentre: arm slope={slope:.4f}px/tick -> extrapolated {lowest} + {step:.0f} = {target}")
                else:
                    logger.debug(
                        f"_recentre: extrapolation to {candidate} disagrees with the "
                        f"downhill direction ({direction:+d}) -- ignoring it"
                    )

        if target is None:
            # Conservative fallback: step half a sweep span downhill, so the next
            # sweep overlaps this one and cannot leapfrog past focus.
            target = int(round(lowest + direction * span / 2.0))
            logger.debug(f"_recentre: stepping half a span downhill -> {target}")

        new_seed = self._clamp(target, st)
        return None if new_seed == seed else new_seed

    # ------------------------------------------------------- low-coma zone
    def _low_coma_zone(self, conf, image, st) -> tuple[tuple[float, float] | None, float | None]:
        """The disk the HFD metric restricts itself to, in THIS frame's pixels.

        Preference order:

        1. the calibrated optical centre + ``low_coma_radius``, when it is in the
           current mechanical epoch and was measured on this frame size;
        2. otherwise a geometric disk of ``fallback_disk_frac * min(nx,ny)/2``
           about the image centre.

        Both are returned as an explicit ``(center, radius)`` rather than via
        ``near_axis_frac``, because that parameter scales by the frame *diagonal*
        -- which for a full frame is ~1.8x larger than the design's
        ``min(nx,ny)/2`` disk and would readmit the coma-heavy margins.

        The stored calibration is in full-detector **bin-1** pixels, so both the
        centre and the radius are divided by the sweep's binning.
        """
        shape = self._frame_shape(image)
        cal = getattr(conf, "calibration", None)
        products = getattr(cal, "products", None) if cal else None
        oc = getattr(products, "optical_center", None) if products else None
        binning = int(st.binning)

        if oc is not None and oc.low_coma_radius:
            epoch = oc.mechanical_epoch
            bin1_shape = tuple(int(v) * binning for v in shape) if shape else tuple(oc.image_shape)
            if oc.matches(bin1_shape, epoch):
                center = (oc.center_x / binning, oc.center_y / binning)
                radius = oc.low_coma_radius / binning
                logger.debug(f"low-coma zone: calibrated centre {center}, radius {radius:.1f}px (bin {binning})")
                return center, radius
            logger.debug(
                f"low-coma zone: optical centre rejected -- "
                f"shape/epoch mismatch (stored {tuple(oc.image_shape)} epoch {epoch})"
            )

        if shape is None:
            # Should not happen -- but never silently fall back to the whole
            # frame, which would readmit the coma-heavy margins the metric
            # exists to exclude.  Say so loudly instead.
            self._log_error(
                "low-coma zone: could not determine frame shape -- "
                "HFD will use the WHOLE FRAME, including coma-heavy margins"
            )
            return None, None
        ny, nx = shape
        center = ((nx - 1) / 2.0, (ny - 1) / 2.0)
        radius = st.fallback_disk_frac * min(nx, ny) / 2.0
        logger.debug(
            f"low-coma zone: no optical centre -- geometric disk r={radius:.1f}px "
            f"({st.fallback_disk_frac} x min({nx},{ny})/2)"
        )
        return center, radius

    @staticmethod
    def _frame_shape(image) -> tuple[int, int] | None:
        """``(ny, nx)`` of a frame given either as an array or as a FITS path.

        The path case is not a corner case: PHD2 -- the imager on the units --
        is file-only (``can_image_to_memory`` is False), so every frame arrives
        as a path.  Reading the shape from the header (NAXIS1/NAXIS2) costs no
        pixel I/O, and without it the low-coma disk cannot be computed at all
        and the metric would silently fall back to the whole frame.
        """
        if image is None:
            return None
        if isinstance(image, (str, os.PathLike)):
            try:
                header = fits.getheader(image)
                return int(header["NAXIS2"]), int(header["NAXIS1"])
            except Exception as ex:
                logger.warning(f"could not read frame shape from '{image}': {ex}")
                return None
        try:
            ny, nx = np.asarray(image).shape[:2]
            return int(ny), int(nx)
        except Exception:
            return None

    # ---------------------------------------------------------------- persist
    def _persist(self, result, best: int, temperature, temperature_read_time):
        conf = self.unit.unit_conf
        assert conf is not None
        n_valid = len([s for s in (result.focus_samples or []) if s.is_valid])
        record = FocuserCalibration(
            best_position=best,
            tolerance=result.tolerance,
            best_hfd=result.best_focus_star_diameter,
            n_samples=n_valid,
            temperature=temperature,
            temperature_read_time=temperature_read_time,
            timestamp=time_stamp(),
        )
        if conf.calibration is None:
            conf.calibration = CalibrationConfig()
        conf.calibration.products.focuser = record
        try:
            Config().set_unit(unit_name=self.unit.hostname, unit_conf=conf)
            logger.info(
                f"saved calibration.products.focuser for '{self.unit.hostname}': "
                f"best_position={best} tolerance={record.tolerance} temp={temperature}"
            )
        except Exception as ex:
            self._log_error(f"could not save calibration.products.focuser for '{self.unit.hostname}': {ex}")

    # ---------------------------------------------------------------- helpers
    def _seed_position(self, conf) -> int | None:
        """Where to take the Phase-0 probe.

        ``calibration.products.focuser.best_position`` -> the focuser's current
        position.  Deliberately does NOT consult
        ``focuser.known_as_good_position``: that is the ps3cli path's
        operational value, and seeding from it would couple the two flows
        through the DB.  Calibration seeds only from its own product.

        On a never-calibrated unit the product is absent, so the run starts
        wherever the focuser currently sits -- which is exactly the cold-start
        case Phase 0 exists to handle, and the one to exercise on sky by parking
        the focuser well out of focus on purpose.
        """
        cal = getattr(conf, "calibration", None)
        products = getattr(cal, "products", None) if cal else None
        product = getattr(products, "focuser", None) if products else None
        if product is not None:
            logger.debug(f"seed from calibration product: {product.best_position}")
            return int(product.best_position)
        current = getattr(self.unit.focuser, "position", None)
        # 0 is a legitimate focuser position -- test for None, not falsiness.
        if current is None:
            return None
        logger.debug(f"no calibration product -- seeding from current focuser position {current}")
        return int(current)

    def _clamp(self, position: int, st) -> int:
        clamped = max(st.min_position, min(st.max_position, int(position)))
        if clamped != int(position):
            logger.debug(f"clamped focuser {int(position)} -> {clamped} [{st.min_position}, {st.max_position}]")
        return clamped

    def _move_focuser(self, position: int, st):
        """Command the focuser and wait for it to arrive.

        Raises :class:`FocuserMoveError` on a stall or an operator abort -- the
        wait is NOT open-ended, because `Focuser.ontimer` re-commands a move that
        went stationary short of target and never gives up.  A focuser that
        cannot reach the target therefore pins this loop forever: observed
        2026-07-21, commanded 27499 -> 26649, stalled after ~14 ticks, and the
        phase hung for 10+ minutes with `/calibrate/abort` powerless because this
        loop did not consult the flag either.

        Raising (rather than returning False) is deliberate: there are six call
        sites, and a bool would have to be checked at every one.  The exception
        unwinds through the `finally` in :meth:`calibrate`, so the exposure
        series and the activity flag are still released.
        """
        position = self._clamp(position, st)
        logger.debug(f"focuser -> {position}")
        self.unit.focuser.position = position
        time.sleep(0.5)  # let the activity register before polling
        deadline = time.monotonic() + FOCUSER_MOVE_TIMEOUT_SECONDS
        while self.unit.focuser.is_active(FocuserActivities.Moving):
            if not self._still_calibrating():
                raise FocuserMoveError(f"aborted while moving to {position}")
            if time.monotonic() >= deadline:
                # Report where it actually got to: that is the difference between
                # "the focuser is stalled" and "the target was unreachable", and
                # it is what had to be dug out of PWI4 by hand the first time.
                raise FocuserMoveError(
                    f"focuser did not reach {position} within "
                    f"{FOCUSER_MOVE_TIMEOUT_SECONDS:.0f}s -- stuck at "
                    f"{self.unit.focuser.position} (target={self.unit.focuser.target}). "
                    f"Focuser.ontimer keeps re-commanding a stalled move, so this "
                    f"never clears on its own; check the focuser in PWI4."
                )
            time.sleep(0.2)

    def _wait_stage(self):
        stage = self.unit.stage
        if stage is None:
            return
        time.sleep(0.5)
        while stage.is_active(StageActivities.Homing) or stage.is_moving:
            time.sleep(0.5)

    def _goto(self, ra, dec) -> bool:
        """Slew + track.  ``ra=None`` means the current LST (transit)."""
        pw, mount = self.unit.pw, self.unit.mount
        if pw is None and mount is None:
            logger.debug("no mount/PWI4 -- calibrating at the current pointing")
            return True
        if ra is None and pw is not None:
            try:
                ra = pw.status().site.lmst_hours  # transit: lowest airmass now
                logger.debug(f"ra not supplied -- using LST {ra}h (transit)")
            except Exception as ex:
                logger.warning(f"could not read LST: {ex}")
        if ra is not None and dec is not None and mount is not None:
            slew_and_settle(mount, ra, dec, "_goto")
            if not self._still_calibrating():
                return False
        if pw is not None:
            try:
                if not pw.status().mount.is_tracking:
                    logger.debug("starting mount tracking")
                    pw.mount_tracking_on()
            except Exception as ex:
                logger.warning(f"could not verify/start tracking: {ex}")
        return True

    def _expose(self, st, folder: str | None, tag: str):
        """One FULL-FRAME exposure; array (memory imager) or saved path (file imager)."""
        imager, conf = self.unit.imager, self.unit.unit_conf
        assert imager is not None and conf is not None
        memory = imager.can_image_to_memory and imager.can_send_image_ready_event
        # The sequence prefix is what makes the name unique.  A tag is only
        # position + kind, and a run legitimately exposes the SAME position more
        # than once -- re-acquisition after a thin sweep re-probes the same seed,
        # and a re-centred sweep re-visits positions of the previous one.  PHD2's
        # capture_single_frame REFUSES to overwrite ("destination file already
        # exists"), so identical names abort the run outright.  Numbering here,
        # at the one place every frame passes through, also puts the folder in
        # chronological order, which position-named files were not.
        self._frame_seq += 1
        image_path = (
            None if memory else os.path.join(folder, f"{self._frame_seq:03d}_{tag}.fits")  # type: ignore[arg-type]
        )
        settings = ImagerSettings(
            seconds=st.exposure,
            binning=st.binning,
            roi=imager.full_frame,  # full frame: the low-coma disk is defined on it
            gain=conf.acquisition.gain,
            image_path=image_path,
            save=image_path is not None,
        )
        try:
            imager.start_exposure(settings)
            if memory:
                imager.wait_for_image_ready()
                return imager.image_array
            imager.wait_for_image_saved()
            return image_path
        except Exception as ex:
            self._log_error(f"_expose({tag}): {ex}")
            return None

    def _still_calibrating(self) -> bool:
        """Cooperative abort -- the operator clearing the flag stops the loop.

        TODO(safety): also return False when the unit becomes unsafe, and stow.
        """
        return self.unit.is_active(UnitActivities.CalibratingFocus) or self.unit.is_active(UnitActivities.Calibrating)

    def _log_error(self, message: str):
        logger.error(message)
        self.errors.append(message)

    def _fail(self, message: str) -> None:
        self._log_error(message)
        return None

    def _abort(self, message: str) -> None:
        logger.warning(message)
        self.errors.append(message)
        return None
