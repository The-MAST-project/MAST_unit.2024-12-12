---
name: Autofocus methods — research findings (HFD/V-curve/donut/thermal)
description: Deep-research validation of the HFD-autofocus plan choices; what's confirmed vs unvalidated, with citations
type: reference
---

Deep-research pass (2026-06-29) validating the [[self-contained-hfd-autofocus]] plan against established practice. 23/25 claims verified. Companion to [[optical-center-coma-research]].

**Verdict: Phase-1 HFD V-curve choices are strongly validated (20+ yr settled practice); the Phase-2 donut-jump and Phase-3 thermal pieces are NOT corroborated by surviving claims — treat as empirical/data-driven, not assumed.**

## Validated (high confidence)
- **HFD estimator** `HFD = 2·Σ(vᵢ·rᵢ)/Σ(vᵢ)` (v = pixel above background, r = dist to brightness centroid) is the de-facto production formula — origin **Weber & Brady** → FocusMax; also MaxIm DL, N.I.N.A. (via HFR), ASTAP. Exactly our formula.
- **HFD > FWHM/RMS**: a flux-weighted *integral* (not a peak fit) → robust to seeing/noise AND **stays defined on donuts** (peak/FWHM metrics degenerate on annular PSFs). MaxIm DL: "much more robust… can handle images so out-of-focus they look like donuts."
- **`D² = a·x² + b·x + c` IS the hyperbolic model**: true HFD-vs-position is hyperbolic `HFD=√(a x²+b x+c)`; squaring → our quadratic, best focus `x*=−b/2a`. Its asymptotes are the linear V-sides; rounds only near vertex (the "linear except near focus" carve-out). Use the **error-weighted** hyperbolic fit (N.I.N.A.'s default), not plain LSQ; a global straight-line fit is wrong near focus.
- **Coarse-then-fine + vertex** = MaxIm DL's exact architecture (coarse, then fine V-curve, best focus "at the center of the V"). Our two-phase design mirrors industry practice.
- **Sign ambiguity is real** (defocus = even-order Zernike → intra/extra-focal nearly identical to first order); established fix = a **paired intra/extra-focal (differential) measurement** — same principle LSST/Rubin (split sensors ±1 mm; Xin 2015) and DECam (donut pupil-Zernikes; Roodman 2014) use. Validates our differential-move sign disambiguation.

## Nuances to honor
- HFD is a **relative focus index, not absolute encircled-energy** (weighted-mean form is ~+6.4 % off true half-energy for a Gaussian). Fine for autofocus; don't read it as a physical diameter.
- **Low-SNR caveat:** HFD/HFR is sensitive to the assumed background and can go **negative** on faint stars / noise valleys → solid local background + a min-SNR cut (matters most at cold-start).
- Intra/extra-focal symmetry is exact only to first order; real spherical/coma on an f/3 makes the two donuts asymmetric — usable extra signal for sign.

## NOT validated — gaps (mostly Phase 2/3)
1. **Thermal (Phase 3):** *nothing* survived on temperature compensation — the linear `seed=offset+slope·T` and "mirror-temp better than ambient" are **unvalidated by this research** (sources were weak forums). Our plan to *learn* the relation from a rolling robust fit is the right hedge; mirror-vs-ambient predictor stays an open question.
2. **Donut slope (Phase 2 jump):** only qualitative donut/sign material survived — the **quantitative near-linear donut-diameter-vs-defocus slope** used to "jump near focus" did NOT. **Characterize empirically/optically on our system**, don't assume linearity/range.
3. **Donut detector:** "DoG beats LoG for blob detection" was **refuted (1-2)** → cold-start detector choice open (threshold/blob vs dedicated donut-radius estimator).
4. **Near-axis cutoff:** the specific coma-edge justification was **refuted (0-3)**; the near-axis-restriction *principle* still stands but the **cutoff radius is unquantified** → tunable parameter, calibrate empirically.
5. **Critical Focus Zone:** CFZ scales ~f-ratio² → **very tight at f/3**; compute it explicitly to set V-curve sample spacing and fine-step.

## Key sources
MaxIm DL Half-Flux (cdn.diffractionlimited.com/help/maximdl/Half-Flux.htm) · AAVSO HFD-vs-FWHM · FocusMax/CCDWare (ccdware.com/focusmax_overview) · N.I.N.A. autofocus docs · hyperbolic V-curve fit (lost-infinity; APT hyperbolicfitdll, arXiv:2201.12466) · LSST WFS **Xin et al. 2015** (arXiv:1506.04839) · DECam AOS **Roodman et al. 2014** (SPIE 9145) · primary origin **Weber & Brady, "Fast Auto-Focus Method and Software for CCD-based Telescopes."**
