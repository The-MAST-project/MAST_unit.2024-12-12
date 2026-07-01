---
name: MAST unit self-calibration design
description: Design (not yet implemented) for unit self-calibration from sky images — autofocus, optical center, thermal focus-seed, calibration invocation/status, pick-off stage geometry
type: project
---

Design agreed 2026-06-18 (Arie + Claude), **design only — no code written yet**. Per-unit self-calibration from night-sky images for a MAST unit (61cm f/3 parabola, PlaneWave/PWI4 mount, pick-off fiber feed to the central spectrograph). Covers autofocus, optical-center finding, thermal focus seed, the calibration invocation/status model, and pick-off stage geometry. All results persist in the per-unit config DB.

## 1. Autofocus (two-phase)
- **Phase A (acquisition):** may start wildly out of focus. Decide direction + magnitude of move to reach a usable near-focus regime. **Phase B (refinement):** sample several focuser positions to trace a V-curve; vertex = best focus.
- **Metric = HFD (half-flux diameter)**, not FWHM/Gaussian: the f/3 parabola has pronounced **coma**, so off-axis stars are asymmetric even at best focus. HFD stays well-behaved far from focus and is insensitive to PSF asymmetry. HFD is a **scalar** (diameter enclosing half the flux) — it does NOT itself give ellipticity.
- **Extreme defocus:** stars become donuts (central-obstruction shadow). Donut **diameter ≈ linear in focuser offset** (slope set by f-ratio + pixel scale), so one differential move calibrates the slope → jump near focus in ~one shot. Donut **size gives magnitude but not sign** (inside vs outside focus look identical) → need a differential move to disambiguate direction. Out there use **blob / connected-component / threshold detection**, not SEP point-source extraction (donuts large and may be blended); blob count + above-threshold area both rise toward focus = a crude "getting warmer" signal before HFD is meaningful.
- **Cold start:** if nothing extracts (sky-limited blur), step focuser in coarse increments until sources appear. **Known-good cached position** (from a prior success) skips most of Phase A → drop near focus, short confirming V-curve; fall back to full acquisition only if the cached point proves stale.
- Restrict the focus metric to **stars near the optical axis** (coma inflates off-axis HFD); before the center is known, use the **geometric image center** as stand-in (optical axis expected in the **middle third** of the image).

## 2. Optical center (coma elongation null)
- Coma elongates each star ~**radially**, magnitude growing **linearly with field radius** from the optical axis. Per star: a position + an **elongation vector** from SEP second moments (a, b, theta or cxx/cyy/cxy) — free in the extraction pass. Fit `e_i ≈ k·(r_i − r₀)` across the field; the **null where elongation vanishes = optical center r₀**.
- Coma is odd-order/asymmetric, so a pure ellipse second-moment slightly under-captures it. Also compute the **peak-to-flux-centroid offset vector** (coma shifts centroid off the peak, along the coma axis). Compute both estimators; they should agree on direction and cross-check.
- **Middle-third prior** regularizes the fit (initial guess + bounding box, outlier rejection).
- **Coupling / ordering:** clean coma signal lives **at best focus** (when defocused, donut shape is pupil-dominated, not coma). So: **focus first** (stars near geometric center) → at best focus measure the elongation field → locate optical axis → optionally re-focus using stars near the refined axis. One iteration usually enough.

## 3. Per-unit calibration outputs
Procedure yields, per mastNN: **optical-center pixel** on the detector, and a **known-as-good focus position**. These are persisted per-unit calibration state, each with **provenance + quality**: timestamp, unit, conditions, and a quality figure (V-curve fit residual + achieved HFD for focus; star count + elongation-null fit residual for optical center). Lets the hub/operator reject bad calibrations and detect drift.

## 4. Thermal focus-seed model
- Best focus is **temperature-dependent** (tube/focuser thermal expansion; WAO day-night swing large). **Mirror temperature is a better predictor than ambient** (mirror lags ambient with a long time constant; ambient-only dFocus/dT is noisy near evening cooldown).
- Relation is approximately **linear**: `seed = offset + slope·T_now` (dFocus/dT). Resist higher-order — sparse points overfit.
- **Training data:** rolling set of `(mirror_temp, best_focus, timestamp, quality)`, one appended per **successful autofocus** → self-calibrating, tracks slow drift. Robust / sigma-clipped fit. **Rolling window / recency weighting** so post-service points age out instead of fighting new state.
- **Maturity gate:** trust a slope only with ≥N points spanning ≥ΔT; below that, return the **most-recent good focus as a flat seed**.
- **Degradation ladder:** (a) mature model + temp available → predicted seed → short V-curve only; (b) temp `None` but recent good focus → flat seed (wider V-curve); (c) no model + no recent focus → full Phase-A. **Guardrail across all:** always take one image and check HFD is in the expected ballpark before committing; if wildly off, the seed is stale (focuser slipped / optics touched) → fall down the ladder.
- **Keying:** per `(unit, config-epoch)` — focus zero-point depends on optical config (filter, spectrograph feed, camera spacing). Stored schema should also carry **altitude** as an unused covariate (flexure), so residuals can later demand it without re-instrumenting.

