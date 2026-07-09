---
name: Folding-mirror shadow detection (implemented)
description: Detection / marking / centerline / darkening of the pick-off folding-mirror shadow, implemented in src/imaging/mirror_shadow.py
type: project
---

Implemented 2026-06-23 (Arie + Claude) in `src/imaging/mirror_shadow.py` (MAST_unit repo) — the
pick-off folding-mirror shadow handling from [[unit-self-calibration-design]] §10, now actual code (that doc is design-only).

Algorithmic logic & rationale (deficit map, projection sweep, band-excluded fill, coma-null fit, shadow↔optical-center coupling): [[mirror-shadow-optical-center-algorithms]].

**Model:** `ShadowModel` — a tilted band: `angle` (long-axis, rad), `offset` (perp distance from image center), `umbra_half_width`, `penumbra_half_width`, `depth`, `prominence`. The "long centerline" Arie asked for is just `(angle, offset)`; `.centerline_endpoints()`, `.masks()` (umbra/penumbra), `.mask()` (union) derive from it.

**API:** `detect_mirror_shadow(image, reference=None, ...) -> ShadowModel` (always returns; check `.present`, so retracted-stage frames read as absent); `darken_shadow(image, model, fill=None, ...)` replaces the whole band with interpolated sky — kills the dip AND the bright-star leak-through ghosts (mirror is not fully opaque), i.e. the "mask before the pipeline" step; `plot_shadow(...)`.

**Approach — each piece forced by real-data testing on `D:/MAST/tmp/Samples/sample1.fits`, which DOES contain a near-vertical shadow:**
- deficit = `clip(1 - image/illum, 0, None)`; illum = sigma-clipped low-order 2D poly. A *local* `Background2D` (the old `find_vertical_obstruction.py` approach) would track the wide band and erase it.
- orientation via projection sweep: bin deficit onto the band-normal at each candidate angle, score the trough's prominence. Full ±90° sweep (band orientation unknown — "usually not vertical" but can be), run on a decimated map for speed (~4 s vs ~37 s on the 3923×2771 frame).
- gates that killed false positives: min penumbra half-width (real band is wide — rejects a 2-px bad-column / poly-edge sliver) + edge margin (centerline must cross the field, not hug the edge where the poly overshoots).
- centerline = midpoint of the penumbra-to-penumbra run, NOT argmax (the cross-section is often saddle-shaped).
- `darken_shadow` refits the poly with the band EXCLUDED (`_fit_illumination(exclude_mask=)`) so it interpolates true sky across the gap, then trims fill level + draws fill noise from a thin collar hugging the band. Verified: in-band max 8568→3808 (ghosts gone), level+noise match sky, no seam.

**Open items:** (1) Validated for DEEP shadows only (15 mirror-in frames, depth 0.25–0.44). **CONFIRMED BUG: the order-2 illumination poly absorbs WIDE shadow bands → false negatives for moderate shadows.** The 19 `Samples1/` frames each have a real right-side shadow (true depth ≈0.084) the detector reported `clear` (≈0.006); `full-frame.fits` similarly (~4.4%). So the clear path is still UNvalidated (no confirmed retracted frame) and the detector is trustworthy only for deep (≳0.15) shadows. Fix prototyped (star-suppressed image + detrended projection recovers 0.084) but orientation handling unfinished — NOT implemented. See [[mirror-shadow-optical-center-algorithms]] §F.
(2) Optical center: coma IS present (was a false "no coma"); fixed by flux×ellipticity weighting + `middle_third` default False — recovers center on 19 `Samples1` frames (radiality 0.58–0.91). See §G. (2) `reference=` ratio mode (retracted frame) is the cleaner illumination path if the pipeline can supply one at masking time — TBD whether masking must be single-image. (3) thresholds tuned on a single frame.

Supersedes the earlier sketches `src/science/find_vertical_obstruction.py` (column sums → vertical only) and the `mask_linear_shadow` half of `src/tools/vigneting/vignetting.py` (argmin X-profile → vertical only).
