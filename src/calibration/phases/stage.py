"""Pick-off stage calibration -- the hardware loop for the ``stage`` phase.

The *solver* lives in :mod:`calibration.analysis.stage_geometry` (pure, no
hardware); this module drives a live unit through the acquire -> detect -> solve
-> persist loop that feeds it.  Split out of the analysis module so the solver
stays importable -- and replayable offline -- without pulling in ``Unit``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

import numpy as np

from calibration.analysis.mirror_shadow import ShadowModel, detect_mirror_shadow
from calibration.analysis.stage_geometry import (
    StageGeometryResult,
    find_spec_stage_position,
)
from common.activities import StageActivities, UnitActivities
from common.config import Config
from common.config.calibration import CalibrationConfig, StageCalibrationConfig
from common.interfaces.imager import ImagerSettings
from common.mast_logging import init_log
from common.utils import time_stamp

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)


class StageCalibrator:
    """Drives the pick-off stage calibration loop on a live unit.

    Sweeps the folding mirror across several inserted stage positions, detects the
    shadow at each (:func:`calibration.analysis.mirror_shadow.detect_mirror_shadow`), and solves
    for the spec stage position (:func:`find_spec_stage_position`) -- the stage
    coordinate that places the shadow centerline on the unit's optical center.  On
    success it persists a :class:`common.config.calibration.StageCalibrationConfig`
    to ``unit_conf.calibration.products.stage``, sharing the optical center's mechanical
    epoch.

    Preconditions: the unit must already carry an optical-center calibration
    (``calibration.products.optical_center``) -- stage geometry is defined relative to it --
    with the mount tracking on a field where the shadow is detectable.  Full frames
    at bin 1 are used throughout, so pixel coordinates match the stored (bin-1)
    optical center.  Design ref: unit self-calibration design, section 10.
    """

    def __init__(self, unit: Unit):
        self.unit = unit
        self.errors: list[str] = []

    def calibrate(  # noqa: C901
        self,
        *,
        n_positions: int = 5,  # >= 3 (design: backlash / nonlinearity / noise)
        span_steps: int | None = None,  # half-sweep about the current spec preset (steps)
        target_ra_j2000_hours: float | None = None,  # slew to a star field first; when
        target_dec_j2000_degs: float | None = None,  #   None, calibrate at the current pointing
        exposure: float = 5.0,  # seconds per frame
        settle_seconds: float = 1.0,  # extra dwell after the stage stops
        reference=None,  # retracted in-focus reference frame (reuse the focus run's);
        #                  when None, one is acquired at the Sky preset
        require_bracketed: bool = True,
        move_to_spec: bool = True,  # park at the solved spec position on success
        folder: str | None = None,  # required only for a file-only imager (PHD2)
    ) -> StageGeometryResult | None:
        """Run the full acquire -> detect -> solve -> persist loop; return the result.

        Returns the :class:`StageGeometryResult` (check ``has_solution``), or
        ``None`` if a precondition failed before any solve (see ``self.errors``).
        """
        op = "StageCalibrator.calibrate"
        self.errors = []
        unit = self.unit
        conf, stage, imager = unit.unit_conf, unit.stage, unit.imager
        pw, mount = unit.pw, unit.mount
        if conf is None or stage is None or imager is None or pw is None or mount is None:
            return self._fail(f"{op}: unit not fully initialised (conf/stage/imager/mount)")

        cal = conf.calibration
        oc_cal = cal.products.optical_center if cal else None
        if oc_cal is None:
            return self._fail(f"{op}: no optical-center calibration -- run 'optical_center' first")
        optical_center = (oc_cal.center_x, oc_cal.center_y)
        mech_epoch = oc_cal.mechanical_epoch

        if not stage.detected or not stage.connected:
            return self._fail(f"{op}: stage not available (detected/connected)")
        min_travel, max_travel = stage.min_travel, stage.max_travel
        if min_travel is None or max_travel is None:
            return self._fail(f"{op}: stage travel range unknown")

        memory = imager.can_image_to_memory and imager.can_send_image_ready_event
        if not memory and folder is None:
            return self._fail(f"{op}: file-only imager needs a 'folder' for the frames")

        # Sweep positions centered on the current spec estimate, clipped to travel.
        spec_center = (
            cal.products.stage.spec_position
            if cal and cal.products.stage
            else conf.stage.presets.spec
        )
        if span_steps is None:
            span_steps = max(2000, int(0.05 * (max_travel - min_travel)))
        lo = max(min_travel, int(spec_center - span_steps))
        hi = min(max_travel - 1, int(spec_center + span_steps))
        positions = [int(round(p)) for p in np.linspace(lo, hi, n_positions)]

        self.unit.start_activity(UnitActivities.Calibrating, details=["stage geometry"])
        series = imager.start_exposure_series(purpose="stage-calibration")
        try:
            # Point at a star field and track -- the star-mode shadow detection needs
            # stars (a dawn/twilight flat is the exception).  With no target given we
            # assume the caller / orchestrator already positioned the mount.
            if target_ra_j2000_hours is not None and target_dec_j2000_degs is not None:
                logger.info(f"{op}: slewing mount to ra={target_ra_j2000_hours}h dec={target_dec_j2000_degs}deg")
                mount.goto_ra_dec_j2000(target_ra_j2000_hours, target_dec_j2000_degs)
                time.sleep(0.5)  # let the slew register before polling
                while mount.is_moving:
                    if not self._still_calibrating():
                        return self._abort(f"{op}: calibration stopped during slew")
                    time.sleep(0.5)
            else:
                logger.info(f"{op}: no target supplied -- calibrating at the current pointing")

            if not pw.status().mount.is_tracking:  # type: ignore[union-attr]
                logger.info(f"{op}: starting mount tracking")
                pw.mount_tracking_on()

            # Reference = retracted (Sky) in-focus frame for the differential-ratio
            # shadow detection; reuse the focus run's frame if the caller passed one.
            if reference is None:
                self._move_stage(conf.stage.presets.sky)
                reference = self._expose(exposure, folder, tag="STAGE_REF")
                if reference is None or not self._still_calibrating():
                    return self._abort(f"{op}: could not acquire reference frame")

            # Backlash: approach every position from below -> pre-position under 'lo'.
            self._move_stage(max(min_travel, lo - 500))

            models: list[ShadowModel] = []
            used: list[int] = []
            image_shape = None
            for pos in positions:
                if not self._still_calibrating():
                    return self._abort(f"{op}: calibration stopped")
                self._move_stage(pos)
                time.sleep(settle_seconds)
                img = self._expose(exposure, folder, tag=f"STAGE{pos:06d}")
                if img is None:
                    self.errors.append(f"{op}: no image at stage {pos}")
                    continue
                model = detect_mirror_shadow(img, reference=reference)
                models.append(model)
                used.append(pos)
                if model.present:
                    image_shape = model.image_shape
                logger.info(
                    f"{op}: stage {pos}: shadow present={model.present} "
                    f"tilt={model.tilt_deg:+.1f} offset={model.offset:+.1f} prom={model.prominence:.1f}"
                )

            result = find_spec_stage_position(
                models, used, optical_center, require_bracketed=require_bracketed,
            )
            logger.info(f"{op}: {result.message}")
            if not result.has_solution:
                self.errors.append(f"{op}: {result.message}")
                return result

            self._persist(result, optical_center, image_shape, mech_epoch)
            if move_to_spec and result.spec_position is not None:
                self._move_stage(int(round(result.spec_position)))
            return result
        finally:
            imager.end_exposure_series(series)
            self.unit.end_activity(UnitActivities.Calibrating)

    # ------------------------------------------------------------------ helpers
    def _still_calibrating(self) -> bool:
        """Cooperative abort: the loop bails if the Calibrating activity was cleared
        (e.g. by an operator stop or a safety interrupt that stows the unit)."""
        return self.unit.is_active(UnitActivities.Calibrating)

    def _move_stage(self, position: int):
        stage = self.unit.stage
        assert stage is not None
        resp = stage.move_absolute(int(position))
        if resp is not None and getattr(resp, "failed", False):
            self.errors.append(f"stage move to {position} failed: {resp.errors}")
            return
        time.sleep(0.5)  # let the stage timer register the move before we poll
        while stage.is_active(StageActivities.Moving) or stage.is_moving:
            time.sleep(0.5)

    def _expose(self, exposure: float, folder: str | None, tag: str):
        """One full-frame, bin-1 exposure; returns an array (memory imager) or the
        saved FITS path (file imager) -- both accepted by ``detect_mirror_shadow``."""
        imager, conf = self.unit.imager, self.unit.unit_conf
        assert imager is not None and conf is not None
        memory = imager.can_image_to_memory and imager.can_send_image_ready_event
        image_path = None if memory else os.path.join(folder, f"{tag}.fits")  # type: ignore[arg-type]
        settings = ImagerSettings(
            seconds=exposure,
            binning=1,  # bin 1 so the frame matches the stored (bin-1) optical center
            roi=imager.full_frame,
            gain=conf.acquisition.gain,
            image_path=image_path,
            save=image_path is not None,
        )
        imager.start_exposure(settings)
        if memory:
            imager.wait_for_image_ready()
            return imager.image_array
        imager.wait_for_image_saved()
        return image_path

    def _persist(self, result: StageGeometryResult, optical_center, image_shape, mech_epoch: int):
        if image_shape is None:
            self.errors.append("no shadow-present frame -- cannot record image_shape")
            return
        conf = self.unit.unit_conf
        assert conf is not None
        stage_cal = StageCalibrationConfig(
            spec_position=int(round(result.spec_position)),  # type: ignore[arg-type]
            slope=result.slope,
            optical_center=(float(optical_center[0]), float(optical_center[1])),
            image_shape=(int(image_shape[0]), int(image_shape[1])),
            n_frames=result.n_frames,
            residual_rms=result.residual_rms,
            angle_rms_deg=result.angle_rms_deg,
            bracketed=result.bracketed,
            timestamp=time_stamp(),
            mechanical_epoch=mech_epoch,
        )
        if conf.calibration is None:
            conf.calibration = CalibrationConfig()
        conf.calibration.products.stage = stage_cal
        try:
            Config().set_unit(unit_name=self.unit.hostname, unit_conf=conf)
            logger.info(f"saved stage calibration for '{self.unit.hostname}': spec_position={stage_cal.spec_position}")
        except Exception as e:
            self.errors.append(f"could not save stage calibration for '{self.unit.hostname}': {e}")

    def _fail(self, msg: str) -> None:
        logger.error(msg)
        self.errors.append(msg)
        return None

    def _abort(self, msg: str) -> None:
        logger.warning(msg)
        self.errors.append(msg)
        return None