## 5. get_mirror_temperature placeholder
- Stub returning `Optional[float]`; **`None` when no reading** → focus-seed model falls back to the bare (non-thermal) seed rather than guessing. **Never fabricate** a constant in the stub (to exercise the thermal path before hardware, temporarily return a fixed value). **Timestamp captured at the call site**, not in the stub — the calibration record carries `(temperature, read_time)` so a stale reading can be rejected.
- PWI4 source **CONFIRMED on a connected unit (PWI4 4.1.8, 2026-06-29):** temperatures are **NOT** in `/status` (no `temp` keys). They ARE served by **`GET http://localhost:8220/temperatures/pw1000`** (the general temp endpoint despite the "pw1000" name), returning lines `temperature.primary=<°C>` (primary **MIRROR**), `temperature.ambient=<°C>`, `temperature.secondary`, `temperature.m3`; **`-999.000` = no sensor** (this unit: secondary/m3 = -999; primary/ambient valid ~27 °C). Use `temperature.primary` as the mirror predictor (and `temperature.ambient` for comparison). The official `pwi4_client.py` has **no method** for this endpoint → add a one-line HTTP GET. (Supersedes the earlier "keywords unconfirmed" note.) Per-unit check: `curl -s http://localhost:8220/temperatures/pw1000`.

## 6. Declarative focus-required flag (Plan schema)
- The Plan carries **one bare boolean**. **Declarative**: `True` = "this observation must be in focus" (unit decides full run / seeded confirm / nothing); `False` = no focus requirement, observe as-is (NOT "forbid focusing"). A transient-followup Plan may set `False` to save time; a precise-transit Plan sets `True`.
- Because the flag is parameterless, everything defining "valid focus" lives **off-Plan as unit-level policy with site defaults**: tolerance, freshness window Δt, temperature window ΔT, max focus-time budget, slew-to-focus-field policy. Plan declares the goal; unit owns the definition of "valid" and the method. Time-budget cap is unit/scheduler policy (matters for time-critical transients — fail fast vs spend 10 min focusing).
- **Field selection:** plan-driven focus runs at/near the science field; if star-poor near the optical axis, fall back to a nearby star-rich field (costs slew time). Each plan-driven run also **feeds the thermal model** (self-calibration during normal ops).

