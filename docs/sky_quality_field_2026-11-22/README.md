# focus_on_star sky-quality frame: how wide a field, 2026-11-22

The sweep's box (`fov_percent`, 10% of the frame, linear) is small on purpose: the focus star is isolated there. The
sky-quality frame (step 7) was measured over the same box, and an isolated star has too few neighbours in it to match the
catalog: **3 of the 6 mast03 runs of 15 and 18 Nov 2026 got no zero point.** These scripts replay step 7 offline on the
saved sky-quality frames (`MAST/mast03/2026-11-15` and `2026-11-18`, `Calibration/FocusOnStar/<run>/`) to choose a
better field. The result is merged: `sky_quality_fov_percent` (default 30) and `zero_point_max_mag` (default 15.5) in
MAST_common#238, used by MAST_unit#419.

## Scripts (`scripts/`)

All three run on mast-ns-control, from a folder holding `top/unit` and `top/common` (a snapshot of the unit and common
trees) and a venv with `h5py`; they read the Gaia DR3 catsHTM files from `/mnt/last0/LAST-CONTAINER/catalogs/GAIA/DR3`.
The paths are hard-coded; they are scratch-quality, kept so the numbers can be redone.

- `count_sq.py`: runs the real `field_stars`, `field_catalog` and `sky_quality.measure` on each frame at a 10, 15, 20 and 30%
  box and prints the catalog, detected and matched stars and the zero point. At 10% it reproduces the recorded counts exactly.
- `coma_sq.py [FOV%]`: for each matched, unsaturated star in the box, its zero point (minus the run's median) and
  peak/flux against distance from the fibre pixel, pooled by radius.
- `shape_sq.py [FOV%]`: per-star adaptive-moment ellipticity, FWHM and the angle of the long axis relative to the radius,
  for stars brighter than 12 sigma, pooled by distance from the fibre pixel.

## Results (`results/`)

- `step7_replay_by_box_size.txt`: **10%: 3 runs with no zero point; 15%: the same 3; 20%: all 6 have one (5 to 6 matched
  stars for the failing runs); 30%: 7 to 18 matched stars.** Zero points are alike across box sizes (27.3 to 27.6).
- `zero_point_vs_radius_30pct.txt`: no radial trend in the zero point out to 30% (76 stars; slope -0.03 mag per 1000 px,
  inside the scatter). Faint stars (G of 15.5 or fainter) carry most of the scatter: the reason for `zero_point_max_mag`.
- `star_shapes_vs_radius.txt`: at 30% (69 stars) ellipticity and FWHM do not grow with radius and the long axes are not
  radial: no coma signature within the box. At 40% the bin beyond 1,300 px shows a hint of radial alignment (+0.32 +- 0.12,
  about 2.7 sigma), which is one reason not to go wider than 30%.

## Caveats

- mast03 only, six runs, one night pair; the star shapes are elongated everywhere (about 0.15 to 0.25), from seeing,
  tracking or defocus on a 3 s exposure, which can hide a mild coma pattern.
- The radius is from the fibre pixel, not the optical centre; the optical-centre survey (on hold) found disagreements.
- peak/flux is a poor size proxy (it tracks magnitude, since a faint star's flux aperture adds noise); the shape script is
  the better coma check, and `shape_sq.py`'s per-star shapes are measured on 3 s frames.
- The box sizes are linear percentages of the frame (8288 x 5644 px at bin 1).
