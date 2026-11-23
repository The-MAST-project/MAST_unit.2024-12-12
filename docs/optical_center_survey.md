# Optical-centre finder vs. the config-DB fibre centres — survey results

Written for: MAST engineers deciding whether the coma-based optical-centre finder
(`calibration.analysis.optical_center`, see `optical_center_design.md`) can be trusted
to set or check `guiding.rois[fcu_v2].fiber_x/y`.

**Status (2026-11-21): the finder and the DB centres disagree by hundreds of pixels,
and no ground truth exists yet to say which is right.** Part of the disagreement is
defocus; part is not.

## Method

`tools/optical_center_survey.py` runs the finder on real frames and compares the
detected centre with the fibre centre in force in the config DB (0-based full-frame
pixels, delta = detected - DB, 0.262 arcsec/px).

- Frames: acquisition and autofocus frames on the share for mast01-03, run on
  mast-ns-control. 267 acquisition frames after each unit's last DB change (mast01 169,
  mast02 64, mast03 34); 185 autofocus-sweep frames for the focus-dependence test.
- "After the DB change": from the MongoDB oplog (`local.oplog.rs`, available from
  2026-10-07). mast02 changed (5175,3066) -> (4050,2439) on 2026-10-12 and (4051,2440)
  on 2026-11-18; mast03 changed 31 times until 2026-11-16; mast01's current value first
  appears 2026-10-25; mast04 has no history and is excluded.
- Two variants: `production` (defaults) and `masked` (mirror-shadow umbra + penumbra
  excluded, local noise).
- Per frame: one JPEG beside the source (`<stem>-optical-center.jpg`) with the shadow,
  DB centre, detected centre(s), a legend with deltas, and star size.
- Star size: median half-flux diameter (HFD) of up to 60 bright, isolated, unsaturated
  stars. A first-pass metric; absolute values are indicative.

Run: `python tools/optical_center_survey.py --manifest M.tsv --out DIR --fibre-from-config`
(manifest columns `unit`, `path`, optional per-frame `fibre_x`, `fibre_y`).

## Results

Pooled fit over all frames, `production` (detected vs DB, px):

| unit | detected | DB | delta |
|---|---|---|---|
| mast01 | (4302, 2554) | (4401, 2407) | (-102, +145) |
| mast02 | (4077, 3632) | (4051, 2440) | (+32, +1193) |
| mast03 | (3359, 2622) | (3986, 2809) | (-639, -188) |

- Masking the shadow does not fix the disagreement (the `masked` variant is mostly worse).
- Radiality is high (spin-1 about 0.9): the star-shape pattern is coherently radial, so
  the finder is fitting a real pattern, not noise. It just may not be centred on the
  fibre.
- Repeatability: the same field on the same night scatters 40-120 px; field-to-field
  scatter is about 140-190 px (pooled per-frame scatter of 200-340 px on mast01).
  The finder fails or refuses on 25-30% of mast01 frames.

### Focus dependence

Autofocus sweeps: the detected x moves 1000-2000 px to the right at +-300-400 focuser
steps, on either side of focus; y is stable. Defocus biases the finder.

Many acquisition frames are poorly focused (median HFD 25-30 px, best about 14-15 px).
Median detected - DB by star-size tercile, `production`:

| unit | sharpest | middle | blurriest |
|---|---|---|---|
| mast01 | (-416, +151) | (-356, +176) | (-362, +83) |
| mast02 | (-42, +1121) | (-28, +1183) | (+40, +1187) |
| mast03 | (-406, -193) | (-715, -164) | (-831, -168) |

- **mast01, mast02:** offsets do not depend on star size, so they are not defocus
  artefacts. mast02's +1.1k px in y persists at every sharpness; its cause is unknown
  (axis differs from the fibre, detector tilt, or a systematic in the finder).
- **mast03:** x offset grows with blur (correlation -0.42 production, -0.59 masked).
  In the `masked` variant the sharpest third is only -154 px from the DB (interval
  -327 to +23, n=11), so much of mast03's offset may be defocus.

## Caveats

- No ground truth for either method. The decisive test is the Gaia-on-fibre one.
- Terciles are small (n = 11-46), use per-frame medians (not pooled fits), and are
  confounded with night and field.
- The DB values may themselves be placeholders: mast04 equals mast01, and `fcu_v1` is
  (4346, 2810) for all units. September sweeps predate the DB trust window.
- Extractions are not saved, so a pooled fit on the best-focused subset needs a rerun.

## Next steps

1. An intra/extra-focal pair on a mast02 field: coma keeps its direction either side
   of focus, defocus effects flip. Needs sky time.
2. Rerun with a focus cut (HFD below about 20 px) and pooled fits per unit.
3. Do not write finder output into the DB until one of these agrees with an
   independent measurement.
