"""Pick-off stage geometry: the "spec" stage position.

The pick-off stage carries a 45-deg folding mirror on a linear (1-DOF) track in
the converging beam.  Inserted, it occults a band of sky and casts a shadow whose
centerline (:func:`imaging.mirror_shadow.detect_mirror_shadow`) translates across
the detector as the stage moves.  For spectroscopy we want the **"spec" stage
position**: the one where that centerline passes through the unit's optical center
(:func:`imaging.optical_center.find_optical_center`), so the mirror picks off the
on-axis target's light into the fiber.

Geometry: at stage position ``s`` the centerline is a line of fixed orientation
whose signed perpendicular distance from the optical center,

    d(s) = -(ocx - cx)*sin(angle) + (ocy - cy)*cos(angle) - offset ,

is **linear** in ``s`` (a translating line, constant orientation).  So

    d(s) = A + B*s ,   spec position  s* = -A / B ,

where ``B`` (perp-pixels per stage step) is the stage->detector **scale** -- it
falls out of the same fit, so it needs no separate calibration.  A few shadow
frames that **straddle** the optical center (a sign change in ``d``) pin the line;
``s*`` is then an interpolation, not an extrapolation.

Scope (v1 = geometry only): ``s*`` is the 1-DOF **sweep coordinate** that puts the
centerline *on* the optical center.  It is NOT on-fiber placement -- the
along-centerline residual (the fiber's fixed mount position vs. the optical
center) is taken up later by a mount offset + a flux peak-up, an uncharacterized
fiber offset this routine deliberately does not chase.

Design reference: unit self-calibration design, section 10 (pick-off stage
geometry).  Sits beside :mod:`imaging.mirror_shadow` and
:mod:`imaging.optical_center`.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from common.activities import StageActivities, UnitActivities
from common.config import Config
from common.config.calibration import CalibrationConfig, StageCalibrationConfig
from common.interfaces.imager import ImagerSettings
from common.mast_logging import init_log
from common.utils import time_stamp
from imaging.mirror_shadow import ShadowModel, detect_mirror_shadow

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)


@dataclass
class StageGeometryResult:
    """The spec stage position and the quality behind it.

    ``spec_position`` is ``s* = -A/B`` (the stage coordinate placing the shadow
    centerline on the optical center); ``slope`` is ``B`` -- the emergent
    stage->detector scale (perp-pixels per stage step).  ``bracketed`` records
    whether the frames straddled the optical center (a sign change in ``d``), so
    ``s*`` was interpolated rather than extrapolated.  ``residual_rms`` (line fit)
    and ``angle_rms_deg`` (centerline-orientation spread across frames) are the
    quality figures behind the ``stage-geometry`` calibration gate.
    """

    has_solution: bool
    spec_position: float | None  # s* : stage coordinate placing centerline on optical center
    slope: float | None  # B : perp-pixels per stage step (the emergent scale)
    intercept: float | None  # A
    n_frames: int  # shadow-present frames used in the fit
    residual_rms: float  # of the d(s) = A + B*s linear fit (pix)
    angle_rms_deg: float  # spread of centerline orientation across frames (consistency)
    bracketed: bool  # did the frames straddle the optical center (sign change in d)
    optical_center: tuple[float, float]
    centerline_angle: float | None  # mean centerline orientation (rad) of the line at s*
    message: str

    # per-frame arrays kept for plotting / inspection (not persisted)
    stage_positions: np.ndarray | None = field(default=None, repr=False)
    distances: np.ndarray | None = field(default=None, repr=False)  # signed d(s), pix


def _perp_distance(model: ShadowModel, px: float, py: float) -> float:
    """Signed perpendicular distance (pix) from point ``(px, py)`` to the centerline.

    Uses the same perpendicular coordinate convention as ``ShadowModel`` (normal
    ``(-sin angle, cos angle)`` about the image center): a pixel lies on the
    centerline where its coordinate equals ``offset``, so the point's distance is
    its coordinate minus ``offset``.
    """
    ny, nx = model.image_shape
    cx, cy = (nx - 1) / 2.0, (ny - 1) / 2.0
    s_point = -(px - cx) * np.sin(model.angle) + (py - cy) * np.cos(model.angle)
    return float(s_point - model.offset)


def _mean_orientation(angles: np.ndarray) -> tuple[float, float]:
    """Wrap-safe mean and std (rad) of headless (period-pi) centerline angles.

    Centerlines are orientations, not directions (period pi), and cluster near
    vertical where the ``(-pi/2, pi/2]`` representation can wrap -- so average on
    the doubled angle (circular statistics) instead of the raw values.
    """
    c = float(np.mean(np.cos(2 * angles)))
    s = float(np.mean(np.sin(2 * angles)))
    mean_angle = 0.5 * np.arctan2(s, c)
    r = np.hypot(c, s)
    std = 0.5 * np.sqrt(-2.0 * np.log(r)) if r > 0 else float("inf")
    return float(mean_angle), float(std)


def find_spec_stage_position(
    shadow_models,
    stage_positions,
    optical_center,
    *,
    require_bracketed: bool = True,  # only solve when the frames straddle the center
    min_frames: int = 3,  # design: >=3 for backlash / nonlinearity / noise-averaging
    min_span_px: float = 5.0,  # the shadow must actually move across the stage range
    max_residual_rms: float = 5.0,  # line-fit quality gate (pix)
    max_angle_rms_deg: float = 5.0,  # centerline-orientation consistency gate
    weights_from_prominence: bool = True,  # weight cleaner detections more
) -> StageGeometryResult:
    """Solve for the spec stage position from shadow frames at known stage positions.

    ``shadow_models`` are :class:`imaging.mirror_shadow.ShadowModel` (one per
    stage position, aligned with ``stage_positions``); frames whose shadow is not
    ``present`` are dropped.  ``optical_center`` is an
    :class:`imaging.optical_center.OpticalCenterResult` (its ``.center``) or an
    ``(x, y)`` tuple.

    Fits ``d(s) = A + B*s`` over the present frames and returns ``s* = -A/B``.
    ``has_solution`` requires: >= ``min_frames`` present, the shadow actually
    translating (``d`` span >= ``min_span_px``, ``B != 0``), the fit clean
    (``residual_rms`` and ``angle_rms`` within gates) and -- when
    ``require_bracketed`` -- the frames straddling the optical center.  ``s*`` is
    still reported on a soft failure (e.g. not bracketed) so the caller can extend
    the stage range toward it.
    """
    oc = getattr(optical_center, "center", None) or tuple(optical_center)
    ocx, ocy = float(oc[0]), float(oc[1])

    pos, dist, prom, ang = [], [], [], []
    for model, s in zip(shadow_models, stage_positions, strict=True):
        if not getattr(model, "present", False):
            continue
        pos.append(float(s))
        dist.append(_perp_distance(model, ocx, ocy))
        prom.append(float(getattr(model, "prominence", 1.0)))
        ang.append(float(model.angle))
    n = len(pos)

    def fail(msg, spec=None, slope=None, intercept=None, resid=float("nan"), arms=float("nan"), brk=False, cang=None):
        return StageGeometryResult(
            has_solution=False,
            spec_position=spec,
            slope=slope,
            intercept=intercept,
            n_frames=n,
            residual_rms=resid,
            angle_rms_deg=arms,
            bracketed=brk,
            optical_center=(ocx, ocy),
            centerline_angle=cang,
            message=msg,
            stage_positions=np.array(pos),
            distances=np.array(dist),
        )

    if n < 2 or len(set(pos)) < 2:
        return fail(f"need >= 2 shadow-present frames at distinct stage positions, have {n}")

    pos_a = np.array(pos)
    prom_a = np.array(prom)
    ang_a = np.array(ang)
    mean_angle, angle_rms = _mean_orientation(ang_a)
    angle_rms_deg = float(np.degrees(angle_rms))

    # Align each frame's signed distance to one reference normal: the (-pi/2, pi/2]
    # representation can flip near vertical, which flips d's sign; project onto the
    # mean normal to keep the sign convention consistent across frames.
    n_ref = np.array([-np.sin(mean_angle), np.cos(mean_angle)])
    signs = np.sign([(-np.sin(a)) * n_ref[0] + np.cos(a) * n_ref[1] for a in ang_a])
    signs[signs == 0] = 1.0
    dist_a = np.array(dist) * signs

    if float(np.ptp(dist_a)) < min_span_px:
        return fail(
            f"shadow barely moved ({np.ptp(dist_a):.1f} < {min_span_px} px) across the "
            "stage range -- check stage motion or widen the range",
            arms=angle_rms_deg,
            cang=mean_angle,
        )

    w = prom_a if (weights_from_prominence and np.all(prom_a > 0)) else None
    slope, intercept = np.polyfit(pos_a, dist_a, 1, w=w)
    if slope == 0:
        return fail("degenerate fit (zero slope)", arms=angle_rms_deg, cang=mean_angle)

    resid = dist_a - (slope * pos_a + intercept)
    residual_rms = float(np.sqrt(np.mean(resid**2)))
    spec = float(-intercept / slope)
    bracketed = bool(dist_a.min() < 0 < dist_a.max())

    reasons = []
    if n < min_frames:
        reasons.append(f"only {n} frames (< {min_frames})")
    if require_bracketed and not bracketed:
        reasons.append(f"optical center not bracketed -- extend stage toward s*~{spec:.0f}")
    if residual_rms > max_residual_rms:
        reasons.append(f"fit rms {residual_rms:.1f} > {max_residual_rms} px")
    if angle_rms_deg > max_angle_rms_deg:
        reasons.append(f"centerline angle spread {angle_rms_deg:.1f} > {max_angle_rms_deg} deg")

    result = StageGeometryResult(
        has_solution=not reasons,
        spec_position=spec,
        slope=float(slope),
        intercept=float(intercept),
        n_frames=n,
        residual_rms=residual_rms,
        angle_rms_deg=angle_rms_deg,
        bracketed=bracketed,
        optical_center=(ocx, ocy),
        centerline_angle=mean_angle,
        message=(
            f"spec stage position s*={spec:.1f} (slope {slope:+.4f} px/step, "
            f"n={n}, rms={residual_rms:.2f} px, bracketed={bracketed})"
            if not reasons
            else "; ".join(reasons)
        ),
        stage_positions=pos_a,
        distances=dist_a,
    )
    return result


def plot_stage_geometry(result: StageGeometryResult):
    """Show d(s) with the linear fit, the spec position, and the zero crossing."""
    import matplotlib.pyplot as plt

    s, d = result.stage_positions, result.distances
    assert s is not None and d is not None
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.axhline(0, color="gray", lw=1, ls=":")
    ax.plot(s, d, "o", color="k", label="d(s): center-to-optical-center")
    if result.slope is not None and result.spec_position is not None:
        xs = np.linspace(min(s.min(), result.spec_position), max(s.max(), result.spec_position), 100)
        ax.plot(
            xs,
            result.slope * xs + result.intercept,
            "-",
            color="crimson",
            lw=1,
            label=f"fit (B={result.slope:+.3f} px/step)",
        )
        ax.axvline(result.spec_position, color="dodgerblue", lw=1.2, label=f"s* = {result.spec_position:.1f}")
    ax.set_xlabel("stage position (steps)")
    ax.set_ylabel("signed perp. distance to optical center (pix)")
    ax.set_title("spec stage position" if result.has_solution else f"no solution: {result.message}")
    ax.legend()
    plt.tight_layout()
    plt.show()


class StageCalibrator:
    """Drives the pick-off stage calibration loop on a live unit.

    Sweeps the folding mirror across several inserted stage positions, detects the
    shadow at each (:func:`imaging.mirror_shadow.detect_mirror_shadow`), and solves
    for the spec stage position (:func:`find_spec_stage_position`) -- the stage
    coordinate that places the shadow centerline on the unit's optical center.  On
    success it persists a :class:`common.config.calibration.StageCalibrationConfig`
    to ``unit_conf.calibration.stage``, sharing the optical center's mechanical
    epoch.

    Preconditions: the unit must already carry an optical-center calibration
    (``calibration.optical_center``) -- stage geometry is defined relative to it --
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
        oc_cal = cal.optical_center if cal else None
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
        spec_center = cal.stage.spec_position if cal and cal.stage else conf.stage.presets.spec
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
                models,
                used,
                optical_center,
                require_bracketed=require_bracketed,
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
        conf.calibration.stage = stage_cal
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
