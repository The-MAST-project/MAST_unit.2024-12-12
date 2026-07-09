# Unit calibration orchestration — the `/calibrate` endpoint

> **Status: DESIGN (2026-07-01), not implemented.** Endpoint-first spine for unit
> self-calibration. The per-subsystem pieces exist to varying degrees (stage:
> `StageCalibrator`; optical-center + focus: analysis built, write-side pending).
> This doc is the contract that ties them together. Sibling designs:
> `autofocus_design.md`, `optical_center_design.md`, `stage_geometry_design.md`.

## Idea

One unit-local routine, reached through one endpoint, that all invocation paths
converge on (manual, plan-flag, nightly fleet pass, unit-side auto-retry). It runs
under a single **`calibrating`** activity (`UnitActivities.Calibrating`) and
reconciles the `calibration:*` slice of `why_not_operational`.

```
POST /calibrate?force=false&subsystems=[]
```

- **`force`** (default `false`) — when false, **skip** any subsystem whose
  calibration is already valid/fresh; when true, redo it.
- **`subsystems`** (default `[]` = all *implemented*) — optional subset of
  `{focus, optical_center, stage}`. It is an **unordered filter**, NOT a run order.

Returns a `CanonicalResponse` immediately (background activity); pollable and
abortable.

## The routine owns the order (this is the crux)

"Order is significant" means **one correct dependency order the routine enforces**,
not "run them in the order the caller typed." The physics fixes it:

```
focus  →  optical_center  →  stage
```

- Clean **coma lives at best focus** (defocused → the donut is pupil-dominated), so
  **focus precedes optical_center**.
- **stage** runs **last**: it *inserts* the mirror (obstructs the field) and reuses
  the focus run's final in-focus retracted frame as its detection reference.
- A requested subset is executed in this order, **auto-including prerequisites**
  (ask for `optical_center` while focus is stale → focus is pulled in), never a bad
  calibration produced from a wrong-regime frame.

## Shared preamble (once, before any subsystem)

1. **Preconditions:** `is_safe AND dark-enough` (a stricter sun threshold than
   safety — you can be safe to open at twilight yet too bright for SEP stars).
2. **Mount:** slew to a star-rich field + track.
3. **Stage:** home / retract (clear field). NOTE: "home the stage" (preamble) ≠ the
   `stage` *subsystem* (which deliberately inserts the mirror).
4. **Reference full frame** at the seed position — for the autofocus **Phase-0
   triage** (donut vs. V-curve; see `autofocus_design.md`).

## Frame reuse (one frame can't serve both consumers)

- **Top reference frame** → autofocus Phase-0 triage only (may be a donut).
- **Focus's final in-focus frame** → reused by `optical_center` *and* `stage`
  (optical center needs an in-focus frame; when focus isn't re-run this session,
  `optical_center` acquires its own at the known best position).

## "Missing" is per-subsystem (reuse the operational-gate predicate)

`force=false` skips a subsystem when its calibration is **valid/in-epoch/fresh**,
which differs by type:

- `optical_center`, `stage` — **geometric**: invalidated as a group by
  `mechanical_epoch` (`.matches(image_shape, epoch)`).
- `focus` — freshness / temperature-windowed.

Same predicate the operational gate computes for `calibration:not-calibrated` — so
define it once and share it.

## Status axes & abort

- Background **`calibrating`** activity; result carries per-subsystem outcome
  `ran / skipped-fresh / failed(sub-reason)` → the `calibration:not-calibrated`
  detail payload (sub-reason ∈ `no-stars`/`didn't-converge`/`focuser-fault`/
  `camera-error`/`stage-geometry`).
- **`is_safe` runtime interrupt:** flipping false mid-run **aborts and stows**;
  the give-up counter only increments on attempts that genuinely ran under good
  conditions (separates "broken" from "waiting on weather"). v1 = indefinite retry,
  surface per-attempt detail in telemetry.
- Operational gate: `not-calibrated` = **never established a baseline this session**;
  calibrated-but-stale **stays operational** (lazy refresh is the plan flag's job).

## Proposed shape

```
check preconditions (safe & dark; mount; stage retractable)
slew to a star-rich field + track; home/retract stage
acquire top reference full frame            (autofocus triage)
run, in dependency order, filtered to the requested set (auto-including prereqs),
  skipping fresh ones unless force:
    focus          triage off reference → sweep → in-focus frame + write calibration.focuser
    optical_center reuse in-focus frame  → write optical_center + low_coma_radius
    stage          insert mirror         → StageCalibrator → write calibration.stage
reconcile calibration:* in why_not_operational
```

## Build status (2026-07-01)

- **Not implemented** as an endpoint. `src/autofocusing.py` still drives ps3cli only.
- Subsystem readiness: `stage` has a full loop (`StageCalibrator`); `focus` and
  `optical_center` have their analysis built but **no write-side** (the optical-center
  finder doesn't yet persist `calibration.optical_center` or compute
  `low_coma_radius = coma_tolerance / k`; autofocus doesn't persist
  `calibration.focuser`).
