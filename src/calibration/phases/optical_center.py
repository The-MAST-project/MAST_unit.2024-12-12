"""Optical-center calibration -- the live drive behind ``POST /calibrate/optical_center``.

The *analysis* is pure and lives in :mod:`calibration.analysis.optical_center`
(``extract_sources`` -> ``solve_optical_center`` -> ``fit_coma_slope``); this
module drives a live unit through the frames that feed it, and persists the
result.

Flow::

    focuser -> calibrated best focus     coma is only clean AT focus -- defocused,
        |                                a star is a pupil donut with no usable coma
    stage.home() + slew/track            retract the mirror (clean frames), point
        |
    N full-frame bin-1 exposures  ->  extract_sources() each
        |
    solve_optical_center(all N)          ONE pooled weighted fit -- per-frame
        |                                centres scatter ~10^2 px, so averaging N
        |                                centres inherits the scatter; pooling
        |                                lets every source constrain one solution
    fit_coma_slope(pooled)               e = k*r through the origin ->
        |                                low_coma_radius = coma_tolerance / k
    persist calibration.products.optical_center

**Two extractions feed two fits differently.**  The centre fit applies its
aggressive cuts (min_ellipticity, margin-stars-only) inside
``solve_optical_center`` -- it needs unambiguous orientations.  The slope fit
must NOT run on that cut sample: truncating at a minimum ellipticity removes
exactly the low-``e`` sources that anchor the origin end of ``e = k*r``, biasing
``k`` steep and ``low_coma_radius`` small.  So the slope pools the RAW
extractions, weighted by flux alone (the uncertainty of ``e`` scales ~1/SNR;
the flux*ellipticity weighting the centre fit uses is for *orientation*
uncertainty, which is not the observable here).

**Frame geometry is hardcoded full-frame bin 1** -- no knob.  The measured
centre *defines* the bin-1 pixel frame that ``OpticalCenterCalibration
.image_shape`` / ``.matches()`` guard and the stage phase later solves against;
acquiring at any other binning would silently poison every downstream geometric
calibration (see ``OpticalCenterCalibrationSettings``' docstring).

Design reference: mast-claude-config ``plans/calibration_orchestration.md`` and
the unit self-calibration design ("Optical center (coma elongation null)").
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

import numpy as np

from calibration.analysis.optical_center import (
    OpticalCenterResult,
    extract_sources,
    fit_coma_slope,
    solve_optical_center,
)
from calibration.logging_context import init_calibration_log
from calibration.phases.slewing import slew_and_settle
from common.activities import FocuserActivities, StageActivities, UnitActivities
from common.config import Config
from common.config.calibration import (
    CalibrationConfig,
    OpticalCenterCalibration,
    OpticalCenterCalibrationSettings,
)
from common.interfaces.imager import ImagerSettings
from common.utils import time_stamp

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)

#: Cap on waiting for the focuser to reach the calibrated best focus.  The
#: orchestrator commands the move; a stalled focuser must fail the phase in
#: minutes, not pin it -- Focuser.ontimer re-commands a stalled move forever.
FOCUSER_SETTLE_TIMEOUT_SECONDS = 120.0


class OpticalCenterCalibrator:
    """Drives the optical-center calibration loop on a live unit."""

    def __init__(self, unit: "Unit"):
        self.unit = unit
        self.errors: list[str] = []
        #: Frames exposed this run -- prefixes the filename to keep it unique.
        self._frame_seq: int = 0

    # ------------------------------------------------------------------ entry
    def calibrate(
        self,
        *,
        settings: OpticalCenterCalibrationSettings | None = None,
        ra_j2000_hours: float | None = None,
        dec_j2000_degs: float | None = None,
        folder: str | None = None,
    ) -> OpticalCenterResult | None:
        """Run the full acquire -> extract -> pool -> fit -> persist loop.

        Returns the pooled :class:`OpticalCenterResult`, or ``None`` when no
        centre could be determined -- see ``self.errors`` for why.  The centre
        can succeed while the coma slope does not: ``low_coma_radius`` is then
        persisted as ``None`` and autofocus falls back to its geometric disk,
        exactly what that field's nullability exists for.
        """
        op = "OpticalCenterCalibrator.calibrate"
        self.errors = []
        st = settings or OpticalCenterCalibrationSettings()
        unit = self.unit
        conf, imager, focuser = unit.unit_conf, unit.imager, unit.focuser
        pw, mount, stage = unit.pw, unit.mount, unit.stage

        if conf is None or imager is None or focuser is None:
            return self._fail(f"{op}: unit not fully initialised (conf/imager/focuser)")
        if st.number_of_frames < 1:
            return self._fail(f"{op}: number_of_frames={st.number_of_frames} must be >= 1")

        memory = imager.can_image_to_memory and imager.can_send_image_ready_event
        if not memory and folder is None:
            return self._fail(f"{op}: file-only imager needs a 'folder' for the frames")

        # min_frames_passing: None -> a majority of the requested frames.
        need = st.min_frames_passing or -(-st.number_of_frames // 2)  # ceil(n/2)

        # --- hardware the phase makes happen ---------------------------------
        # The focuser was commanded to the calibrated best focus by the
        # orchestrator; frames taken while it is still travelling carry smeared
        # PSFs whose elongation is motion, not coma -- so wait, bounded.
        if not self._wait_focuser_settled(op):
            return None

        if stage is not None:
            logger.debug(f"{op}: stage.home() -- retract the mirror, clean field")
            stage.home()
            self._wait_stage()
        if mount is not None and ra_j2000_hours is not None and dec_j2000_degs is not None:
            slew_and_settle(mount, ra_j2000_hours, dec_j2000_degs, op)
            if not self._still_calibrating():
                return self._abort(f"{op}: aborted during slew")
        if pw is not None:
            try:
                if not pw.status().mount.is_tracking:
                    logger.debug(f"{op}: starting mount tracking")
                    pw.mount_tracking_on()
            except Exception as ex:
                logger.warning(f"{op}: could not verify/start tracking: {ex}")

        # --- acquire + extract ------------------------------------------------
        extractions: list[dict] = []
        for i in range(st.number_of_frames):
            if not self._still_calibrating():
                return self._abort(f"{op}: aborted at frame {i + 1}/{st.number_of_frames}")
            image = self._expose(st, folder, tag=f"OC{i + 1:02d}")
            if image is None:
                continue  # _expose logged it; the min_frames gate decides below
            extracted = extract_sources(image)
            if extracted is None:
                self._log_error(f"{op}: frame {i + 1}: no sources extracted")
                continue
            logger.debug(f"{op}: frame {i + 1}: {extracted['n_detected']} sources")
            extractions.append(extracted)

        if len(extractions) < need:
            return self._fail(
                f"{op}: only {len(extractions)}/{st.number_of_frames} frames usable; "
                f"need >= {need} (min_frames_passing)"
            )

        # --- pooled centre fit ------------------------------------------------
        result = solve_optical_center(extractions)
        if result is None:
            return self._fail(
                f"{op}: no optical-center solution from {len(extractions)} pooled frames "
                f"(too few usable sources, or no radial coma signal)"
            )
        logger.info(
            f"{op}: center=({result.center_x:.1f}, {result.center_y:.1f}) "
            f"from {result.n_sources}/{result.n_detected} pooled sources, "
            f"residual_rms={result.residual_rms:.1f}px radiality={result.radiality:.2f}"
        )

        # --- coma slope over the RAW pooled sample (see module docstring) -----
        slope = fit_coma_slope(
            np.concatenate([e["x"] for e in extractions]),
            np.concatenate([e["y"] for e in extractions]),
            np.concatenate([e["ellipticity"] for e in extractions]),
            np.concatenate([e["flux"] for e in extractions]),  # flux-only weights
            result.center,
            coma_tolerance=st.coma_tolerance,
        )
        if slope is None or slope.low_coma_radius is None:
            logger.warning(f"{op}: no trustworthy coma slope -- persisting low_coma_radius=None "
                           f"(autofocus falls back to its geometric disk)")
        else:
            logger.info(f"{op}: coma slope k={slope.slope:.3e}/px -> "
                        f"low_coma_radius={slope.low_coma_radius:.0f}px "
                        f"(tolerance={slope.coma_tolerance}, rms={slope.residual_rms:.4f})")

        self._persist(result, slope, st)
        return result

    # ----------------------------------------------------------------- helpers
    def _wait_focuser_settled(self, op: str) -> bool:
        deadline = time.monotonic() + FOCUSER_SETTLE_TIMEOUT_SECONDS
        while self.unit.focuser.is_active(FocuserActivities.Moving):
            if not self._still_calibrating():
                self._abort(f"{op}: aborted while waiting for the focuser")
                return False
            if time.monotonic() >= deadline:
                self._fail(
                    f"{op}: focuser did not settle within {FOCUSER_SETTLE_TIMEOUT_SECONDS:.0f}s "
                    f"(at {self.unit.focuser.position}, target={self.unit.focuser.target}); "
                    f"check the focuser in PWI4"
                )
                return False
            time.sleep(0.5)
        return True

    def _wait_stage(self):
        stage = self.unit.stage
        if stage is None:
            return
        time.sleep(0.5)  # let the activity register before polling
        while stage.is_active(StageActivities.Homing) or stage.is_moving:
            time.sleep(0.5)

    def _expose(self, st, folder: str | None, tag: str):
        """One FULL-FRAME BIN-1 exposure; array (memory imager) or saved path."""
        imager, conf = self.unit.imager, self.unit.unit_conf
        assert imager is not None and conf is not None
        memory = imager.can_image_to_memory and imager.can_send_image_ready_event
        self._frame_seq += 1
        image_path = (
            None if memory else os.path.join(folder, f"{self._frame_seq:03d}_{tag}.fits")  # type: ignore[arg-type]
        )
        settings = ImagerSettings(
            seconds=st.exposure,
            binning=1,  # hardcoded: the centre DEFINES the bin-1 frame (module doc)
            roi=imager.full_frame,
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

    def _persist(self, result: OpticalCenterResult, slope, st):
        conf = self.unit.unit_conf
        assert conf is not None
        if conf.calibration is None:
            conf.calibration = CalibrationConfig()
        # Servicing the optics bumps the epoch elsewhere; re-calibrating within
        # the same epoch keeps it, a first calibration starts at 0.
        previous = conf.calibration.products.optical_center
        epoch = previous.mechanical_epoch if previous is not None else 0
        record = OpticalCenterCalibration(
            center_x=result.center_x,
            center_y=result.center_y,
            low_coma_radius=None if slope is None else slope.low_coma_radius,
            coma_slope=None if slope is None else slope.slope,
            coma_tolerance=st.coma_tolerance,
            image_shape=result.image_shape,
            n_sources=result.n_sources,
            residual_rms=result.residual_rms,
            radiality=result.radiality,
            timestamp=time_stamp(),
            mechanical_epoch=epoch,
        )
        conf.calibration.products.optical_center = record
        try:
            Config().set_unit(unit_name=self.unit.hostname, unit_conf=conf)
            logger.info(f"saved calibration.products.optical_center for '{self.unit.hostname}': "
                        f"center=({record.center_x:.1f}, {record.center_y:.1f}) "
                        f"low_coma_radius={record.low_coma_radius} epoch={epoch}")
        except Exception as ex:
            self._log_error(f"could not save calibration.products.optical_center "
                            f"for '{self.unit.hostname}': {ex}")

    def _still_calibrating(self) -> bool:
        """Cooperative abort -- the operator clearing the flags stops the loop."""
        return self.unit.is_active(UnitActivities.CalibratingOpticalCenter) or self.unit.is_active(
            UnitActivities.Calibrating
        )

    def _fail(self, message: str):
        logger.error(message)
        self.errors.append(message)
        return None

    def _abort(self, message: str):
        logger.info(message)
        self.errors.append(message)
        return None

    def _log_error(self, message: str):
        logger.error(message)
        self.errors.append(message)