## 7. Calibration invocation paths (all converge on one unit-local routine + `calibrating` activity)
1. **Manual** (operator-initiated).
2. **Plan-flag** — declarative, just-in-time, per-unit; fans out per-unit under multi-unit allocation (each focuses against its own calibration state; a unit that can't focus is unavailable → feeds quorum).
3. **Orchestrator nightly fleet-wide pass** at night-start — **declarative, best-effort**: primes every deployed unit so later plan flags resolve to cheap "already valid" confirms. Runs **after twilight / dark enough**, after covers open / tracking, before scheduling. At night-start "calibrate" bundles focus anchor + optical-center verify (+ future pointing/sky-quality).
4. **Auto-retry** (unit-side) at short intervals while not-operational.

## 8. Operational vs Activity status (two orthogonal axes)
- **Operational** — *can I be assigned science?* **Derived: `operational ⇔ why_not_operational is empty`** (no separate boolean to drift).
- **Activity** — *what am I doing now?* (idle, slewing, exposing, **calibrating**, parked…). A retrying unit = `{not-operational: not-calibrated} × {activity: calibrating}`; on success → `{operational} × {idle}`. Scheduler reads only the operational axis.
- **`why_not_operational`** is a list/set keyed by an **`owner:reason` prefix** (e.g. `safety:unsafe-sun`, `safety:weather-hold`, `calibration:not-calibrated`). Single owner per namespace; each owner **reconciles its whole slice idempotently** on its tick ("set `calibration:*` to [current reasons]") → no cross-owner stomping, no intra-owner races.
- **`calibration:not-calibrated` detail payload:** sub-reason (`no-stars` / `didn't-converge` / `focuser-fault` / `camera-error` / `stage-geometry`), last-attempt timestamp, consecutive-failure count, last error.
- **Definition:** `not-calibrated` = **no usable calibration exists at all** (never established a baseline this session). **Calibrated-but-stale stays operational** (lazy refresh is the plan flag's job); only **never-calibrated is not-operational** — otherwise units churn in/out of the pool mid-night.

## 9. is_safe gate (safety service: sun elevation + weather)
- `is_safe` is **necessary but not sufficient** for calibration: calibration precondition = **`is_safe AND dark-enough`** (a stricter sun threshold than safety — you can be safe to open at twilight while still too bright for SEP stars). Safety owns the first term, calibration owns the second.
- `is_safe` is also a **runtime interrupt**: a unit mid-calibration when it flips false **aborts and stows**. While unsafe, the unit is not-operational for an **environmental** reason and the retry loop naturally pauses, resuming when safe.
- **Give-up counter only increments on attempts that genuinely ran** (under `is_safe AND dark`) and still failed → cleanly separates "stuck on focuser fault" (fails under good conditions) from "waiting on weather" (defers, never faults).
- **v1: indefinite retry, no auto-escalation to `faulted`.** But **surface per-attempt failure detail in telemetry (Grafana)** so a genuinely broken unit isn't hidden behind a hopeful "calibrating". The structured sub-reason means a `faulted` state (fail K times under good conditions → stop retry, flag a human) can be added later with zero rework.

## 10. Pick-off stage geometry (fiber feed to spectrograph)
- A **45° folding mirror on a linear translation stage** in the converging beam either sits **retracted** (clear → clean full-field image, what all imaging/calibration needs) or **inserted** to pick the target's light into an **optical fiber → central spectrograph**. Imaging and fiber-feed are **mutually exclusive**. Stage is **1-D linear**; its path **may not follow the detector x-axis**.
- **Method:** take **3+ exposures** with the mirror at different stage positions; detect the shadow (roughly rectangular, graded/penumbral fringes, spanning between the horizontal edges); find the **longitudinal middle axis + its midpoint** in each. **Fit a line** through the midpoints (3+ for redundancy: averages centerline noise, exposes nonlinearity/backlash). Recovers **stage-axis orientation on the detector** (the "path not along x" term) + **stage→detector scale** → an affine map `stage position → centerline`.
- **What the shadow does NOT give:** where along the centerline the **fiber actually accepts light** (acceptance is tiny, conjugate to one point on the mirror). Shadow center is a proxy for the pickoff point only if the fiber looks exactly at the mirror center — not trustworthy to that precision. **True pickoff needs a flux-based fine calibration with a star.**
- **v1 = geometry only** (carries an uncharacterized fiber offset). Ideally store one **commissioning flux peak-up** constant (shadow-center→fiber offset) — still open-loop, and the exact slot the automated flux calibration drops into later.
- **Final on-fiber placement = stage (coarse) + mount-offset (fine):** stage sets the **sweep coordinate** (centerline through optical center); the **along-centerline residual** (optical center is 2-D, the fiber's along-centerline coordinate is fixed by mounting) is taken up by a **mount offset**; then peak on fiber flux (later).
- **Illumination regimes:** dawn/twilight flat → shadow is a **direct intensity drop** (primary, no reference needed). **Star fallback = differential ratio:** one **retracted in-focus reference** frame, ratio each mirror-in frame against it (stars under the penumbra are dimmed → pulls the rectangle out of a sparse field). Use **ratio not raw-difference** (twilight brightness changes within seconds); **register** the field (tracking moves stars). Sub-pixel **penumbra gradient fit**, not a hard threshold; approach every stage position from a **consistent direction** (backlash).
- **Quality gate:** line-fit residual + axis-orientation consistency → `calibration:not-calibrated` detail `stage-geometry`. No valid stage geometry → can't feed the fiber → no spectroscopy.
- **Stability:** mechanical, **stable night-to-night** → **per-mechanical-epoch** refresh (dawn periodically + after service), not a hard nightly gate.
- **Star-mode ordering dependency:** `focus → in-focus retracted reference → stage geometry`. **Reuse the focus run's final in-focus retracted frame** as the reference (no extra slew/readout). Dawn/flat regime has no such dependency.

## 11. Storage — per-unit config DB (MongoDB)
- **Each unit writes its own document directly** (single writer per record, no cross-unit contention — partitioned by unit). Make writes **atomic**, or stage then flip a **"current/valid" marker last**, so readers (including the unit's own operational gate) never see a half-written calibration.
- **Two write patterns, model distinctly:** geometric calibrations (optical center, stage axis/scale, fiber offset) = **replace-on-refresh versioned snapshots**; thermal focus-seed = **accumulating/rolling point set**.
- **Shared mechanical epoch id** across the geometric calibrations — bumps on service, **invalidates them as a group** (prevents fresh stage geometry paired with a stale optical center).
- **The operational gate reads this DB:** "is this unit calibrated?" = "does it have valid, in-epoch, fresh-enough entries?" — so `not-calibrated` is computed against DB contents, closing the loop between calibration values, provenance/quality, and operational status.

**Status:** design only, nothing implemented. Source: shared Claude conversation "Designing MAST unit features" (Arie, 2026-06-18).
