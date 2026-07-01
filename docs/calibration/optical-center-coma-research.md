---
name: Optical-center-from-coma — literature & recommended improvements
description: Deep-research findings (2026-06-23) validating the coma-null optical-center method and the spin-1 / vector upgrades to adopt; key papers
type: reference
---

Deep-research pass (2026-06-23) on finding the optical center from pronounced coma, to validate/improve `src/imaging/optical_center.py`. Companion to [[mirror-shadow-optical-center-algorithms]] §D and design [[unit-self-calibration-design]] §2.

**Verdict: our coma-null approach is physically sound.** Canonical ref **Jarvis, Schechter & Jain 2008 (arXiv:0810.0027)**: coma at the prime focus of a parabola grows *linearly* with field angle, points *outward*, vanishes on-axis. Our 61 cm f/3 **bare** parabola is exactly their Fig. 4 case (corrected survey scopes cancel this).

**Biggest upgrade — coma is SPIN-1 (a vector), not spin-2 (ellipse).** Our major-axis-line / `cos 2θ` radiality is a spin-2 (headless) view. Coma has an unambiguous *outward direction* and *shifts the photometric centroid off the peak* (JSJ: "coma does move the centroid"; "the comatic PSF behaves like a vector"). So:
1. **Add the centroid-vs-peak estimator** — flux-centroid minus peak = a per-star vector pointing radially; the odd/third-moment signal an ellipse fit discards. This is the unimplemented half of our own design §2. Ma et al. 2008 (arXiv:0809.2954) fit third moments to "break the degeneracy between translations and rotations."
2. **Make the radiality gate spin-1** (`cos(θ−θ_radial)`, signed/outward) — strictly stronger than `cos 2(θ−θ_radial)`: separates real coma from a 180° flip and from uniform tracking smear.
3. **Don't assume the null is at frame center.** Field-CONSTANT coma = misalignment (Schechter & Levinson 2009, arXiv:1009.0708). Fit a vector field with a **free node/decenter**, separate the field-constant (misalignment) from field-linear (axis) term, and **report the decenter** as a per-unit calibration output. `middle_third` stays a prior only.

**Confounder rejection.** Aberrations have separable field signatures: coma linear/radial/spin-1 vs astigmatism quadratic/binodal "ovals of Cassini"/spin-2 (Noethe astro-ph/0111136). The research found **no dedicated published test** for coma-vs-tracking/field-rotation/seeing — our `mean(cos 2(θ−θ_radial))` radiality is judged "a reasonable home-grown analogue." Literature's fit-*validation* tool: **rho-statistics** (two-point correlation of residual elongation; Rowe 2010 / Jarvis 2016, still standard 2024). Weak-lensing PSF-field modeling is the same math (spin-2 ellipticity-field interpolation) → reusable: PSFEx, Euclid graph-Laplacian (Schmitz 2020 A&A 636 A78), Liaudat 2023 review (Frontiers).

**Correction:** do NOT justify third moments with "second moments can't distinguish coma from guiding/trefoil" — that claim was *refuted* in verification. Correct justification = coma's spin-1 vector nature + degeneracy breaking.

**Ranked code changes:** (1) add centroid-vs-peak spin-1 estimator + direction cross-check; (2) spin-1 radiality gate; (3) fit vector field with free node, report decenter; (4) rho-statistic residual check as fit quality.

**Scope caveat:** the most quantitative sources (NAT — Thompson/Schmid/Rolland; Kent spin-weighted Zernikes arXiv:1711.03916; Ma et al.) are TWO-MIRROR RC/Cassegrain or space telescopes; coma-physics transfers cleanly to our single-mirror parabola, the astigmatic-node/binodal alignment results transfer only as methodology.
