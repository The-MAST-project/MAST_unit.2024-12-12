# Optical-center-from-coma — design/methods document for the PI

> **Status: DELIVERED (2026-06-29).** Produced `docs/optical_center_design.md`
> (+ `docs/figures/coma_field_0002.png`, `radial_consistency_0002.png`) and a
> self-contained `docs/optical_center_design.pdf` (7 pp, math + figures rendered)
> in the MAST_unit repo. This is the approved plan, archived for reference.

## Context

The user (MAST) wants a written document to present to their Principal Investigator (a PhD in optics for astrophysics instruments). The PI must be able to evaluate the **academic basis** of the design and methodology, the **approach/design** itself, and the **literature sources** we used. The subject is the implemented `find_optical_center` (`src/imaging/optical_center.py`) — locating the optical axis on the detector from the coma in a single sky image of the 61 cm f/3 **bare parabola**.

User choices: markdown in the repo; lead with the **optics basis + equations**; include the methodology, a **code-level walkthrough**, **example figures**, and the cited sources; add a caveat that we have **not yet run on our full range of images**.

## Deliverable

**`docs/optical_center_design.md`** (create `docs/` if absent) plus **`docs/figures/`** for the example images. GitHub-rendered LaTeX (`$...$` / `$$...$$`). Written for an optics PhD — rigorous, concise, citation-backed.

## Document outline

**1. Purpose & optical regime (short).** Per-unit optical-axis calibration from night-sky images. The unit is an uncorrected prime-focus paraboloid (f/3), so off-axis **coma dominates** — exactly the regime of Jarvis–Schechter–Jain 2008 Fig. 4 (correctors in survey scopes cancel it; we have none).

