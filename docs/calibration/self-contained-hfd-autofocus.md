# HFD autofocus — phased plan (self-contained, parallel to ps3cli)

> **Status: PLANNED (2026-06-29; Phase 0 + near-axis emphasis added 2026-06-30).**
> Phase 0 (assess) + Phase 1 are the immediate scope (Phase 0's far→donut routing
> completes with Phase 2); Phases 2–3 are the roadmap. Archived approved plan.
> **Guideline (Arie):** the HFD implementation lives **in parallel** to the current
> ps3cli code (does not replace it); add a new endpoint `start_hfd_autofocus`.

## Context

The MAST unit already has a working V-curve autofocus (`src/autofocusing.py`): step
the focuser over N exposures (`FOCUSnnnnn.fits`, position in the name), hand the
files to PlaneWave's external **`ps3cli --server`** (best-focus + tolerance from a
quadratic V-curve over each image's **star RMS diameter**), persist
`known_as_good_position`. A second path delegates to PWI4's native autofocus.

Per our self-calibration design (`unit-self-calibration-design` §1/§4) we want a
**self-contained, coma-robust HFD (half-flux diameter)** autofocus: no external
ps3cli/catalog; a metric that tolerates the asymmetric PSFs an f/3 parabola
produces; robust from far out of focus; and temperature-seeded so most runs are a
short confirm. Built as a **parallel path** to ps3cli so the two can be A/B'd.

Architectural spine for all phases: **shared focuser sweep + pluggable analyzer**.
Factor the sweep/capture loop out of `do_start_autofocus`; `start_autofocus` keeps
the ps3cli analyzer, `start_hfd_autofocus` uses the HFD analyzer; both return the
same `PS3AutofocusStatus`. ps3cli (and its `app.py` launch) stay untouched.

## Entry point — start-position policy (cross-phase)

On `start_hfd_autofocus`, pick the starting regime from the config DB's cached focus
(`unit_conf.focuser.known_as_good_position`, already persisted today):
- **Known-good present/valid → V-curve directly** (Phase 1): center the sweep on it
  (`known_good ± (N/2)·spacing`) and refine. This skips coarse acquisition — the
  common, routine case.
- **Known-good absent → coarse acquisition first** (Phase 2, Phase A: cold-start
  coarse stepping → donut slope-jump) to reach near-focus, then hand off to the
  V-curve.
- **Regime confirmed by Phase 0:** the seed only chooses *where* to take the first
  exposure; **Phase 0 (Assess)** then classifies the actual frame and routes. A stale
  known-good that returns a wildly-off / donut frame is simply classified "far" and
  sent to acquisition — no special-case staleness guardrail needed.
- On success, write the new best focus back to `known_as_good_position` (existing
  behavior) so the cache stays current.

