# Pick-off stage geometry — the "spec" stage position

> **Status: DESIGNED + IMPLEMENTED (2026-07-01).** Pure solver
> `find_spec_stage_position` + hardware loop `StageCalibrator` are in
> `src/imaging/stage_geometry.py` and synthetic-verified; **not hardware-tested**.
> Persisted result: `common/config/calibration.py::StageCalibrationConfig` (a field
> of `CalibrationConfig`). Sibling designs: `autofocus_design.md`,
> `optical_center_design.md`.

## Context & purpose

The MAST unit's pick-off stage carries a **45° folding mirror on a linear (1-DOF)
track** in the converging beam. Retracted (stage "Sky" preset) the field is clean;
inserted, the mirror occults a band of sky and, at the right position, picks off the
**on-axis target's light into the fiber → central spectrograph**. This calibration
finds the **"spec" stage position**: the stage coordinate where the mirror's shadow
centerline passes through the unit's **optical center**, so the pick-off sits on the
science target.

Inputs are already available: `imaging/mirror_shadow.detect_mirror_shadow` gives a
shadow centerline `(angle, offset)` per frame; `imaging/optical_center.find_optical_center`
(see `optical_center_design.md`) gives the optical center pixel. This routine ties
them together.

## Geometry — a 1-D linear solve

At stage position `s` the centerline is a line of (near-)fixed orientation whose
signed perpendicular distance from the optical center is

```
d(s) = -(ocx - cx)·sin(angle) + (ocy - cy)·cos(angle) - offset
```

(the optical center's perpendicular coordinate in `ShadowModel`'s convention, minus
`offset`). As the stage translates, the line translates, so **`d(s)` is linear**:

```
d(s) = A + B·s        spec position   s* = -A / B
```

- **`B`** (perp-pixels per stage step) is the stage→detector **scale**. It falls out
  of the same fit — **no separate scale calibration is needed**. A *rough* prior is
  only needed to plan where to put the shots; get it empirically (spread across
  travel and check for a sign change, or a 2-shot Δs bootstrap), **not** from the
  stage spec (steps↔mm alone can't give steps↔pixels; that also needs the optics and
  the very stage-axis-on-detector projection we're implicitly measuring).
- **Sign disambiguation is free.** A single frame's `|d|` gives the magnitude of the
  offset but not the side; the differential across frames gives the *signed* slope,
  so `sign(s* - s_near)` is the move direction.
- **Bracket, don't extrapolate.** Take ≥3 frames that **straddle** the optical center
  (a sign change in `d`), so `s*` is interpolated. The solver refuses (soft-fail, but
  still reports `s*`) when the frames are all on one side.

### "Sweep coordinate" — and what it is *not*

`s*` is the **1-DOF sweep coordinate**: it slides the centerline until it passes
*through* the optical center. It is **not on-fiber placement**. The optical center is
2-D; the stage controls one axis, so the **along-centerline residual** (the fiber's
fixed mount position vs. the optical center) is left over. Final placement is:

```
on-fiber = stage sweep coord (s*, coarse, this calibration)
         + mount along-centerline offset (fine, later)
         + flux peak-up on a star (later; v1 skips)
```

So **v1 = geometry only**, carrying an uncharacterized fiber offset
(`StageCalibrationConfig.fiber_offset = None` until a commissioning peak-up).

## Procedure

1. **Preconditions:** the unit already has an **optical-center calibration** (stage
   geometry is defined relative to it); mount slewed to a suitable field + tracking;
   stage able to insert the mirror.
2. **Reference frame** for the differential-ratio shadow detection: a **retracted
   (Sky), in-focus** frame — reuse the focus run's final frame, or acquire one.
   (Dawn/twilight flat needs no reference — the shadow is a direct intensity drop.)
3. **Sweep** ≥3 stage positions centered on the current spec estimate, **approached
   from a consistent direction** (backlash). At each: full-frame, **bin-1** exposure
   (so pixels match the stored bin-1 optical center) → `detect_mirror_shadow(img,
   reference=...)`.
4. **Solve** `find_spec_stage_position(models, positions, optical_center)`.
5. **Persist** `StageCalibrationConfig` and (optionally) park at `s*`.

## Implementation

- **`imaging/stage_geometry.find_spec_stage_position(shadow_models, stage_positions,
  optical_center) → StageGeometryResult`** — the pure solver. Drops `present=False`
  frames; wrap-safe centerline-orientation handling (headless, near-vertical, so it
  averages on the doubled angle and re-aligns each `d` to one reference normal);
  prominence-weighted linear fit; guards for too-few-frames, shadow-not-translating,
  not-bracketed, and nonlinear-fit. Quality: `residual_rms` (line fit) +
  `angle_rms_deg` (orientation consistency).
- **`imaging/stage_geometry.StageCalibrator(unit)`** — the hardware loop: slew+track,
  retract stage, acquire reference, sweep+detect, solve, persist via
  `Config().set_unit(...)`, cooperative abort on the `UnitActivities.Calibrating`
  flag. Branches on `imager.can_image_to_memory` (ZWO array via `image_array` vs.
  PHD2 file path); both feed `detect_mirror_shadow`.
- **`common/config/calibration.StageCalibrationConfig`** — `spec_position` (steps) +
  `slope` + `fiber_offset` placeholder + provenance/quality (`optical_center`,
  `image_shape`, `n_frames`, `residual_rms`, `angle_rms_deg`, `bracketed`) +
  `timestamp` + `mechanical_epoch`. A **geometric** calibration: it shares
  `mechanical_epoch` with the optical center, so servicing the optics invalidates the
  pair together (`.matches(image_shape, epoch)` gates consumption). Naming note:
  `...Config` breaks the sibling pattern (`OpticalCenterCalibration`,
  `FocuserCalibration`); kept per Arie's request, may rename.

## Status & limits

- **Verified:** synthetic sweep — `s*` recovered to <1 step, `B` to <1 %, guards fire
  (bracketing, flat, absent-frame); config round-trips with the epoch/size guard.
- **Not hardware-tested.** The `StageCalibrator` loop, the illumination-regime
  handling, and the real shadow detection on-sky are unproven.
- **Deferred:** the fiber offset (mount peak-up), and the `/calibrate` orchestrator
  that runs `stage` *after* `focus → optical_center` (see
  `calibration_orchestration.md`).