**2. Optical basis (with equations).**
- Third-order **Seidel coma** wavefront $W_{\text{coma}} = a\,H\,\rho^{3}\cos\phi$ (field height $H$, pupil radius $\rho$, azimuth $\phi$): amplitude **linear in field angle**, vanishing on-axis. Transverse coma blur grows $\propto H/(f/\#)^2$ → strongest at the margins.
- The comatic PSF is **radial** (the flare points along the line from the field point through the optical axis) and is a **spin-1 vector** (amplitude + unambiguous outward direction; adds by vector rules) — *not* the spin-2 (headless) ellipticity. State the spin-1/spin-2 distinction explicitly (coma = spin-weight 1, astigmatism = spin-weight 2).
- Image moments. Second moments $Q_{ij}=\frac{\int I\,(x_i-\bar x_i)(x_j-\bar x_j)\,d^2x}{\int I\,d^2x}$; ellipticity $(e_1,e_2)=\frac{(Q_{xx}-Q_{yy},\,2Q_{xy})}{Q_{xx}+Q_{yy}}$, major-axis angle $\theta=\tfrac12\operatorname{atan2}(e_2,e_1)$ — a **spin-2** proxy for the coma direction (degenerate to $180°$). The **odd/third-moment** signature — the flux **centroid shifts off the peak**, $\mathbf{o}=\mathbf r_{\text{centroid}}-\mathbf r_{\text{peak}}$, pointing radially outward — is the genuine **spin-1** estimator that breaks the degeneracy.
- Briefly: misalignment adds a **field-constant** (decentering) coma on top of the field-linear Seidel term (Nodal Aberration Theory), so the on-axis null is the right observable but a residual constant term flags misalignment.

**3. Methodology / design.**
- **Null-finding principle**: every star's coma axis points (anti-)radially through the optical center; recover the center as the point the major-axis lines best pass through.
- **Weighted least-squares intersection** (normal-equation form, numerically stable for all orientations): with line normals $\mathbf n_i=(-\sin\theta_i,\cos\theta_i)$, minimize $\sum_i w_i\,[\mathbf n_i\cdot(\mathbf p-\mathbf p_i)]^2 \Rightarrow M\mathbf p=\mathbf v$. (This is `_solve_center`.)
- **Weighting** $w_i=\text{flux}_i\cdot e_i$: orientation uncertainty scales $\sim 1/(\text{SNR}\cdot e)$, so bright, elongated margin stars carry the signal.
- **Margin selection** (`min_field_radius`): coma $\propto H$ → use off-axis stars; **iterative sigma-clipping** for robustness.
- **Confirmation metrics**: spin-2 radiality $\langle\cos 2(\theta_i-\varphi_i)\rangle$ and spin-1 radiality $\langle\cos(\theta_{cp,i}-\varphi_i)\rangle$ about the fitted center ($\varphi_i$ = radial angle); $+1$ = radial coma, $0$ = random, $-1$ = tangential (trailing/field rotation). Spin-1 is the cleaner confirmation; the ellipse is the better locator (centroid-peak offsets are too small to fit positions) — hence **ellipse fits, spin-1 gates**.

**4. Code-level walkthrough.** Map the methodology onto `src/imaging/optical_center.py`, function by function, so the PI can audit the realization:
- `find_optical_center(image, ...)` pipeline, numbered as in the code: (1) load; (2) `Background2D` + `MedianBackground` background subtraction; (3) `detect_threshold`/`detect_sources` (photutils segmentation, with `exclude_mask` for the folding-mirror shadow); (4) `SourceCatalog` → per-source `xcentroid/ycentroid`, `orientation` (θ), `ellipticity` (e), `segment_flux`, `maxval_xindex/yindex` (peak, for the spin-1 offset); (4a) source filtering (`min_area/max_area`, `min_ellipticity`, finite, `min_field_radius` margin cut); (5) weighted iterative fit; (6) quality gates.
- `_solve_center(x, y, theta, weights)` — the normal-equation least-squares intersection; explain the normal-vector form and why it replaced the `tan θ` form (blew up near vertical axes).
- `_coma_radiality(...)` — spin-2 vs spin-1 metrics; the gate falls back to spin-2 when too few resolved centroid-peak offsets exist.
- Key parameters and their physical meaning (`min_field_radius`, `min_ellipticity`, `clip_sigma`, `min_sources`, `min_radiality`) and the `OpticalCenterResult` fields (center, `radiality`, `radiality_spin1`, `residual_rms`, provenance arrays). Short, faithful code excerpts (signatures + the normal-equation lines), not full listings.

**5. Example figures.** Generated from a clean-coma real frame (e.g. `Samples1/0002`, which gave spin-1 radiality ≈ 0.91) and committed to `docs/figures/`:
- (a) **Coma elongation field** — quiver of major-axis directions over the (zscale) image with the fitted optical center marked and the geometric center for reference; visually shows the radial pattern converging on the axis.
- (b) **Detected/used sources + center** — sources kept by the margin/ellipticity cuts, color-coded inliers vs clipped, with the fitted center.
- (c) optional **cross-section / per-frame radiality** annotation (spin-1 vs spin-2 values in the caption).
Captions state the frame, source counts, and radiality. Reuse the plotting already prototyped (quiver + center overlay); render headless (Agg) to PNG.

**6. Status & limitations (honest, brief).** Method grounded in the literature and exercised on a limited set of real frames; **not yet validated across our full range of images** (no confirmed retracted/in-focus calibration frame yet). Per-frame center scatter (~hundreds of px on the frames seen) is fit noise on a fixed quantity → the per-unit center is best obtained by aggregating frames. Constant-coma (misalignment) separation and rho-statistic residual validation are identified next steps.

**7. References (the sources).** Cited list with arXiv links:
- **Jarvis, Schechter & Jain 2008** (arXiv:0810.0027) — coma is a spin-1 vector, linear in field angle, radial, vanishing on-axis; centroid shift. *Primary basis.*
- **Ma, Bernstein, Weinstein & Sholl 2008** (arXiv:0809.2954) — fitting third moments breaks the translation/rotation degeneracy; field-dependent PSF from misalignment+jitter.
- **Schechter & Levinson 2009** (arXiv:1009.0708) — misalignment → field-constant decentering coma.
- **Noethe** (arXiv:astro-ph/0111136) — aberration field dependence; binodal astigmatism.
- **Kent 2018**, PASP (arXiv:1711.03916) — spin-weighted Zernikes (coma spin-1, astigmatism spin-2).
- **Rowe 2010 / Jarvis et al. 2016** — rho-statistics (residual PSF-ellipticity correlation), the weak-lensing validation tool.
- (Context) **Liaudat et al. 2023**; **Schmitz et al. 2020** (A&A 636 A78) — PSF-field modeling/interpolation.

Note in the doc which sources directly ground the coma method (JSJ 2008, Ma 2008, Schechter-Levinson 2009) vs. methodological context (NAT/Noethe/Kent, weak-lensing rho-stats).

## Verification

1. Generate the figures: run `find_optical_center` on the chosen frame(s) headless and save PNGs to `docs/figures/`; confirm the quiver visibly converges on the marked center and the radiality values in captions match the run.
2. Render-check the markdown (GitHub math): equations display; the spin-1/spin-2 distinction is stated correctly; the code-walkthrough excerpts match the current source; every claim mirroring a paper is attributed; the "not yet run on our full image range" caveat is present; figures resolve and are referenced.