This is the simple precursor to the **Phase-3 thermal ladder**, which only refines the
*seed value* (mirror temperature) — the cache-vs-coarse branch is the same skeleton.
**Phasing note:** the "absent → coarse" branch needs Phase 2; until then, no-known-good
falls back to requiring an explicit `start_position` (today's behavior).

---

## Phase 0 — Assess (regime triage)  ← runs first, one cheap frame

Before any sweep, take **one exposure** at the seed position (from the start-position
policy — known-good if present, else a nominal mid-travel position) and classify the
focus regime from that single frame, then route:
- **Near focus** — point sources extract and the **near-axis** median HFD sits within a
  near-focus band (≲ a few × the in-focus HFD) → go straight to **Phase 1** (V-curve),
  centering the sweep here.
- **Far defocus (donuts)** — sources are large **annular blobs** (central dark hole) /
  HFD or above-threshold blob area very large → **Phase 2** donut acquisition, then Phase 1.
- **Nothing extracts** (sky-limited blur / wild defocus) → **Phase 2 cold-start** coarse
  stepping until structure appears.

Classification signals, all from the one frame: extracted point-source count + median
**near-axis** HFD vs a near-focus threshold; donut/annular morphology (central minimum)
or large above-threshold blob area. This **subsumes the staleness guardrail** — a stale
known-good seed just classifies as "far" and routes to acquisition.

New piece: `assess_focus_regime(image) -> {"near"|"far"|"empty"}` in `src/imaging/hfd.py`
(reuses `frame_hfd` + a blob/annulus check), called by the sweep helper to pick V-curve
vs acquisition. **Phasing note:** the **near vs not-near** decision is implementable in
Phase 1 (route "near"→V-curve, else report "not near focus" / fall back to an explicit
`start_position`); the **far→donut / empty→cold-start** routing lands with Phase 2.

---

## Phase 1 — Self-contained HFD V-curve (near-focus), parallel path  ← BUILD NOW

**Key benefit of parallel:** analysis is decoupled from capture (position is in the
filename), so **both analyzers run on the SAME captured sweep** — identical star
field, no extra telescope time — for an apples-to-apples HFD-vs-ps3cli comparison.

### New `src/imaging/hfd.py` — the metric
Reuse the photutils detection pattern from `src/imaging/optical_center.py`.
- `half_flux_diameter(stamp, cx, cy, r_out)` — per star, background-subtracted,
  negative-clamped: $\text{HFD}=2\,\dfrac{\sum_i v_i r_i}{\sum_i v_i}$ within `r_out`.
- `frame_hfd(image, ...) -> (hfd_median, n_stars)` — detect sources; **restrict to
  near-axis stars and AVOID THE IMAGE MARGINS** where coma is pronounced, via
  `near_axis_frac` about the geometric center (optical center unknown during focus);
  robust median; reject saturated/blended/too-small. Rationale: in this f/3
  coma-driven system coma is a **focus contaminant** — it inflates and asymmetrizes
  off-axis HFD — so we use the well-behaved central stars. This is the **deliberate
  opposite** of `find_optical_center`, which selects *margin* stars because there coma
  is the signal, not noise.

### New `src/focus_analysis_hfd.py` — the analyzer (parallel to `focus_analysis.py`)
`analyze_focus_files_hfd(files, ...) -> PS3AutofocusStatus`, **reusing the existing
Pydantic models** so it drops into the orchestrator (`focus_analysis.py` untouched):
parse position from `FOCUSnnnnn` (fallback `FOCUSPOS` header) → `frame_hfd` per file
→ fit $D^2=ax^2+bx+c$ via `np.polyfit(x, D**2, 2)` (linear LSQ; matches `vcurve_a/b/c`),
require $a>0$, best $x^\*=-b/2a$, $D_{\min}=\sqrt{c-b^2/4a}$, tolerance
$\Delta x=\sqrt{D_{\min}^2((1+f)^2-1)/a}$.

### Plumbing (additive)
`start_hfd_autofocus` endpoint (`src/unit.py`), `UnitActivities.HfdAutofocusing`
(`src/common/activities.py`), HFD config block in `AutofocusConfig`
(`src/common/config/unit.py`; MAST_common submodule → sync). `src/app.py`/ps3cli
unchanged.

**Refinements (validated by autofocus-methods-research):** the `D²=ax²+bx+c` fit *is*
the standard hyperbolic model — keep it, but use an **error-weighted** fit (N.I.N.A.'s
default), not plain LSQ. HFD is a **relative focus index**, not absolute encircled-energy
(don't treat it as a physical diameter). **Low-SNR caveat:** HFD can go **negative** on
faint stars / noise valleys → require a solid local background + a min-SNR cut (matters at
cold-start). Compute the **Critical Focus Zone** (CFZ ∝ ~f-ratio², so *tight* at f/3) to set
the V-curve `spacing` and fine step. `near_axis_frac` is a **tunable** to calibrate
empirically (the off-axis-coma cutoff is not quantified in the literature).

**Files:** new `src/imaging/hfd.py`, `src/focus_analysis_hfd.py`; additive edits to
`src/autofocusing.py` (shared-sweep helper + `do_start_hfd_autofocus`), `src/unit.py`,
`src/common/activities.py`, `src/common/config/unit.py`.

**Verification:** (1) **A/B on the same files** — extend `tests/autofocus/validate_autofocus_solve.py`
to run both analyzers over real `FOCUS*.fits`, compare best-focus/tolerance;
(2) synthetic Gaussian-defocus sequence recovers the injected vertex; (3) HFD rises
off-axis (coma) confirming the near-axis cut; (4) manual end-to-end `start_hfd_autofocus`
with ps3cli still available.

---

## Phase 2 — Far-from-focus robustness (two-phase acquisition)  ← LATER

Phase 1 assumes you start **near** focus (point sources extractable). Starting wildly
out of focus, stars are large **donuts** (central-obstruction shadow) or sky-limited
blur, and point-source HFD breaks. Add a **Phase A (acquisition)** ahead of the
Phase-1 V-curve (which becomes **Phase B, refinement**).

- **Donut regime metric (not SEP point sources):** detect **blobs** via
  connected-components/threshold (donuts are large and may blend). The donut **outer
  diameter ≈ linear in focuser offset** (slope set by f-ratio + pixel scale). One
  **differential focuser move** calibrates the slope → jump to near-focus in ~one
  shot. Donut size gives **magnitude but not sign** (inside vs outside focus look
  identical) → the differential move **disambiguates direction**. "Getting warmer"
  signal: blob count + above-threshold area both rise toward focus.
- **Cold start:** if nothing extracts at all (sky-limited blur), step the focuser in
  **coarse increments** until blobs/sources appear, then enter the donut regime.
- **Hand-off A→B:** when point sources become extractable and HFD is measurable,
  switch to the Phase-1 HFD V-curve for the precise vertex.
- **New pieces:** a donut/blob metric in `src/imaging/hfd.py` (or a `focus_metrics`
  module); an adaptive Phase-A controller in the sweep helper (direction-finding +
  coarse stepping) that precedes the fixed N-step V-curve. Phase B reuses Phase-1
  unchanged.
- **Research caveats (autofocus-methods-research):** the qualitative donut picture and the
  **differential intra/extra-focal sign disambiguation are validated** (same principle as
  LSST/DECam). BUT the **quantitative donut-diameter-vs-defocus slope is NOT corroborated** —
  do not assume linearity/range; **characterize it empirically/optically on our system**
  before relying on a one-shot jump. The **blob-detector choice is open** (the DoG>LoG claim
  was refuted) — evaluate threshold/connected-components vs a dedicated donut-radius estimator
  on real donuts. (Spherical/coma asymmetry between the two donuts can *help* the sign.)
- **Verification:** synthetic donut sequences (annuli whose diameter ∝ |offset|) →
  the slope-calibration move lands near focus and the sign is resolved; cold-start
  test from a blank/blurred frame finds the regime; **measure the real slope** on a
  captured intra/extra-focal series before trusting the jump.

---

## Phase 3 — Thermal focus-seed model  ← LATER

Best focus drifts with temperature (tube/focuser expansion; large WAO day-night
swing); **mirror temperature predicts better than ambient** (mirror lags ambient).
Goal: seed each run so most are a short confirming V-curve, not a full sweep.

- **Model:** `seed = offset + slope · T_mirror` (linear; resist higher-order — sparse
  points overfit). **Robust/sigma-clipped** fit over a **rolling, recency-weighted**
  training set of `(mirror_temp, best_focus, timestamp, quality)` — one point
  **appended per successful autofocus** (Phase 1/2), so it self-calibrates and tracks
  drift. **Keyed per `(unit, config-epoch)`**; also store **altitude** as an
  (initially unused) covariate for later flexure analysis.
- **Maturity gate:** trust the slope only with **≥N points spanning ≥ΔT**; below that
  use the **most-recent good focus as a flat seed**.
- **Degradation ladder:** (a) mature model + temp → predicted seed → short V-curve;
  (b) temp `None` but recent good focus → flat seed (wider V-curve); (c) neither →
  full Phase-A acquisition. **Guardrail across all:** take one image first and check
  HFD is in the expected ballpark; if wildly off, the seed is stale → fall down the
  ladder.
- **`get_mirror_temperature() -> Optional[float]`:** stub returning `None` when no
  reading (never fabricate a constant); **timestamp captured at the call site** so a
  stale reading can be rejected. **PWI4 source CONFIRMED on-unit (PWI4 4.1.8, 2026-06-29):**
  temps are **NOT** in `/status`; query **`GET http://localhost:8220/temperatures/pw1000`**
  (the general temperature endpoint, despite the name), which returns
  `temperature.primary` (primary MIRROR), `temperature.ambient`, `temperature.secondary`,
  `temperature.m3` as `temperature.<sensor>=<°C>` lines, with **`-999.000` = no sensor →
  map to `None`**. Read `temperature.primary` for the mirror seed and `temperature.ambient`
  for the mirror-vs-ambient comparison. The official `pwi4_client.py` has no method for this
  endpoint → add a one-line HTTP GET.
- **Persistence:** per-unit config DB as an **accumulating/rolling point set**
  (distinct from replace-on-refresh geometric calibrations like optical center).
- **Research caveat (autofocus-methods-research):** literature support for a *linear*
  focus-vs-temperature law and for "mirror temp beats ambient" was **not corroborated**
  (thin, forum-grade sources). So treat the linear form as a prior, **lean on the
  data-driven rolling fit** (it self-calibrates from real runs and can be upgraded if
  residuals demand), and keep **mirror-vs-ambient as an open question** — log both
  (ambient is readable via EFA; mirror via PWI4 `status.raw`) and let the data pick the
  better predictor.
- **Verification:** feed a synthetic `(T, best_focus)` history → the robust fit
  recovers the slope and the maturity gate / ladder behave; with `get_mirror_temperature`
  returning `None`, the ladder falls back cleanly; once real data accrues, compare the
  predictive power of mirror vs ambient temperature.

---

## Phasing rationale
Phase 0 triages one cheap frame to route near→V-curve vs far→acquisition (avoiding a
blind sweep). Phase 1 delivers an offline, coma-robust analyzer A/B-able against ps3cli
today. Phase 2 makes it reliable from any starting defocus. Phase 3 makes routine runs
cheap and drift-aware. Each phase stands alone and slots into the same pluggable-sweep
spine.
