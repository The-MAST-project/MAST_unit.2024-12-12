# Unit self-calibration — design overview

> **Status: design overview + partly implemented (2026-07-01).** Umbrella for the
> per-unit self-calibration of a MAST unit (0.6 m f/3 parabola, PlaneWave/PWI4 mount,
> pick-off fiber feed). Per-subsystem designs live alongside this file; this doc
> indexes them and carries the cross-cutting sections not yet split out (thermal
> focus-seed, mirror temperature, the Plan focus flag, and calibration storage).
> Original source: shared Claude conversation "Designing MAST unit features"
> (Arie, 2026-06-18), relocated here from shared memory per the per-repo-docs policy.

## Subsystem designs

- **Autofocus (HFD / donut)** — `autofocus_design.md`
- **Optical center (coma-null)** — `optical_center_design.md`
- **Pick-off stage geometry** — `stage_geometry_design.md`
- **Calibration invocation / status / `/calibrate`** — `calibration_orchestration.md`

## Per-unit calibration outputs

The procedure yields, per `mastNN`: the **optical-center pixel** on the detector and
a **known-as-good focus position** (plus, for the fiber feed, the stage **spec
position**). Each is persisted with **provenance + quality**: timestamp, unit,
conditions, and a quality figure — V-curve fit residual + achieved HFD for focus;
star count + elongation-null fit residual for optical center; line-fit residual +
angle-consistency for stage geometry. This lets the hub/operator reject bad
calibrations and detect drift. *(Realized: `common/config/calibration.py`.)*

## Thermal focus-seed model *(designed; not implemented)*

- Best focus is **temperature-dependent** (tube/focuser expansion; the day-night
  swing at the site is large). **Mirror temperature predicts better than ambient**
  (the mirror lags ambient with a long time constant; ambient-only `dFocus/dT` is
  noisy near evening cooldown).
- Relation is approximately **linear**: `seed = offset + slope·T_now`. Resist
  higher-order — sparse points overfit.
- **Training data:** a rolling set of `(mirror_temp, best_focus, timestamp, quality)`,
  one appended per **successful autofocus** → self-calibrating, tracks slow drift.
  Robust / sigma-clipped fit; **rolling window / recency weighting** so post-service
  points age out instead of fighting new state.
- **Maturity gate:** trust a slope only with ≥N points spanning ≥ΔT; below that,
  return the **most-recent good focus as a flat seed**.
- **Degradation ladder:** (a) mature model + temp → predicted seed → short V-curve;
  (b) temp `None` but recent good focus → flat seed (wider V-curve); (c) neither →
  full Phase-A acquisition. **Guardrail across all:** take one image and check HFD is
  in the expected ballpark before committing; if wildly off, the seed is stale → fall
  down the ladder.
- **Keying:** per `(unit, config-epoch)` — the focus zero-point depends on optical
  config (filter, spectrograph feed, camera spacing). Carry **altitude** as an unused
  covariate (flexure) so residuals can later demand it without re-instrumenting.

## Mirror temperature source

- A `get_mirror_temperature()` returning `Optional[float]`; **`None` when no reading**
  → the focus-seed model falls back to the bare (non-thermal) seed rather than
  guessing. **Never fabricate** a constant. The **timestamp is captured at the call
  site**, not in the stub, so the calibration record carries `(temperature,
  read_time)` and a stale reading can be rejected.
- **PWI4 reality (confirmed on-unit, PWI4 4.1.8):** not in `/status`;
  `GET http://localhost:8220/temperatures/pw1000` returns `temperature.primary`
  (primary **mirror**), `.ambient`, `.secondary`, `.m3`, with `-999.000` = no
  sensor → `None`.

## Declarative focus-required flag (Plan schema) *(designed)*

- The Plan carries **one bare boolean**. **Declarative:** `True` = "this observation
  must be in focus" (the unit decides full run / seeded confirm / nothing); `False` =
  no focus requirement, observe as-is (**not** "forbid focusing").
- Because the flag is parameterless, everything defining "valid focus" lives
  **off-Plan as unit-level policy with site defaults**: tolerance, freshness window
  Δt, temperature window ΔT, max focus-time budget, slew-to-focus-field policy. The
  time-budget cap is unit/scheduler policy (matters for time-critical transients —
  fail fast vs. spend 10 min focusing).
- **Field selection:** plan-driven focus runs at/near the science field; if star-poor
  near the optical axis, fall back to a nearby star-rich field (costs slew time).
  Each plan-driven run also **feeds the thermal model** (self-calibration during ops).

## Storage — per-unit config DB *(partly realized)*

- **Each unit writes its own document directly** (single writer per record — no
  cross-unit contention). Make writes **atomic**, or stage then flip a "current/valid"
  marker last, so readers (including the unit's own operational gate) never see a
  half-written calibration.
- **Two write patterns, modelled distinctly:** geometric calibrations (optical center,
  stage axis/scale, fiber offset) = **replace-on-refresh versioned snapshots**;
  thermal focus-seed = **accumulating/rolling point set**.
- **Shared mechanical epoch id** across the geometric calibrations — bumps on service,
  **invalidates them as a group** (prevents fresh stage geometry paired with a stale
  optical center). *(Realized: `mechanical_epoch` + `.matches()` on the calibration
  models.)*
- **The operational gate reads this DB:** "is this unit calibrated?" = "does it have
  valid, in-epoch, fresh-enough entries?" — closing the loop between calibration
  values, provenance/quality, and operational status (see
  `calibration_orchestration.md`).
