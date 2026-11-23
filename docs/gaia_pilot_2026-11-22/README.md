# Gaia pilot, 2026-11-22: the solved WCS of `original` and `selective` against Gaia, near the fibre

An **offline** pilot of the survey in `mastrometry_gaia_accuracy_survey.md` (mast-claude-config): no telescope time,
frames already on the share. It asks whether `solving.source_selection.mode = selective` places stars closer to their Gaia
positions than `original` does, close to the fibre. The scoring is now the tool `tools/gaia_accuracy_survey.py analyse`
(MAST_unit PR #420); these scripts are what was run around it.

## What was run

1. **Solve each frame in both modes** on mast08 (Windows, real RAM disk, `D:\mast-indexes`), one single-frame survey run
   per frame, with `batch_survey.py` (a loop over `tools/source_survey.py --via-solver --solver-modes original,selective`):

       python batch_survey.py data\mast02_acquisition_frames.txt OUT\acq mast02 4050 2439
       python batch_survey.py data\mast01_traverse_spec_frames.txt OUT\m01 mast01 4401 2407

   The loosened selection parameters of `mastrometry_stages.md` (Appendix B) are set inside it. The frames are
   read from `\\mast-ns-control\mast-share\MAST\<unit>\`, the lists hold the paths below that.
2. **Move a light copy to control**, where the Gaia catalog is mounted (mast08's venv has no `h5py`): `export_stub.py`
   writes, per frame, the CSVs, the `.corr` files and a header-only copy of each solved FITS, which is all the tool reads.
3. **Score** with the tool, on control:

       PYTHONPATH=<top> python tools/gaia_accuracy_survey.py analyse <stub dir> --fibre-xy 4050 2439 --out <out> \
           --catalog-root /mnt/last0/LAST-CONTAINER/catalogs/GAIA/DR3

4. `extractor_vs_fixed_window.py` compares astrometry.net's own star positions (`original`'s `.corr`) with our fixed-window
   centroids; `count_guide.py` counts the clean stars inside a disk about the fibre and what the shadow took, per frame.

The scripts have mast08's and control's paths in them. They are scratch-quality, kept so the numbers can be redone.

## Results (`results/`)

- `mast02_acquisitions/bins.csv`, `frames.csv`, `stars.csv.gz`: the tool's output on 24 mast02 acquisition frames
  (the 2 s frame did not solve). Near the fibre the offset of the median held-out star from Gaia is **0.52 arcsec for
  `original` and 0.17 for `selective`** (0-300 px, 126 stars in 19 frames); `selective` is closer in 18 of 19 frames.
  Beyond 1200 px the two are alike. Both sit north of Gaia.
- `extractor_vs_fixed_window.txt`: astrometry.net's own positions agree with the fixed-window centroid to 0.00 arcsec overall
  and 0.10 arcsec south within 1000 px of the fibre (3,599 stars). About 0.1 arcsec of `original`'s offset is that
  difference; the other 0.25 arcsec of the gap is the fits.
- `guide_stars_and_shadow.txt`: on mast01's traverse `spec` frames the shadow (umbra half-width about 1,330 px,
  penumbra about 1,960 px) is over the fibre, and **no clean star lies within 1,411 px of it** (all 20 frames). On mast02's
  acquisition frames (mirror out) 194 clean stars lie in that disk at the median.

## Caveats

- Star positions against Gaia, **not the fibre**: only the on-sky Gaia-star-on-fibre test measures that.
- The `selective` fit and the scoring use the same fixed-window centroid.
- The 24 frames come from about 10 fields, so standard errors over stars are optimistic; the tool also gives them over frames.
- One unit (mast02). mast01's traverse frames cannot be scored near the fibre: the shadow covers it.
- The Gaia positions come from the catsHTM files on `\\last0\catalogs\GAIA\DR3`, with proper motion applied to the frame's epoch.
