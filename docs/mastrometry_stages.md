# Mastrometry stages: plate solving that knows about the fold-mirror shadow

*Status, 2026-11-20: working and measured on 37 real frames from two units (mast01 and mast02), **pushed, not merged,
off by default**. MAST_unit branch `solver-source-selection` (on top of PR #409), MAST_common branch
`solving-source-selection-config`. Every number below comes from real MAST frames, full frame, solved on mast08. What we
have not done yet, and what the numbers cannot tell us, is in [section 8](#8-what-we-do-not-know-yet).*

## 1. Summary

`MastrometryDotNet` solves a frame with astrometry.net and reports the sky position at the fibre. We added two things
in front of astrometry.net and made them switchable, as three **stages**:

| Stage | What astrometry.net is given |
|---|---|
| **original** | the frame as it always was (the default) |
| **shadowed** | the same frame, with the fold mirror's shadow detected and its band darkened |
| **selective** | a list of about 40 chosen stars, outside the shadow and away from trouble, solved as an xylist |

On 37 real frames (mast01: 6; mast02: 31, from production acquisitions and a mirror-map sweep), measured as the median
distance of the matched stars from their catalogue positions, after the solve (the median over frames of each frame's
median):

| | original | shadowed | selective |
|---|---|---|---|
| frames solved | 36 of 37 | 36 of 37 | 36 of 37 |
| stars used by the fit (median) | 208 to 274 | 154 to 207 | 22 to 30 |
| median residual, mast01 (6 frames) | 0.63″ | 0.50″ | **0.25″** |
| median residual, mast02 (31 frames) | 0.48 to 0.52″ | 0.38 to 0.41″ | **0.11 to 0.17″** |
| time per frame, whole frame (median) | 5.7 s | 32.7 s | 28.3 s |

The one frame not solved is the same in all three stages (a 2 s exposure). The shadowed stage is better than the
original on every set (by 20 to 21%), and the selective stage is 2.5 to 4.7 times better. **That is the agreement of a
solution with its own stars, not its accuracy**: we have no ground truth yet (see [section 8](#8-what-we-do-not-know-yet)).
The next stage is an on-sky test with a Gaia star on the fibre. The time is spent in Python (shadow detection, then
darkening or star selection), not in astrometry.net; it is broken down in [section 7.3](#73-where-the-time-goes).

## 2. Motivation

**What mastrometry does today.** For the `sky` and `spec` phases the unit crops the frame to a region of interest,
bins it 2×2 (0.2616″ becomes 0.5232″ per pixel), and hands the image to astrometry.net, which extracts its own sources
(about 410 on a 10 s frame), matches them to the index and fits a WCS. The reference pixel (CRPIX) is the ROI's centre.
For the `spec` phase the guiding ROI is centred on the **fibre pixel**, so the solved RA and Dec are the sky at the
fibre; for the `sky` phase it is the sky ROI's centre. The acquisition corrects against that position.

**The problem: the fold mirror's shadow.** The pick-off mirror throws a shadow on the camera, and where it falls depends on where
the stage puts the mirror:

- with the mirror in (`spec`), a band about 4000 px wide across the middle of the frame, 50% deep;
- at the `sky` position, the mirror's edge is at a vertical edge of the frame: a partial band, up to 65% deep.

Stars in the shadow are dimmed, and astrometry.net uses them like any other. In the first frame we measured (a `spec`
frame), 38 of the 210 stars used were in the umbra and 41 in the penumbra, and the umbra stars had **twice the median
residual** of the whole (1.55″ against 0.79″). astrometry.net fits an order-2 distortion to all of them.

**The second problem: what we feed the solver.** The planned in-house guiding (correlating star positions, to disentangle
from PHD2) will need a reliable list of stars anyway: unsaturated, unblended, not in the shadow, centroided in a way
that does not depend on how bright or comatic a star is. MAST has no field corrector, so coma is at full strength, and
stars toward the periphery are wedge-shaped (a head and a tail), as the pictures below show. A list that is chosen,
rather than whatever astrometry.net extracts, is the first step toward that.

**What we wanted to learn:** does removing the shadow's influence (shadowed), and feeding the solver a curated list
(selective), give a steadier and more accurate pointing at the fibre than what we do today?

## 3. The three stages

The mode is `solving.source_selection.mode` in the unit's configuration (MAST_common `SourceSelectionConfig`). It
defaults to `original`, and nothing changes until a unit sets it.

```
original :  frame -> crop to ROI -> bin 2x2 -> solve-field on the image

shadowed :  frame -> crop to ROI -> [fill hot pixels] -> [detect the shadow] -> [darken its band]
                  -> bin 2x2 -> solve-field on the image

selective:  frame -> crop to ROI -> [fill hot pixels] -> [detect the shadow] -> [choose the stars]
                  -> write an xylist -> solve-field on the xylist (order 1)
                                          '-- if that fails: bin 2x2 -> solve-field on the image

[ ] = added by this work
```

| | original | shadowed | selective |
|---|---|---|---|
| hot pixels filled (if mapped) | no | yes | yes |
| shadow detected | no | yes | yes |
| shadow darkened | no | yes (umbra and penumbra) | no: it only rejects sources |
| who picks the stars | astrometry.net's `image2xy` | the same, on the darkened image | our `select_sources` |
| fit order | astrometry.net's default (2) | 2 | **1** |
| failure | the frame fails | the frame fails | falls back to the image solve; fewer than 8 chosen stars fails the frame |

**Order 1 is a decision.** What we care about is the accuracy at the fibre, where CRPIX is, not the distortion toward
the corners. Order 1 does not extrapolate a distortion fitted on central stars to the corners. It costs about 0.5″
against order 2 on the same stars at CRPIX = frame centre (section 6), and the on-sky test will tell which is closer.

## 4. What was added to the original mastrometry

### 4.1 The original path, for reference

`MastrometryDotNet._solve_in` reads the full frame, crops it to the phase's ROI, bins it 2×2 by block mean, writes it as
`downsampled_<frame>.fits`, and runs `solve-field` (Cygwin) on it with the options in [Appendix A](#appendix-a-the-astrometrynet-call).
It parses the output, takes RA and Dec from the solved FITS's CRVAL, keeps the CD matrix, and hands the artifacts to
the mover. All of that is unchanged. The additions are in front of the binning and around the solve.

### 4.2 The shadow: detection and darkening

The unit now detects the shadow in the frame itself, so it is found wherever the stage left the mirror, whatever the
phase. A real frame with a shadow in the middle (mast04, 10 s), as the detector sees it:

![The shadow on a centred-shadow frame](mastrometry_stages/selection_centred_shadow.jpg)

*The umbra (purple, ±1400 px) and penumbra (blue, ±2168 px) are drawn from the detector's model, with the stars the
selection kept (green circles). The yellow lines mark the band's tilt and the distances from its centre. The text
above is the survey's summary line: 3406 detections, 53 kept. This is the survey tool's own picture, not the unit's.*

And a frame at the `sky` position, with the shadow's edge inside the frame and the rest of it outside (mast03, 3 s):

![A partial shadow at the right edge](mastrometry_stages/selection_partial_shadow.jpg)

*Half the band is outside the frame, so what is visible is a penumbra (blue) leading to the umbra (purple), and the
far penumbra at the very edge. All 31 chosen stars are outside it.*

In the shadowed stage the band is then replaced by an estimate of the sky that should be there:

![Before and after the darkening](mastrometry_stages/fig_darkening.jpg)

*Left: the frame as astrometry.net receives it today. Right: the band filled. The stars in it are gone, so astrometry.net
cannot use them. The detector and the filler are described in [section 6](#6-the-shadow-detector-and-the-darkener).*

### 4.3 The hot pixel mask

A hot pixel is at the same sensor pixel in every frame of one camera, so it is found by persistence across many frames
(pixels that are isolated spikes over the local sky in at least 80% of them) and stored per camera on the share:
`MAST/<unit>/Calibration/hot_pixels.json`. The shadowed and selective stages fill them with the median of their
neighbours before anything else, so that a hot pixel in a star's window does not drag its centroid.

| Unit | Hot pixels | Built from | |
|---|---|---|---|
| mast00 | 45 | 17 frames | |
| mast01 | 131 | 24 frames, 30 Oct to 16 Nov, 4.5 to 5.3 °C | checked on independent frames: 46 of 47 found there are in it |
| mast02 | 137 | 12 frames, 15 to 17 Nov, ~5 °C | |
| mast03 | 101 | 24 frames | |
| mast04 | 12 | 14 frames at **32 to 48 °C** | the camera uncooled: needs a rebuild at its operating temperature |
| mastw | 24 | 24 frames, 3 recent nights | no temperature in its headers |

**A map belongs to its period.** On mast02 the 24 frames of September, October (uncooled, 28 to 30 °C) and November
(5 °C) held three populations whose hot pixels do not overlap at all, so no pixel was persistent in the mixture and the
first map came out empty. The build tool uses frames at the set temperature, can be limited to the most recent
nights, and warns when many pixels persist in half the frames but none in 80%.

**The effect is small.** 131 pixels in 46.8 million touch about 0.2% of the stars (1 of 700 to 1900 detections had one
in its centroid window, and the centroids moved by 0.05 px or less). The mask guards the rare star. It does not change
the results below.

### 4.4 The source selection and the xylist route

The selective stage chooses the stars ([section 5](#5-the-star-selection-in-detail)), writes them as an xylist (a FITS
table of X, Y and FLUX, brightest first, on the binned grid) and runs `solve-field` on that instead of on the image.
Here is the same frame solved by the original stage and by the selective stage, as the unit's own star map (the shadowed
stage, and a frame with a centred shadow, are in [section 7](#7-results)):

![Star map, original stage](mastrometry_stages/margin_original_stars.jpg)

*Original stage: astrometry.net extracted 410 sources (orange +) and used 207 (blue circles). Many are in the shadow
on the right. The residual RMS is 1.57″.*

![Star map, selective stage](mastrometry_stages/margin_selective_stars.jpg)

*Selective stage, same frame (mast01 `sky` frame, 10 s): 42 chosen stars (orange +), 27 matched and used (blue
circles). All are outside the shadow and near the fibre (the white +). The residual RMS is 0.28″.*

**Reading the star map.** Orange + is a source that was **extracted**: the list handed to the solver (our 42 chosen
stars in the selective stage; astrometry.net's own 410 in the original). A blue circle is a star the solution **used**,
matched to a catalogue star. An orange + without a circle was handed over and not matched. The circle's radius runs from
9 to 30 display pixels and is **relative within each map**: it is scaled to how far the pointing at the reported
pixel would move if that star were left out (the labelled values, in arcseconds). That shift is a least-squares proxy
(order 2) and not astrometry.net's own fit, so it is indicative.

The xylist route is exact. The pixel convention was checked against astrometry.net's own `image2xy`: our positions
agree to (−0.02, −0.03) binned pixels, and solving `image2xy`'s own 342 sources through our route at order 2
reproduces the original solve to **0.004″**.

### 4.5 Plumbing

- `SolvingResult.selection`: the stage, whether a shadow was found, the counts per reject reason, the radius used,
  and whether the xylist or the image solved (or the fallback).
- `tools/source_survey.py`: runs the real solver on a frame in each stage (`--via-solver`), draws the pictures above,
  writes a CSV per frame, and can solve with CRPIX at the unit's fibre pixel (`--fibre mast01`). The fibre pixel is not
  in the FITS headers; it is in the unit's configuration (`guiding.rois[...]`).
- Everything is switched by one configuration value; nothing else changes while it is `original`.

## 5. The star selection in detail

A 10 s frame has 1351 detections after the noise cut. 54 were kept. This is where the rest went (each source is counted
under the first test it fails; a source can fail several):

| Fate | Count | Code | Why the test exists |
|---|---|---|---|
| kept | 54 | | |
| spare (passed, not needed) | 47 | | |
| faint | 410 | `FA` | A centroid's error scales with 1/SNR; noise blobs have no position. |
| in the shadow | 176 | `SH` | Dimmed stars have unreliable fluxes and gradients across them. |
| saturated | 10 | `SA` | A saturated star's centre is not where its light is. |
| small or sharp ("hot pixel") | 622 | `HP` | Mostly noise peaks (see below); a hot pixel is not a star. |
| blended | 20 | `BL` | A neighbour pulls the centroid. |
| at the frame's edge | 1 | `TR` | The window is truncated. |
| bad centroid | 11 | `BC` | The windowed centroid did not converge, or it moved too far (`max_centroid_shift_fwhm` seeing widths) from where detection put the star. |

(spec frame 3 of the traverse, with the loosened parameters below; 1351 = 54 + 47 + 410 + 176 + 10 + 622 + 20 + 1 + 11)

The stages, in order, and why each is there (`solvers/source_selection.py`):

1. **Detect against the local noise.** `Background2D` on a 50 px mesh, then a threshold of 2σ over 5 connected pixels,
   with σ the *local* background RMS. *Motivation:* the sky varies several-fold across a frame (dark under the shadow,
   bright at the edges) and photon noise follows it. One global threshold was 1.7σ in the bright flanks and 2.7σ in the
   dark centre, so thousands of noise peaks appeared in the flanks (1500 detections in the first 2000 columns, 27
   between columns 3000 and 5000). Using the local noise halved the detections (3073 to 1455) and removed them.
2. **Measure the seeing** from the unsaturated sources with SNR of at least 30, preferring those inside the low-coma
   radius (0.25 × the shorter side, 1411 px, by default; 7.6 px if there are too few).
3. **A fixed-window centroid** (a Gaussian of σ = FWHM/2.355, iterated to 2e-4 px or 16 passes). *Motivation:* a window that
   does not grow with the star measures a bright, comatic star the same way as a faint one, so a star's position does not depend on its brightness.
   The signal-to-noise is the window's mass over the local noise.
4. **Reject**, by the table above. The thresholds: shadow deficit above 0.05; peak above 0.8 of the saturation level
   (`SATURATE` in the header); FWHM under half the seeing or area under 5 px; a neighbour closer than 3 × seeing, of at
   least SNR 8 and 10% of the star's flux; within 3 window-sigmas + 5 px of the edge; SNR under 15 (relaxed to 10 if fewer than 30
   would survive); centroid not converged, or (inside the low-coma radius only) more than half a seeing from the first
   moment. *Motivation for the radius:* off axis the coma itself makes that shift, and the next step handles it.
5. **Rank the survivors by expected position error**: the noise term (1.1 σ_PSF / SNR) and, beyond the low-coma radius,
   a coma term that grows as the square of the distance.
6. **Choose with a grid quota**: 4 × 3 cells, at most `n_max` (150) in all and per-cell shares, lowest error first,
   starting inside the low-coma radius and widening it by 25% until 30 stars are chosen. *Motivation:* an even spread
   over the field keeps the fit from depending on one corner.
7. **Fail honestly**: fewer than 8 chosen stars fails the frame (`too_few_sources`) rather than solving from a handful.
8. **Write the xylist** through `pixel_grid` (the single place that converts between the full-frame and binned grids,
   in the pixel-centre convention), and solve it with order 1. If that fails, solve the image instead.

**Parameters we used that are not yet the defaults.** The periphery has strongly comatic stars, and in the centre,
where stars are round, the mirror's shadow is. With the design's starting values only 1 to 13 stars survived on
these frames. We loosened four parameters, each with a measured reason: `centroid_tolerance` 0.002 and 32 iterations
(a head-and-tail star iterates slowly), `min_sep_fwhm` 1.5 (a star's tail was counting as a neighbour), and
`max_fwhm_ratio` 2.5. Two further changes are already on by default: the local-noise detection, and `blend_ignores_hot`
(a hot-pixel-like detection no longer makes a neighbouring star "blended"). They give 42 to 54 chosen stars on the six frames,
of which 27 to 51 end up used by the fit.

**The "hot pixel" reject is a misnomer.** It is a size/sharpness test, and mostly catches noise peaks: 0.0 to 0.3% of the
sources it flags recur at the same sensor pixel in another frame. Real hot pixels never form the five connected pixels
detection needs. They are handled by the mask in section 4.3.

## 6. The shadow detector and the darkener

### 6.1 The detector (`imaging/mirror_shadow.py`)

It looks for one broad band, near vertical, whose position and size it does not assume.

1. **Suppress the stars.** Decimate the frame by 8 and take a 5-pixel median; then **clip each pixel to 5% above its local
   sky**, a median over 41 decimated pixels (328 px), wider than any star. (Why: lesson 4, below.)
2. **Project.** Sweep the band's tilt within 25° of vertical in 2° steps. For each, average the pixels in 16 px bins along
   the band's normal, giving a profile, and take a 5-bin median to remove one-bin spikes.
3. **Find the dip.** The sky level is a grey closing of the profile with an element half its length, which fills any dip
   narrower than that. The dip is `(sky − profile) / sky`, and its prominence is its depth over the profile's
   *point-to-point* noise.
4. **Pick the tilt:** the deepest dip; among angles within 1% of the deepest, the one nearest vertical.
5. **Refine the tilt.** Cross-correlate the band's profile in 7 horizontal strips and fit the band's shift against
   height; the slope is the tilt. It is accepted only if its standard error is small, the lean is within
   `near_vertical_deg` and its depth is at least 0.75 of the sweep's, and otherwise the sweep's tilt stands. The profile
   is then re-projected at the refined tilt.
6. **Report** the umbra (50% of the depth) and penumbra (10%) half-widths, the centre, the tilt and the depth, and the
   umbra and penumbra *bounds* as measured along the band's normal. Where the run of the profile reaches the frame's end,
   that bound is open (the shadow is cut by the frame). A shadow is present if the depth is at least 0.04, the
   prominence at least 6 and the penumbra half-width at least 60 px.

![The dip, the umbra and the penumbra on a real frame](mastrometry_stages/fig_profile.png)

*The profile of the frame in section 4.2. The orange dashed line is the sky continuum, the red curve is the dip the
detector scores (depth 0.21), and the shaded bands are the umbra (purple) and penumbra (blue) it reports. The flat
ends are bins with too few pixels, which are ignored.*

Defaults: `near_vertical_deg` 25, `angle_step_deg` 2, `bin_px` 16, `search_downsample` 8, `continuum_frac` 0.5,
`umbra_frac` 0.5, `penumbra_frac` 0.1, `min_depth` 0.04, `min_prominence` 6, `min_penumbra_half_width` 60, `edge_margin`
120 px. A full frame takes about 5 s. Two options exist and are **off**: `max_center_offset` (look only near the
centre) and `require_two_sided` (the dip must be a valley), because both reject real partial shadows (lesson 2).

### 6.2 The darkener (`darken_shadow`)

The folding mirror is not opaque, so bright stars under it survive as dimmed ghosts and the sky itself is suppressed. We
do not try to un-vignette the band; we replace it, umbra and penumbra, with an estimate of the sky that should be there:

1. Fit a smooth illumination model to the frame with the band masked out: a second-order polynomial, with 3σ clipping,
   so that it interpolates the true sky across the gap.
2. Match the fill's level to the sky in a thin collar (30 px) just outside the band, which corrects the polynomial's local
   offset.
3. Add noise with the collar's pixel-to-pixel scatter, so the patch is not a tell-tale flat region.

*Limit:* the fill is not seamless. On the frame in section 4.2 the filled pixels at the band's edge are 2.7% below the sky
just outside it (2117 against 2176 ADU), and a little smoother in noise (117 against 137 ADU). astrometry.net has not
objected, but it is a step.

### 6.3 Lessons learned

1. **The shadow is not where we thought it was.** We started by assuming it is centred. It is a band whose place depends
   on where the stage puts the mirror: centred when the mirror is in, with its edge at a vertical edge of the frame at the
   `sky` position, in part (even only a penumbra) outside the frame. We had taken `sky` frames as "mirror out, no shadow",
   and the detector's "false positives" on them were the real shadow. **Assume nothing about its position or size.**
2. **Two good-sounding fixes were wrong.** Restricting the search to the centre, and requiring the dip to be a valley
   between two shoulders, each removed real partial shadows at the frame's edge, which have no far shoulder.
3. **The header's stage position is not a label.** We first judged the detector against the stage position in the
   headers, and it misled us. What worked was looking at the frames in ds9 and telling the detector's answer from what the
   eye sees.
4. **A big star lifts the sky continuum.** Each profile bin is a mean over thousands of pixels, so a saturated or
   bloated star (100+ px wide, which the 5-pixel median does not touch) raises every bin it crosses, and the closing
   spreads that over a stretch of "sky". Flat sky then reads as a dip. On a real frame this made the detector report a
   20% shadow on a flat left edge and lose the true penumbra on the right. The fix is the clip to the local sky: a shadow
   only lowers the sky, so clipping above it costs it nothing.

   ![Flat sky read as a shadow](mastrometry_stages/fig_lesson_continuum.png)

   *The same frame, before and after the clip. Top: the detector picks the left end (x = 216, depth 0.20), where the sky
   is flat. Bottom: it picks the real penumbra at the right edge.*
5. **Do not let a wide band be its own noise.** Prominence is the depth over the profile's noise. Measured as the spread
   of the whole profile, a band that covers half of it *is* most of that spread, and a real shadow 15 to 21% deep and 4000 px wide
   scored about 3 against a threshold of 6. Use the point-to-point noise.
6. **The score picks the blurriest angle.** A tilted projection smears the band's edges, lowers the point-to-point noise
   and raises depth/noise. We pick by depth, with near-ties going to vertical. (Edge steepness was tried instead and
   fails on real frames: stars and noise decide it. A partial shadow is equally deep at every angle, and an arbitrary
   tilted one gave a mask 730 px too wide.)
7. **Test the way the failures hide.** Single frames passed while a mirror image of the same frame failed, and a
   synthetic matrix (7 positions from the left edge to the right edge × 3 sizes, in both orientations) caught the
   tilt bug. Both are now tests (`tests/test_mirror_shadow_detection.py`).
8. **Know the closing's limit.** A band wider than half the profile cannot be filled by the closing, so its measured
   depth is clipped. The real shadows are about half of the profile, so this is near the limit, not past it.
9. **A faint shadow can be left alone.** On 17 one-second frames the detector sees the shadow's width but at depth
   0.024 to 0.028, under its 0.04 gate, and reports none. A shadow that shallow is under the selection's own tolerance
   (deficit 0.05), so it rejects no star and needs no darkening. The gate stays.
10. **A tilt picked from a sweep is only as good as its step.** On a wide, near-vertical band a 2° step with a
    profile peak that is flat across several angles can be 8 to 12° off, and the darkener then paints the wrong band and
    leaves the old edges showing. Measure the tilt from the band itself (the shift between strips) and keep the sweep
    only as the starting point and the fallback.
11. **Do not assume a cut band is symmetric.** A band whose far side is outside the frame has a measured edge on one
    side only. Modelling it with symmetric half-widths made the umbra too narrow and drew the far part of the umbra as
    penumbra. The model now keeps the measured bounds, open at the frame's edge.

And two lessons from the work around it: **report the median, not the RMS** (one star at 13″ made a solve that was the
same at the median read 1.58″ instead of 1.10″; every stage has a few 3 to 13″ stars, and nothing in the solve rejects
them); and **check the display before concluding** (the survey's first pictures block-averaged the frame and hid the
faint stars, and we called a field "sparse" that was not).

## 7. Results

### 7.1 What was run

The survey tool ran the real solver, in each stage, on 37 full frames, on mast08, with CRPIX at the unit's fibre pixel
and the unit's hot pixel map, and the loosened selection parameters of section 5:

| set | unit | frames | what they are |
|---|---|---|---|
| traverse | mast01 | 6 | one field, 10 s, 2026-10-25: three `spec` frames (mirror in, shadow centred) and three `sky` frames (shadow at the edge); fibre pixel (4401, 2407) |
| acquisitions | mast02 | 25 | the first `spec` frame of each production acquisition of 2026-11-17 and 11-18, 5 s (one of 2 s), about ten different fields; fibre pixel (4050, 2439) |
| mirror-map | mast02 | 6 | six fields of a stage sweep, 2026-11-16, 5 s, sensor at 5 to 7 °C, all at stage position 100000 |

### 7.2 Residuals, by set

Each cell is original / shadowed / selective. Median over the frames of the set, with the range in brackets.

| set | stars used | median residual (″) | clipped RMS (″) |
|---|---|---|---|
| traverse (6) | 208 / 159 / 27 | 0.63 / 0.53 / 0.24  (0.49 to 0.93 / 0.40 to 0.71 / 0.21 to 0.34) | 0.91 / 0.77 / 0.32 |
| acquisitions (24 solved of 25) | 260 / 200 / 24 | 0.48 / 0.40 / 0.17  (0.41 to 0.63 / 0.29 to 0.51 / 0.08 to 0.37) | 0.75 / 0.60 / 0.22 |
| mirror-map (6) | 274 / 210 / 23 | 0.52 / 0.41 / 0.11  (0.42 to 0.58 / 0.35 to 0.45 / 0.08 to 0.16) | 0.78 / 0.60 / 0.13 |

The median residual is the median distance of the matched stars from their catalogue position through the solved WCS.
The clipped RMS is the RMS after a 3σ clip about the median. No selective solve fell back to the image solve.

The six mast01 frames one by one (the `original` columns reproduce the first run of 2026-11-19 to the last digit; the
`shadowed` and `selective` ones changed when the shadow detector's tilt and truncated-band fixes of section 6 went in,
mostly on the three `spec` frames, whose shadow is centred):

| frame | stars used | median residual (″) | clipped RMS (″) |
|---|---|---|---|
| **spec 1** | 210 / 176 / 23 | 0.79 / 0.65 / 0.23 | 1.22 / 1.10 / 0.38 |
| **spec 2** | 206 / 171 / 50 | 0.93 / 0.71 / 0.34 | 1.23 / 1.09 / 0.45 |
| **spec 3** | 199 / 163 / 55 | 0.74 / 0.66 / 0.26 | 1.10 / 0.94 / 0.35 |
| **sky 1** | 212 / 155 / 27 | 0.50 / 0.40 / 0.22 | 0.71 / 0.56 / 0.24 |
| **sky 2** | 214 / 155 / 27 | 0.49 / 0.40 / 0.21 | 0.66 / 0.59 / 0.23 |
| **sky 3** | 207 / 154 / 27 | 0.51 / 0.40 / 0.25 | 0.71 / 0.59 / 0.28 |

**Position at the fibre, selective minus original** (arcsec, RA·cos Dec and Dec), mast01: spec (−0.51, −0.09),
(−0.44, +0.11), (−0.33, −0.03); sky (−0.25, −0.31), (−0.23, −0.24), (−0.41, −0.03). **Shadowed minus original** ranges
from −0.42 to +0.06″ in RA, with no consistent direction.

**The shadowed stage on the centred frames.** With the detector's tilt fixed, more stars survive the darkening on the
three `spec` frames (163 to 176 used, against 115 to 168 before), and the plain RMS of that stage there is *worse* than
the original's (1.57, 1.85, 1.81″ against 1.48, 1.35, 1.10″) while its median and clipped RMS are still better. We
have not traced which stars carry the excess; the star map shows used stars close to the darkened band's edges on both
sides, where the darkening leaves a gradient. The selective stage does not meet this, because it never uses a star near
the band.

**Where the 0.2 to 0.5″ west offset of the selective stage comes from.** It is the fit order, not our selection or
the xylist route. On the same 342 `image2xy` stars through our route, order 2 reproduces the original solve to 0.00″
and order 1 moves it 0.54″ west. With our own selected stars at order 2, a third of the offset goes (0.13″ on `spec`),
because 27 to 50 stars do not constrain an order-2 fit as 200 do.

### 7.3 Where the time goes

The survey times each step of a solve. Median seconds per frame over the 36 frames that solved (full frame, on mast08):

| step | original | shadowed | selective |
|---|---|---|---|
| fill hot pixels (131 to 137 of them) | | 0.0 | 0.0 |
| detect the shadow | | 7.3 (6.6 to 8.5) | 7.3 (6.7 to 8.5) |
| darken the band | | **19.7** | |
| select the stars | | | **18.9** |
| solve-field | 4.2 | 4.4 | 1.3 |
| everything else (read, bin, read back) | 1.4 | 0.9 | 0.9 |
| **whole call** | **5.7** | **32.5** | **28.6** |

- The extra time is all Python. In the selective stage astrometry.net is *faster* than in the original (1.3 s against
  4.2 s), because it is given 20 to 50 stars rather than finding 300 to 500 itself.
- **Darkening is the slowest single step, and only the shadowed stage pays it.** The selective stage never darkens: its
  stars go to astrometry.net as a list, and nothing downstream reads the pixels.
- **The shadow detection need only be paid once per acquisition.** The acquisition can keep the detected shadow and
  reuse it while the phase and the stage position are the same (`Acquisition.shadow_for`). That is implemented and
  unit-tested, but the survey solves single frames, so no run here has exercised it.
- The slowest solves in the set (206 s, 121 s) are the frame that did not solve: astrometry.net runs to its CPU limit.
- Production solves a cropped ROI, not a full frame, so the detection and the selection will cost less; this was not
  timed.

### 7.4 The shadow data

What the detector reported, over the frames of each set (distances in pixels of the full-resolution frame, the centre
measured from the frame's centre across the band, + to the right). Where the band is cut by the frame ("cut"), the
far side is outside the frame: the detector then reports the bounds it measured (open at the frame's edge) as well as
the model's nominal half-widths. The half-width columns below are those nominal figures, so for a cut band they are not
what is in the frame; the bounds are (see the pictures in 7.6).

| set | shadow found | depth | centre across the band | umbra half-width | penumbra half-width |
|---|---|---|---|---|---|
| mast01 traverse, `spec` (3) | 3 of 3 | 0.48 to 0.54 | −114 to −44 (**centred**, not cut) | 1168 to 1192 | 1952 to 1960 |
| mast01 traverse, `sky` (3) | 3 of 3 | 0.62 to 0.68 | −2558 to −2550 (**at the margin**, cut) | 832 to 880 | 1608 to 1616 |
| mast02 acquisitions (24 solved) | 24 of 24 | 0.12 to 0.29 | ±3067 to ±3238 (**at the margin**, cut) | 504 to 600 | 904 to 1072 |
| mast02 mirror-map (6) | 6 of 6 | 0.12 to 0.14 | ±3094 to ±3216 (**at the margin**, cut) | 504 to 576 | 920 to 1056 |

- **The tilt is now measured, not picked from a sweep.** On `spec` 3 the band leans 1.2° from vertical; the first
  detector, which took the tilt from a 2° sweep, drew it at about 9°, which the pictures made plain (section 6).
- **mast01 and mast02 differ.** On mast01 the mirror-in shadow is centred, half the light deep, and wide; at `sky` it is
  a margin shadow, deeper still. On mast02 *every* frame has a narrower, much shallower shadow at the frame's margin
  (left or right, in the sign of the centre), including the acquisition frames that are `spec` frames. We have not
  analysed why (the mirror's position, its calibration, or the camera's mounting could each do it); the detector found it
  in every frame and the pictures show a real band.
- **No assumptions were needed.** The detector looked for the shadow with no hint of where it would be, and found a
  centred shadow, an edge shadow on either side, and shadows from 12% to 68% deep.
- The one frame without a shadow is the 2 s frame that did not solve.

### 7.5 Case A: a centred shadow (mast01, `spec` 3, 10 s)

The frame as it came (left), the shadow the detector found (middle), and the frame after darkening (right):

![The centred frame as it came, the shadow detected, and the frame darkened](mastrometry_stages/centred_shadow_detected.jpg)

*Middle: umbra (purple, −1309 to +1092 px from the frame's centre) and penumbra (blue, −1917 to +2004 px), 0.54 deep; the
band's centre is 44 px left of the frame's centre and it leans 1.2° from vertical (the detector reports the angle of its
normal: 88.8°). Right: the band replaced by a smooth estimate of the dim sky that should be there, without the stars in
it, with no trace of the old edges. It is not made as bright as the sky outside: the shadow is physically 54% deep, and
we replace it with a shadowed sky, not with a lit one.*

The three solves, left to right, as the unit's star maps (orange + is a source handed to the solver; blue circle a star the
solution used; the white + is the fibre pixel; the display is anchored on the sky, so the shadow shows black):

<table>
<tr>
<td width="33%"><img src="mastrometry_stages/centred_original_stars.jpg" alt="Original stage"></td>
<td width="33%"><img src="mastrometry_stages/centred_shadowed_stars.jpg" alt="Shadowed stage"></td>
<td width="33%"><img src="mastrometry_stages/centred_selective_stars.jpg" alt="Selective stage"></td>
</tr>
<tr>
<td valign="top"><em>Original: astrometry.net extracted 316 sources and used 199. Many of them are in the black band in the
middle, where the stars are dimmed. Residual RMS 1.10″; median 0.74″.</em></td>
<td valign="top"><em>Shadowed: with the band darkened astrometry.net extracted 185 sources, none in the band, and used 163. The
stars are on both sides. Residual RMS 1.81″; median 0.66″; clipped RMS 0.94″. The RMS is worse than the original's
here, with the median still better: see the note after the §7.2 table.</em></td>
<td valign="top"><em>Selective: 58 stars chosen, 55 used, on both sides of the band and away from it. None is near the fibre
(the white +), which is in the middle of the umbra: the fit interpolates across the shadow to it. Residual RMS 0.35″;
median 0.26″.</em></td>
</tr>
</table>

| | original | shadowed | selective |
|---|---|---|---|
| sources handed over / stars used | 316 / 199 | 185 / 163 | 58 / 55 |
| median residual / RMS | 0.74″ / 1.10″ | 0.66″ / 1.81″ (clipped 0.94″) | 0.26″ / 0.35″ |
| solved from | the image | the image | the xylist |
| time: whole call (shadow, darken or select, solve-field) | 6.9 s (5.5) | 35.2 s (8.2, 19.9, 6.0) | 26.2 s (8.3, 15.7, 1.3) |

### 7.6 Case B: a shadow at the margin (mast01, `sky` 3, 10 s)

![The margin frame as it came, the shadow detected, and the frame darkened](mastrometry_stages/margin_shadow_detected.jpg)

*Middle: the penumbra (blue) starts 942 px right of the frame's centre and the umbra (purple) 2399 px right of it; both run
to the frame's right edge, so the band is cut by the frame and its far side is outside it (the detector reports these
bounds as open at the edge, not a symmetric band). 0.68 deep; the band leans 1.8° from vertical (normal 88.2°). Right:
the band filled with the dim sky.*

![Original stage](mastrometry_stages/margin_original_stars.jpg)

*Original: 410 sources extracted, 207 used, many of them in the shadow (the black strip on the right). Residual RMS
1.57″, of which most is a few stars; the median is 0.51″.*

![Shadowed stage](mastrometry_stages/margin_shadowed_stars.jpg)

*Shadowed: 282 sources, none in the band, 154 used. RMS 0.91″; median 0.40″.*

![Selective stage](mastrometry_stages/margin_selective_stars.jpg)

*Selective: 42 chosen, 27 used, all well outside the shadow and in a group around the fibre (the white +, just left of
the penumbra). RMS 0.28″; median 0.25″.*

| | original | shadowed | selective |
|---|---|---|---|
| sources handed over / stars used | 410 / 207 | 282 / 154 | 42 / 27 |
| median residual / RMS | 0.51″ / 1.57″ (clipped 0.71″) | 0.40″ / 0.91″ (clipped 0.59″) | 0.25″ / 0.28″ |
| solved from | the image | the image | the xylist |
| time: whole call (shadow, darken or select, solve-field) | 6.9 s (5.4) | 34.7 s (8.5, 19.9, 5.3) | 26.1 s (8.5, 15.9, 0.8) |

**What the two cases show.** In the centred case the fibre is inside the umbra, so no stage has a star near it, and the
selective solution is an interpolation across the shadow from stars 1700 to 2500 px away on either side. In the margin case the
fibre is next to the penumbra and the selective stage has stars all round it. The on-sky test has to be done on both,
because they are different situations for the fit.

**Fallbacks:** none in any of the final runs.

## 8. What we do not know yet

- **Accuracy.** Everything above is the agreement of a solution with its own stars, plus differences between stages. We
  have no ground truth. The next stage is an on-sky test: put a Gaia star on the fibre, solve, and compare the
  solution with the star's Gaia position. That settles the order-1 decision and the 0.5″ question, and it is the real
  test of all three stages.
- **How representative the frames are.** 37 frames (36 solved), two cameras. mast01 is one field, three frames per case; mast02 is
  about ten fields, but only `spec` frames (all with a margin shadow) and at most one frame per acquisition. This
  shows the pipeline works and the stages differ; it does not make one a proven improvement.
- **mast02's margin shadow.** It is found reliably, but we do not know why a `spec` frame there has a shallow shadow at
  the margin and not a centred one; that is for whoever knows the mirror's calibration.
- **Whole frames, not production crops.** The survey solves full frames with CRPIX at the fibre. Production solves the
  sky and guiding ROIs. The ROI path (the crop, and the hot pixel map by full-frame pixels) is implemented and tested on
  synthetic data, but the numbers here are not from it.
- **The selective stage on comatic fields** needs the loosened parameters in section 5, which are not yet defaults.
- **A hot pixel map goes stale.** It records its build date and the frames' exposure and temperature, but the solver
  does not yet check its age. mast04's needs a rebuild at its operating temperature.
- **The darkener's 20 s.** Only the shadowed stage pays it; if that stage is to run for real, the darkener needs
  profiling.
- **Open:** the detection threshold (2σ over 5 pixels) lets through mostly noise (about 1450 detections per frame, of
  which about 50 are chosen); `solvers/tests` still has to be run by hand on a unit with the indexes.

## Appendix A: the astrometry.net call

`solve-field` (the Cygwin build) with the same options in every stage, except where noted.

| Option | Value | Stage |
|---|---|---|
| `--scale-units arcsecperpix`, `--scale-low`, `--scale-high` | 0.47 and 0.58 (0.5232″ per binned pixel, ±10%) | all |
| `--index-dir` | the indexes (`index-5202`, `5203`; 9.9 GB, on the RAM disk) | all |
| `--no-plots --overwrite --solved none --match none --rdls none` | | all |
| `--cpulimit` | 30 s | all |
| `--ra --dec --radius 2.0` | a position hint, only when a target is known | all |
| `--crpix-x`, `--crpix-y` (or `--crpix-center`) | the fibre or ROI centre on the binned grid (the frame centre if none) | all |
| `--corr` | the correspondences (matched stars) | all |
| `--dir`, `--temp-dir` | the scratch folder on the RAM disk | all |
| `--new-fits <file>` | the solved frame | original, shadowed |
| `--wcs <file>`, `--new-fits none` | there is no image for `--new-fits` to annotate | selective |
| `--width`, `--height`, `--x-column X`, `--y-column Y`, `--sort-column FLUX` | the xylist's grid and columns | selective |
| `--uniformize 0` | the selection has already spread the stars | selective |
| `--tweak-order 1` | tweak stays on, at order 1 | selective |

**Outputs, per solve:** the solved FITS (with the WCS; for selective it is built from `.wcs` and the binned image),
`...-result.txt` (the command, return code, output and timing), `.corr` (field and catalogue positions and weights of
the matched stars), the star map (`-stars.jpg` and `-stars.json`), and for selective also `.xyls` and `.wcs`.
`SolvingResult` carries RA and Dec from CRVAL, the rotation, the pixel scale, the CD matrix, the sources and matched
stars, the index file and the `selection` record.

## Appendix B: running it

```
# the pictures and CSVs for a frame, in each stage, with CRPIX at mast01's fibre
python tools/source_survey.py FRAME.fits --out DIR --via-solver --fibre mast01 \
    --param centroid_tolerance=0.002 --param centroid_max_iterations=32 \
    --param min_sep_fwhm=1.5 --param max_fwhm_ratio=2.5

# the same, also copying each frame's pictures (survey picture; per stage the star map, its data, the result text and the
# shadow picture) to the frame's own folder, named ...,solver=mastrometry.net,stage=<stage>-stars.jpg and so on.
# The survey.csv has each stage's time per step (<stage>_t_shadow, _t_darken, _t_select, _t_solve_field, _t_other).
python tools/source_survey.py FRAME.fits ... --out DIR --via-solver --beside-frames --fibre mast02 --hot-pixels mast02

# a unit's hot pixel map (writes MAST/<unit>/Calibration/hot_pixels.json on the share)
python tools/build_hot_pixels.py FRAME_FOLDER --unit mast01 --last-nights 3
```

Code: `src/solvers/{mastrometry,selection_stage,source_selection,survey_solve}.py`, `src/imaging/{mirror_shadow,hot_pixels}.py`,
`tools/{source_survey,build_hot_pixels}.py`. Decisions and the full history are in `DECISIONS.md`. The detector and selection
have about 170 tests (`tests/test_{mirror_shadow_detection,source_selection,selection_stage,hot_pixels,source_survey,survey_solve}.py`).
