# Solvers — guidance & warnings

## ⚠️ Fragile coordinate surface: the binned/cropped → full-frame transform

`MastrometryDotNet` (`mastrometry.py`) pre-downsamples (2×2) and optionally
ROI-crops the image **in numpy before** calling astrometry.net. As a result the
WCS astrometry.net returns lives in the **binned, cropped** grid — not the
original full frame. Relating that solution back to original full-frame pixels
requires a coordinate transform, and **that transform has already produced two
silent sub-arcsecond bugs** (a 0.5px naive-division offset, and an
integer-division ROI `refpix` that biased pointing by ~0.4").

**Rules when working in this directory:**

1. **All** original↔binned / ROI-`refpix` coordinate math goes through
   `solvers/pixel_grid.py`. Do not hand-roll `orig/factor` or `(center-start)//factor`
   inline — those are the exact bugs. `--crpix-x/--crpix-y` are FITS **1-based**.
2. The transform is **silent on failure**: the WCS still solves and the numbers
   look plausible; the error surfaces only as a small constant pointing offset
   (worst on the spec/fiber path). Treat any change here as high-risk.
3. After any change to `pixel_grid.py`, `mastrometry.py` downsample/crop/refpix
   logic, or the solve-field flags, **run `solvers/tests/` by hand** (see that
   dir's README): its integration tests need astrometry.net + indexes + a
   sample FITS, so CI never runs them. The pure-math tests
   (`tests/test_pixel_grid.py`) run anywhere and in CI.
   - Run them on a machine that has `solve-field` and the index set
     (`D:\mast-indexes`): a unit, a dev unit, or labcomp2.
   - **A skip is not a pass.** Elsewhere both tests skip, and a plain `pytest`
     run still looks green. Run with `-ra` and confirm they ran.
   - If they cannot be run, say so in the PR and ask for a run on a unit. Do
     not report the change as verified.
4. **Tweak (SIP) is intentionally ON** in `mastrometry.py` — do not re-add
   `--no-tweak` without reading the comment there; it breaks agreement with the
   reference solver and with full-frame pixel consistency.

## The star map (`star_map.py`)

Each kept solve leaves `<stem>,solver=mastrometry.net-stars.jpg` and `-stars.json` beside the solved
FITS: the frame with the stars astrometry.net **extracted** (orange ticks) and the ones it **used**
(blue circles, the radius growing with how far the reported point would move without the star), and
the per-star numbers. It is made in the cleanup thread, so it adds nothing to the solve, and a
failure there is logged and costs none of the other artifacts.

- It needs `--corr <path>` (the solver used to pass `--corr none`); the extraction list is the
  `.axy` `solve-field` already leaves in the scratch folder. `--rdls` and `--match` stay `none`.
- It draws on the frame `solve-field` was given, in that frame's own pixel grid, so it does not
  use `pixel_grid.py` and does not touch the coordinate surface. It does change the solve-field
  flags, so rule 3 above applies: run `solvers/tests/test_star_map_integration.py` by hand on a
  unit, which is what confirms the real column names of the `.corr`.
- The influence numbers (leverage, Cook's distance, fibre shift) are a **proxy** from a
  least-squares polynomial fit of astrometry.net's tweak order, not its own arithmetic. They rank
  the stars; they do not reproduce the solver. See the module docstring.
- The frame is found by `downsampled_frame_path`, never by a glob: `solve-field` leaves
  `downsampled_<stem>.axy` beside it, which a pattern matches first.

## Source selection (`source_selection.py`) and the survey (`tools/source_survey.py`)

Stage 0 of the source-filtering plan (`mast-claude-config`, `plans/mastrometry_source_selection_design.md`).
`select_sources` detects with `extract_sources`, measures each source with a fixed-window centroid,
rejects the ones that bias a fit (faint, shadow, saturated, hot pixel, blended, truncated, bad
centroid), ranks and selects the rest, and chooses the fit order. It is **pure and not wired into
`MastrometryDotNet`**: nothing here changes a solve. The survey runs it on saved frames and writes a
picture (shadow masked, kept / spare / rejected marked), per-source and per-frame CSVs, an
exclusion-width sweep and a cross-frame consistency table, only under `--out`.

- Coordinates are 0-based array coordinates of the array given (the crop, with `--roi`).
- Every failed test is kept in `Selection.all_reasons`; `fate` carries the first, `faint` first, so
  the noise blobs a 2 sigma detection finds are not called blends.
- A neighbour counts toward a blend only with SNR >= `neighbour_min_snr`; a merged pair is caught
  by its width, only where the width is reliable (SNR >= 30).
- Detections along the shadow's penumbra edges (the sky step the 50 px mesh leaves) turn up as
  blends; they are not kept, but they are what the survey is meant to show.
- A full 8288 x 5644 frame takes ~8 s per selection run and ~2.8 GB; use `--repeat 1` for a batch,
  or `--roi`.
- The solver layer ("the stars the solver used") is not in the survey: it needs `solve-field` and the
  xylist handover (Stage 2).

## Artifact names come from the input's stem

`MastrometryDotNet.solve` names its outputs `<input stem>,solver=<name>.fits` and
`<input stem>,solver=<name>-result.txt`. Never derive them by editing the input's
name (`.replace(".fits", ...)`): a `sav*.tmp` guide frame then gives both the same
path, and the result text overwrites the solved FITS (#314).

## Why the surface is kept (don't "simplify" it away)

A bare `solve-field --downsample N` on the full frame would avoid this surface
entirely (its WCS comes back in full-frame pixels). We **deliberately keep** the
numpy pre-downsample + crop path so ROI cropping stays implementable in-house
without reopening the solver choice. The surface is the price of that
flexibility; it is fenced, not removed. See `COORDINATE_SURFACE.md`.

## Background

Conventions, derivation, and the equivalence evidence are in
`pixel_grid.py` (module docstring) and `COORDINATE_SURFACE.md`. The original
standalone study lives at `C:\MAST\mastrometry-equivalence`.
