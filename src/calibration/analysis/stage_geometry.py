"""Pick-off stage geometry: the "spec" stage position.

The pick-off stage carries a 45-deg folding mirror on a linear (1-DOF) track in
the converging beam.  Inserted, it occults a band of sky and casts a shadow whose
centerline (:func:`calibration.analysis.mirror_shadow.detect_mirror_shadow`) translates across
the detector as the stage moves.  For spectroscopy we want the **"spec" stage
position**: the one where that centerline passes through the unit's optical center
(:func:`calibration.analysis.optical_center.find_optical_center`), so the mirror picks off the
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
geometry).  Sits beside :mod:`calibration.analysis.mirror_shadow` and
:mod:`calibration.analysis.optical_center`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from calibration.analysis.mirror_shadow import ShadowModel
from common.mast_logging import init_log

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

    ``shadow_models`` are :class:`calibration.analysis.mirror_shadow.ShadowModel` (one per
    stage position, aligned with ``stage_positions``); frames whose shadow is not
    ``present`` are dropped.  ``optical_center`` is an
    :class:`calibration.analysis.optical_center.OpticalCenterResult` (its ``.center``) or an
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

    def fail(msg, spec=None, slope=None, intercept=None, resid=float("nan"),
             arms=float("nan"), brk=False, cang=None):
        return StageGeometryResult(
            has_solution=False, spec_position=spec, slope=slope, intercept=intercept,
            n_frames=n, residual_rms=resid, angle_rms_deg=arms, bracketed=brk,
            optical_center=(ocx, ocy), centerline_angle=cang, message=msg,
            stage_positions=np.array(pos), distances=np.array(dist),
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
            arms=angle_rms_deg, cang=mean_angle,
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
        spec_position=spec, slope=float(slope), intercept=float(intercept),
        n_frames=n, residual_rms=residual_rms, angle_rms_deg=angle_rms_deg,
        bracketed=bracketed, optical_center=(ocx, ocy), centerline_angle=mean_angle,
        message=(f"spec stage position s*={spec:.1f} (slope {slope:+.4f} px/step, "
                 f"n={n}, rms={residual_rms:.2f} px, bracketed={bracketed})"
                 if not reasons else "; ".join(reasons)),
        stage_positions=pos_a, distances=dist_a,
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
        ax.plot(xs, result.slope * xs + result.intercept, "-", color="crimson", lw=1,
                label=f"fit (B={result.slope:+.3f} px/step)")
        ax.axvline(result.spec_position, color="dodgerblue", lw=1.2,
                   label=f"s* = {result.spec_position:.1f}")
    ax.set_xlabel("stage position (steps)")
    ax.set_ylabel("signed perp. distance to optical center (pix)")
    ax.set_title("spec stage position" if result.has_solution else f"no solution: {result.message}")
    ax.legend()
    plt.tight_layout()
    plt.show()
